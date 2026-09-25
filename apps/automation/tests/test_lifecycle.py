"""Lifecycle automation: track a customer, say how interested they are, hand them to a
teammate, tell the team. All deterministic, all through the same workflow engine.
"""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from apps.accounts import notifications
from apps.accounts.models import Account, Membership
from apps.automation import api as automation_api
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import enroll, enroll_for_trigger, validate_definition
from apps.contacts.models import Contact
from apps.conversations.models import Conversation
from apps.core.actions import ActionError, run_action
from apps.crm import services as crm
from apps.crm.models import Lead

SEND = "apps.email.services.send.send_system_email"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def member(account, username, role=Membership.Role.MEMBER, **user_fields):
    user = User.objects.create_user(
        username, user_fields.pop("email", f"{username}@example.com"), "pw", **user_fields)
    Membership.objects.create(user=user, account=account, role=role)
    return user


@pytest.fixture
def team(account):
    return {
        "owner": member(account, "owner", Membership.Role.OWNER),
        "admin": member(account, "admin", Membership.Role.ADMIN),
        "ada": member(account, "ada"),
        "sam": member(account, "sam"),
    }


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567", first_name="Chanda", source="whatsapp")


@pytest.fixture
def conversation(account, contact):
    return Conversation.objects.create(account=account, contact=contact, channel="whatsapp")


def other_conversation(account, n, assigned_to=None, status=Conversation.Status.OPEN):
    contact = Contact.objects.create(account=account, phone=f"+26097000{n:04d}")
    return Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", assigned_to=assigned_to, status=status)


def publish(account, trigger, steps, slug="wf"):
    return automation_api.upsert_published_workflow(
        account, slug=slug, name=slug, definition={"trigger": trigger, "steps": steps})


# --- lead status ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_lead_moves_through_its_statuses(account, contact):
    lead = crm.create_lead(account, contact, source="test")
    assert crm.set_lead_status(lead, "contacted") is True
    assert crm.set_lead_status(lead, "qualified") is True
    lead.refresh_from_db()
    assert lead.status == "qualified"
    assert crm.set_lead_status(lead, "qualified") is False           # repeat: quiet no-op


@pytest.mark.django_db
def test_a_converted_lead_or_a_bad_status_is_refused_in_plain_words(account, contact):
    lead = crm.create_lead(account, contact)
    with pytest.raises(ValueError, match="new, contacted, qualified or lost"):
        crm.set_lead_status(lead, "converted")
    crm.convert_lead_to_deal(lead)
    lead.refresh_from_db()
    with pytest.raises(ValueError, match="already in your pipeline"):
        crm.set_lead_status(lead, "lost")


@pytest.mark.django_db
def test_status_changes_start_the_matching_workflows(account, contact):
    stop = [{"id": "stop", "type": "stop"}]
    for trigger in ("lead.status_changed", "lead.qualified", "lead.lost"):
        publish(account, {"type": trigger}, stop, slug=trigger)
    lead = crm.create_lead(account, contact)

    crm.set_lead_status(lead, "contacted")
    assert set(WorkflowRun.objects.values_list("workflow__slug", flat=True)) == {"lead.status_changed"}
    WorkflowRun.objects.all().delete()
    crm.set_lead_status(lead, "qualified")
    assert set(WorkflowRun.objects.values_list("workflow__slug", flat=True)) == {
        "lead.status_changed", "lead.qualified"}
    WorkflowRun.objects.all().delete()
    crm.set_lead_status(lead, "lost")
    assert set(WorkflowRun.objects.values_list("workflow__slug", flat=True)) == {
        "lead.status_changed", "lead.lost"}
    run = WorkflowRun.objects.get(workflow__slug="lead.lost")
    assert run.context == {"lead_id": lead.public_id, "status": "lost", "previous_status": "qualified"}


@pytest.mark.django_db
def test_the_action_updates_a_lead_and_respects_the_account(account, contact):
    lead = crm.create_lead(account, contact)
    result = run_action("update_lead_status", {"account": account}, lead=lead, status="qualified")
    assert result == {"lead_id": lead.public_id, "status": "qualified", "changed": True}
    with pytest.raises(ActionError):
        run_action("update_lead_status", {"account": Account.objects.create(company_name="Other")},
                   lead=lead, status="lost")
    with pytest.raises(ActionError, match="qualified or lost"):
        run_action("update_lead_status", {"account": account}, lead=lead, status="nonsense")


@pytest.fixture
def owner_client(client, account, team):
    client.force_login(team["owner"])
    return client


@pytest.mark.django_db
def test_the_lead_page_lets_you_change_status(owner_client, account, contact):
    lead = crm.create_lead(account, contact)
    page = f"/sales/leads/{lead.public_id}/"
    assert 'name="status"' in owner_client.get(page).content.decode()
    resp = owner_client.post(page, {"action": "status", "status": "qualified"}, follow=True)
    assert "Marked as qualified." in resp.content.decode()
    lead.refresh_from_db()
    assert lead.status == "qualified"
    bad = owner_client.post(page, {"action": "status", "status": "converted"}, follow=True)
    assert "new, contacted, qualified or lost" in bad.content.decode()


@pytest.mark.django_db
def test_a_lead_opened_from_a_conversation_carries_it_into_its_workflows(account, contact, conversation):
    publish(account, {"type": "lead.created"}, [{"id": "stop", "type": "stop"}])
    run_action("capture_conversation_lead", {"account": account}, account=account, contact=contact,
               conversation_id=conversation.public_id, signal="how much")
    assert WorkflowRun.objects.get().context["conversation_id"] == conversation.public_id


# --- assigning a conversation -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_auto_assign_picks_the_least_busy_teammate(account, team, conversation):
    for n in range(3):
        other_conversation(account, n, assigned_to=team["owner"])
    other_conversation(account, 10, assigned_to=team["admin"])
    other_conversation(account, 11, assigned_to=team["ada"])
    other_conversation(account, 12, assigned_to=team["ada"])
    other_conversation(account, 13, assigned_to=team["sam"], status=Conversation.Status.CLOSED)  # closed: not load
    result = run_action("auto_assign_conversation", {"account": account}, conversation=conversation)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["sam"] and result["changed"] is True


@pytest.mark.django_db
def test_ties_go_to_whoever_joined_first(account, team, conversation):
    run_action("auto_assign_conversation", {"account": account}, conversation=conversation)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["owner"]


@pytest.mark.django_db
def test_an_assigned_conversation_is_left_with_its_owner_unless_forced(account, team, conversation):
    conversation.assign(team["sam"])
    result = run_action("auto_assign_conversation", {"account": account}, conversation=conversation)
    assert result["changed"] is False
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["sam"]
    run_action("auto_assign_conversation", {"account": account}, conversation=conversation,
               email="ADA@example.com", force=True)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["ada"]


@pytest.mark.django_db
def test_assignment_only_ever_uses_this_businesss_active_team(account, team, conversation):
    User.objects.filter(pk__in=[team[k].pk for k in ("owner", "admin", "ada")]).update(is_active=False)
    outsider = User.objects.create_user("outsider", "outsider@example.com", "pw")
    Membership.objects.create(user=outsider, account=Account.objects.create(company_name="Other"),
                              role=Membership.Role.OWNER)
    run_action("auto_assign_conversation", {"account": account}, conversation=conversation)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["sam"]
    with pytest.raises(ActionError, match="isn't on your team"):
        run_action("auto_assign_conversation", {"account": account}, conversation=conversation,
                   email="outsider@example.com", force=True)


@pytest.mark.django_db
def test_nobody_to_assign_is_an_error_not_a_crash(account, conversation):
    with pytest.raises(ActionError, match="nobody on your team"):
        run_action("auto_assign_conversation", {"account": account}, conversation=conversation)


# --- telling the team ------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_owners_means_owners_and_admins_only(account, team):
    assert {u.username for u in notifications.recipients(account, "owners")} == {"owner", "admin"}


@pytest.mark.django_db
def test_assignee_falls_back_to_owners_so_nobody_is_skipped(account, team, conversation):
    assert {u.username for u in notifications.recipients(account, "assignee", conversation=conversation)} == {
        "owner", "admin"}
    conversation.assign(team["ada"])
    assert [u.username for u in notifications.recipients(account, "assignee", conversation=conversation)] == ["ada"]


@pytest.mark.django_db
def test_a_named_recipient_must_be_a_teammate(account, team):
    assert [u.username for u in notifications.recipients(account, "SAM@example.com")] == ["sam"]
    with pytest.raises(notifications.NotifyError, match="isn't on your team"):
        notifications.recipients(account, "stranger@example.com")
    with pytest.raises(notifications.NotifyError):
        notifications.recipients(account, "everyone")


@pytest.mark.django_db
def test_notify_sends_the_text_with_a_link(account, team, conversation):
    with patch(SEND) as send:
        sent = notifications.notify_team(
            account, to="owners", subject="Hot lead", text="Chanda wants a quote.",
            path=f"/inbox/{conversation.public_id}/")
    assert sent == 2
    assert {c.kwargs["to_email"] for c in send.call_args_list} == {"owner@example.com", "admin@example.com"}
    body = send.call_args.kwargs["text_body"]
    assert "Chanda wants a quote." in body and f"/inbox/{conversation.public_id}/" in body
    assert body.count("http") == 1


@pytest.mark.django_db
def test_one_failed_delivery_does_not_stop_the_rest(account, team):
    with patch(SEND, side_effect=[RuntimeError("smtp down"), None]) as send:
        assert notifications.notify_team(account, to="owners", subject="s", text="t") == 1
    assert send.call_count == 2


@pytest.mark.django_db
def test_a_teammate_without_an_email_is_skipped_and_empty_text_is_refused(account, team):
    User.objects.filter(pk=team["admin"].pk).update(email="")
    with patch(SEND) as send:
        assert notifications.notify_team(account, to="owners", subject="s", text="t") == 1
    assert send.call_args.kwargs["to_email"] == "owner@example.com"
    with pytest.raises(notifications.NotifyError):
        notifications.notify_team(account, to="owners", subject="s", text="   ")


# --- the workflow steps -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_lead_then_update_status_act_on_the_same_lead(account, team, contact):
    workflow = publish(account, {"type": "manual"}, [
        {"id": "a", "type": "create_lead", "source": "menu", "next": "b"},
        {"id": "b", "type": "update_lead_status", "status": "qualified", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ])
    run = enroll(workflow, contact)
    assert run.status == WorkflowRun.Status.COMPLETED
    lead = Lead.objects.get(contact=contact)
    assert (lead.status, lead.source) == ("qualified", "menu")
    assert run.context["lead_id"] == lead.public_id


@pytest.mark.django_db
def test_updating_a_customer_with_no_lead_fails_loudly(account, contact):
    workflow = publish(account, {"type": "manual"}, [
        {"id": "b", "type": "update_lead_status", "status": "lost", "next": "stop"}, {"id": "stop", "type": "stop"}])
    assert enroll(workflow, contact).status == WorkflowRun.Status.FAILED


@pytest.mark.django_db
def test_the_lead_created_workflow_assigns_and_tells_the_assignee(account, team, contact, conversation):
    publish(account, {"type": "lead.created"}, [
        {"id": "assign", "type": "assign_conversation", "next": "tell"},
        {"id": "tell", "type": "notify_team", "to": "assignee",
         "text": "{contact} is interested, {first_name} asked about prices.", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ])
    other_conversation(account, 1, assigned_to=team["owner"])
    other_conversation(account, 2, assigned_to=team["admin"])
    other_conversation(account, 3, assigned_to=team["ada"])
    with patch(SEND) as send:
        crm.create_lead(account, contact, source="conversation", conversation_id=conversation.public_id)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["sam"]
    assert send.call_count == 1 and send.call_args.kwargs["to_email"] == "sam@example.com"
    assert "Chanda is interested, Chanda asked about prices." in send.call_args.kwargs["text_body"]
    assert f"/inbox/{conversation.public_id}/" in send.call_args.kwargs["text_body"]


@pytest.mark.django_db
def test_a_run_with_no_conversation_falls_back_to_the_customers_open_one(account, team, contact, conversation):
    workflow = publish(account, {"type": "manual"}, [
        {"id": "assign", "type": "assign_conversation", "to": "ada@example.com", "next": "stop"},
        {"id": "stop", "type": "stop"}])
    enroll(workflow, contact)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["ada"]


@pytest.mark.django_db
def test_notify_still_works_with_no_conversation_and_links_the_profile(account, team, contact):
    workflow = publish(account, {"type": "manual"}, [
        {"id": "tell", "type": "notify_team", "text": "Look at {contact}.", "next": "stop"}, {"id": "stop", "type": "stop"}])
    with patch(SEND) as send:
        run = enroll(workflow, contact)
    assert run.status == WorkflowRun.Status.COMPLETED
    assert f"/contacts/{contact.public_id}/" in send.call_args.kwargs["text_body"]


@pytest.mark.django_db
def test_set_attribute_can_store_something_from_the_run(account, contact):
    workflow = publish(account, {"type": "manual"}, [
        {"id": "a", "type": "set_attribute", "key": "interest", "value": "context.reply.title", "next": "b"},
        {"id": "b", "type": "set_attribute", "key": "plain", "value": "hello", "next": "c"},
        {"id": "c", "type": "set_attribute", "key": "missing", "value": "context.reply.nope", "next": "stop"},
        {"id": "stop", "type": "stop"}])
    from apps.automation.workflow_engine import enroll as enrol

    run = enrol(workflow, contact, context={"reply": {"id": "sofa", "title": "Sofa"}})
    contact.refresh_from_db()
    assert contact.attributes == {"interest": "Sofa", "plain": "hello", "missing": None}
    assert run.status == WorkflowRun.Status.COMPLETED


# --- validation -----------------------------------------------------------------------------------------------------


def problems(*steps, trigger="manual"):
    result = validate_definition({"trigger": {"type": trigger}, "steps": [*steps, {"id": "stop", "type": "stop"}]})
    return {(e["field"]) for e in result if e["severity"] == "error"}


def test_lifecycle_steps_are_validated_when_saved():
    assert problems({"id": "a", "type": "update_lead_status", "status": "bogus", "next": "stop"}) == {"status"}
    assert problems({"id": "a", "type": "assign_conversation", "to": "not-an-email", "next": "stop"}) == {"to"}
    assert problems({"id": "a", "type": "notify_team", "text": " ", "next": "stop"}) == {"text"}
    assert problems({"id": "a", "type": "notify_team", "text": "x", "to": "everyone", "next": "stop"}) == {"to"}
    assert problems({"id": "a", "type": "notify_team", "text": "x" * 1001, "next": "stop"}) == {"text"}
    assert problems({"id": "a", "type": "create_lead", "source": "x" * 51, "next": "stop"}) == {"source"}


def test_valid_lifecycle_steps_and_new_triggers_pass():
    assert problems(
        {"id": "a", "type": "create_lead", "next": "b"},
        {"id": "b", "type": "update_lead_status", "status": "qualified", "next": "c"},
        {"id": "c", "type": "assign_conversation", "next": "d"},
        {"id": "d", "type": "notify_team", "to": "assignee", "text": "Look", "next": "stop"},
        trigger="lead.qualified") == set()


# --- the one-click starter --------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_team_starter_installs_and_works_end_to_end(owner_client, account, team, contact, conversation):
    page = owner_client.get("/automations/").content.decode()
    assert "Hand new interested customers to your team" in page and 'name="notify_text"' in page
    resp = owner_client.post("/automations/starters/install/", {
        "starter": "route-new-leads", "assign": "on", "notify_to": "assignee",
        "notify_text": "{contact} is interested. Please reply soon."})
    assert resp.status_code == 302
    workflow = Workflow.objects.get(account=account, slug="route-new-leads")
    assert workflow.status == Workflow.Status.PUBLISHED and not validate_definition(workflow.definition)

    with patch(SEND) as send:
        crm.create_lead(account, contact, source="conversation", conversation_id=conversation.public_id)
    conversation.refresh_from_db()
    assert conversation.assigned_to == team["owner"]
    assert send.call_args.kwargs["to_email"] == "owner@example.com"


@pytest.mark.django_db
def test_the_team_starter_can_skip_assigning_and_needs_a_message(owner_client, account):
    owner_client.post("/automations/starters/install/", {
        "starter": "route-new-leads", "notify_to": "owners", "notify_text": "Look."})
    steps = Workflow.objects.get(slug="route-new-leads").definition["steps"]
    assert [s["type"] for s in steps] == ["notify_team", "stop"] and steps[0]["to"] == "owners"
    Workflow.objects.all().delete()
    owner_client.post("/automations/starters/install/", {"starter": "route-new-leads", "notify_text": " "})
    assert not Workflow.objects.exists()


@pytest.mark.django_db
def test_the_editor_offers_the_lifecycle_steps_and_lead_triggers(owner_client, account):
    workflow = Workflow.objects.create(
        account=account, name="Lifecycle", slug="lifecycle",
        definition={"trigger": {"type": "lead.qualified"}, "steps": [{"id": "stop", "type": "stop"}]})
    body = owner_client.get(f"/automations/{workflow.slug}/").content.decode()
    for needle in ("lead.qualified", "lead.status_changed", "update_lead_status", "assign_conversation",
                   "notify_team", "create_lead"):
        assert needle in body
