"""Tags: a deterministic way to say what a customer is about.

Service rules (case-folding, idempotence, limits), then everything that reads or writes
them: segments and workflow branches, the action registry, the workflow steps, the screens,
and the keyword starters that tag as they answer.
"""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation import api as automation_api
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import enroll, validate_definition
from apps.contacts import tags
from apps.contacts.models import Contact, Tag
from apps.contacts.segments import SegmentError, contacts_for
from apps.core.actions import ActionError, run_action


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567", first_name="Ada", source="whatsapp")


def names(contact):
    return sorted(contact.tags.values_list("name", flat=True))


# --- the service ------------------------------------------------------------------------


@pytest.mark.django_db
def test_add_creates_the_tag_and_labels_the_customer(contact):
    assert tags.add_tag(contact, "Pricing Enquiry") is True
    assert names(contact) == ["Pricing Enquiry"]


@pytest.mark.django_db
def test_names_are_one_tag_whatever_the_case_or_spacing(contact):
    tags.add_tag(contact, "VIP")
    assert tags.add_tag(contact, "  vip ") is False
    assert Tag.objects.filter(account=contact.account).count() == 1
    assert names(contact) == ["VIP"]                # shown as first written


@pytest.mark.django_db
def test_add_and_remove_are_quiet_no_ops_when_repeated(contact):
    tags.add_tag(contact, "vip")
    assert tags.add_tag(contact, "vip") is False
    assert tags.remove_tag(contact, "VIP") is True
    assert tags.remove_tag(contact, "vip") is False
    assert tags.remove_tag(contact, "never-had-it") is False
    assert names(contact) == []


@pytest.mark.django_db
def test_the_same_tag_in_two_businesses_is_two_tags(contact):
    other = Account.objects.create(company_name="Other")
    other_contact = Contact.objects.create(account=other, phone="+260979999999")
    tags.add_tag(contact, "vip")
    tags.add_tag(other_contact, "vip")
    assert Tag.objects.filter(slug="vip").count() == 2
    assert names(other_contact) == ["vip"]


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["", "   ", "!!!", "x" * 41])
def test_unusable_names_are_refused_in_plain_words(contact, bad):
    with pytest.raises(tags.TagError):
        tags.add_tag(contact, bad)
    assert names(contact) == []


@pytest.mark.django_db
def test_limits_protect_the_account(contact, monkeypatch):
    monkeypatch.setattr(tags, "MAX_TAGS_PER_CONTACT", 2)
    tags.add_tag(contact, "a")
    tags.add_tag(contact, "b")
    with pytest.raises(tags.TagError):
        tags.add_tag(contact, "c")
    monkeypatch.setattr(tags, "MAX_TAGS_PER_ACCOUNT", 2)
    other = Contact.objects.create(account=contact.account, phone="+260972222222")
    with pytest.raises(tags.TagError):
        tags.add_tag(other, "brand-new")
    assert tags.add_tag(other, "a") is True         # an existing tag is still fine


@pytest.mark.django_db
def test_changes_appear_on_the_customers_timeline(contact):
    from apps.contacts.event_labels import event_detail, event_label

    tags.add_tag(contact, "vip")
    tags.remove_tag(contact, "vip")
    added, removed = contact.events.order_by("id")
    assert (event_label(added.type), event_detail(added.type, added.data)) == ("Tag added", "vip")
    assert event_label(removed.type) == "Tag removed"
    assert not contact.events.filter(type="tag.added").exclude(pk=added.pk).exists()


@pytest.mark.django_db
def test_a_repeated_add_leaves_no_second_timeline_entry(contact):
    tags.add_tag(contact, "vip")
    tags.add_tag(contact, "vip")
    assert contact.events.filter(type="tag.added").count() == 1


# --- segments and branches read tags back -------------------------------------------------


@pytest.mark.django_db
def test_segment_conditions_on_tags(account):
    vip = Contact.objects.create(account=account, phone="+260971111111")
    both = Contact.objects.create(account=account, phone="+260972222222")
    plain = Contact.objects.create(account=account, phone="+260973333333")
    tags.add_tag(vip, "VIP")
    tags.add_tag(both, "vip")
    tags.add_tag(both, "pricing-enquiry")

    def found(**cond):
        return set(contacts_for({"op": "and", "conditions": [{"field": "tag", **cond}]}, account))

    assert found(operator="eq", value="Vip") == {vip, both}
    assert found(operator="ne", value="vip") == {plain}
    assert found(operator="in", value=["pricing-enquiry", "nothing"]) == {both}
    assert found(operator="exists") == {vip, both}
    assert found(operator="not_exists") == {plain}


@pytest.mark.django_db
@pytest.mark.parametrize("cond", [
    {"operator": "eq", "value": ""}, {"operator": "in", "value": []},
    {"operator": "gt", "value": "vip"}, {"operator": "eq", "value": 5},
])
def test_bad_tag_conditions_are_rejected(account, cond):
    with pytest.raises(SegmentError):
        contacts_for({"op": "and", "conditions": [{"field": "tag", **cond}]}, account)


# --- the action registry ---------------------------------------------------------------------


@pytest.mark.django_db
def test_actions_add_and_remove(contact):
    ctx = {"account": contact.account}
    assert run_action("add_tag", ctx, contact=contact, tag="vip")["changed"] is True
    assert run_action("add_tag", ctx, contact=contact, tag="vip")["changed"] is False
    assert run_action("remove_tag", ctx, contact=contact, tag="vip")["changed"] is True


@pytest.mark.django_db
def test_an_action_cannot_tag_another_accounts_customer(contact):
    other = Account.objects.create(company_name="Other")
    with pytest.raises(ActionError):
        run_action("add_tag", {"account": other}, contact=contact, tag="vip")
    assert names(contact) == []


@pytest.mark.django_db
def test_an_action_reports_a_bad_name_as_an_action_error(contact):
    with pytest.raises(ActionError):
        run_action("add_tag", {}, contact=contact, tag="")


# --- workflow steps ----------------------------------------------------------------------------


def publish(account, steps, trigger=None, slug="tagger"):
    return automation_api.upsert_published_workflow(
        account, slug=slug, name=slug,
        definition={"trigger": trigger or {"type": "manual"}, "steps": steps})


@pytest.mark.django_db
def test_workflow_add_and_remove_tag_steps(account, contact):
    workflow = publish(account, [
        {"id": "a", "type": "add_tag", "tag": "hot", "next": "b"},
        {"id": "b", "type": "add_tag", "tag": "cold", "next": "c"},
        {"id": "c", "type": "remove_tag", "tag": "cold", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ])
    run = enroll(workflow, contact)
    assert run.status == WorkflowRun.Status.COMPLETED
    assert names(contact) == ["hot"]


@pytest.mark.django_db
def test_a_workflow_can_branch_on_a_tag_and_not_tag_twice(account, contact):
    """Tag once; later runs see the tag and skip the welcome."""
    steps = [
        {"id": "seen", "type": "branch", "field": "tag", "operator": "eq", "value": "greeted",
         "on_true": "stop", "on_false": "mark"},
        {"id": "mark", "type": "add_tag", "tag": "greeted", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ]
    workflow = publish(account, steps)
    first = enroll(workflow, contact)
    second = enroll(workflow, contact)
    assert "mark" in list(first.step_runs.values_list("step_id", flat=True))
    assert "mark" not in list(second.step_runs.values_list("step_id", flat=True))
    assert contact.events.filter(type="tag.added").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("step_type", ["add_tag", "remove_tag"])
def test_tag_steps_need_a_valid_tag(step_type):
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "t", "type": step_type, "tag": "  ", "next": "stop"}, {"id": "stop", "type": "stop"}],
    })
    assert [e["field"] for e in errors if e["severity"] == "error"] == ["tag"]


# --- the screens ----------------------------------------------------------------------------------


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_add_and_remove_from_the_customer_profile(owner, contact):
    resp = owner.post(f"/contacts/{contact.public_id}/tags/add/", {"tag": "VIP"})
    assert resp.status_code == 302 and resp.url == f"/contacts/{contact.public_id}/"
    assert names(contact) == ["VIP"]
    assert "VIP" in owner.get(f"/contacts/{contact.public_id}/").content.decode()
    owner.post(f"/contacts/{contact.public_id}/tags/remove/", {"tag": "vip"})
    assert names(contact) == []


@pytest.mark.django_db
def test_a_bad_tag_name_shows_a_message_and_changes_nothing(owner, contact):
    resp = owner.post(f"/contacts/{contact.public_id}/tags/add/", {"tag": ""}, follow=True)
    assert "Type a tag name." in resp.content.decode()
    assert names(contact) == []


@pytest.mark.django_db
def test_cannot_tag_another_businesses_customer(owner):
    other = Account.objects.create(company_name="Other")
    theirs = Contact.objects.create(account=other, phone="+260979999999")
    assert owner.post(f"/contacts/{theirs.public_id}/tags/add/", {"tag": "vip"}).status_code == 404
    assert names(theirs) == []


@pytest.mark.django_db
def test_tag_endpoints_only_accept_post(owner, contact):
    assert owner.get(f"/contacts/{contact.public_id}/tags/add/").status_code == 405


@pytest.mark.django_db
def test_next_returns_to_the_page_tagging_started_on_but_never_off_site(owner, contact):
    resp = owner.post(f"/contacts/{contact.public_id}/tags/add/", {"tag": "a", "next": "/inbox/"})
    assert resp.url == "/inbox/"
    resp = owner.post(f"/contacts/{contact.public_id}/tags/add/", {"tag": "b", "next": "https://evil.example/x"})
    assert resp.url == f"/contacts/{contact.public_id}/"


@pytest.mark.django_db
def test_the_contact_list_shows_and_filters_by_tag(owner, account, contact):
    other = Contact.objects.create(account=account, phone="+260972222222")
    tags.add_tag(contact, "VIP")
    body = owner.get("/contacts/").content.decode()
    assert "VIP" in body and 'name="tag"' in body
    filtered = owner.get("/contacts/?tag=vip").content.decode()
    assert contact.phone in filtered and other.phone not in filtered
    empty = owner.get("/contacts/?tag=nothing").content.decode()
    assert "No contacts match those filters" in empty


@pytest.mark.django_db
def test_the_inbox_side_panel_shows_tags_and_lets_you_tag(owner, account, contact):
    from apps.conversations.models import Conversation

    conversation = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    tags.add_tag(contact, "VIP")
    page = f"/inbox/{conversation.public_id}/"
    body = owner.get(page).content.decode()
    assert "VIP" in body and f"/contacts/{contact.public_id}/tags/add/" in body
    resp = owner.post(f"/contacts/{contact.public_id}/tags/add/", {"tag": "asked-price", "next": page})
    assert resp.url == page and "asked-price" in names(contact)


# --- keyword starters tag as they answer ---------------------------------------------------------------


@pytest.mark.django_db
def test_the_pricing_starter_answers_and_tags_the_customer(owner, account, contact):
    from apps.automation.workflow_engine import enroll_for_trigger
    from apps.conversations.models import Conversation
    from apps.whatsapp.models import Conversation as WhatsAppConversation
    from apps.whatsapp.models import OutboundMessage, WhatsAppContact

    owner.post("/automations/starters/install/",
               {"starter": "answer-pricing-questions", "reply_text": "Prices start at K50."})
    workflow = Workflow.objects.get(account=account, slug="answer-pricing-questions")
    assert not validate_definition(workflow.definition, account=account)

    wa = WhatsAppContact.objects.create(account=account, phone_number=contact.phone, contact=contact)
    conversation = Conversation.get_or_create_for_whatsapp(WhatsAppConversation.get_or_open(wa))
    enroll_for_trigger(account.id, "conversation.message_received", contact, context={
        "conversation_id": conversation.public_id, "message": {"body": "How much is it?", "type": "text"}})

    assert [m.payload["body"] for m in OutboundMessage.objects.all()] == ["Prices start at K50."]
    assert names(contact) == ["pricing-enquiry"]
