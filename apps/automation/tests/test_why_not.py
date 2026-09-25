"""'Why didn't it reply?': the same checks the engine applies, explained as structured reasons and plain words."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation import explain
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import explain_enrollment, enroll_for_trigger
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

NOW = timezone.now()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def keyword_wf(account, words=("price", "cost"), status=Workflow.Status.PUBLISHED, slug="pricing"):
    return Workflow.objects.create(
        account=account, name="Answer pricing questions", slug=slug, status=status, version=1,
        definition={
            "trigger": {"type": "conversation.message_received", "match": {"mode": "contains", "any": list(words)}},
            "steps": [{"id": "r", "type": "reply_text", "text": "hi", "next": "s"}, {"id": "s", "type": "stop"}],
        })


def say(account, body, *, phone="+260971000001", when=None):
    contact = Contact.objects.filter(account=account, phone=phone).first() or Contact.objects.create(
        account=account, phone=phone)
    conversation = Conversation.objects.filter(account=account, contact=contact).first() or Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp")
    message = Message.objects.create(
        account=account, conversation=conversation, direction=Message.Direction.INBOUND, body=body,
        timestamp=when or NOW)
    return contact, message


def explain_for(account, contact, message, first=True):
    return explain_enrollment(
        account.id, contact, message={"body": message.body}, message_at=message.timestamp, is_first_message=first)


def only(verdicts):
    assert len(verdicts) == 1
    return verdicts[0]


@pytest.mark.django_db
def test_a_keyword_mismatch_names_the_words_and_what_was_said(account):
    keyword_wf(account)
    contact, message = say(account, "hello")
    v = only(explain_for(account, contact, message))
    assert v["code"] == "keyword_mismatch"
    assert v["details"]["expected"] == ["price", "cost"] and v["details"]["received"] == "hello"
    text = explain.describe_verdict(v)
    assert "“price”" in text["headline"] and "hello" in text["headline"] and text["tone"] == "not_replied"


@pytest.mark.django_db
def test_an_automation_that_is_not_on_offers_to_turn_it_on(account):
    keyword_wf(account, status=Workflow.Status.DRAFT)
    contact, message = say(account, "price?")
    v = only(explain_for(account, contact, message))
    assert v["code"] == "not_on"
    assert explain.describe_verdict(v)["fix"]["url"] == "/automations/"


@pytest.mark.django_db
def test_a_matching_message_that_started_a_run_is_reported_as_ran(account):
    keyword_wf(account)
    contact, message = say(account, "what is the price?")
    enroll_for_trigger(account.id, "conversation.message_received", contact,
                       context={"message": {"body": message.body}})
    v = only(explain_for(account, contact, message))
    assert v["code"] == "ran"


@pytest.mark.django_db
def test_a_recent_answer_explains_the_cooldown(account):
    wf = keyword_wf(account)
    contact, message = say(account, "price?")
    run = WorkflowRun.objects.create(workflow=wf, contact=contact, status="completed")
    WorkflowRun.objects.filter(pk=run.pk).update(started_at=NOW - timedelta(minutes=30))
    v = only(explain_for(account, contact, message))
    assert v["code"] == "cooldown" and v["details"]["minutes"] == 60


@pytest.mark.django_db
def test_welcome_only_applies_to_a_first_message(account):
    Workflow.objects.create(
        account=account, name="Welcome", slug="welcome", status=Workflow.Status.PUBLISHED, version=1,
        definition={"trigger": {"type": "contact.created"}, "steps": [{"id": "s", "type": "stop"}]})
    contact, message = say(account, "hi again")
    assert only(explain_for(account, contact, message, first=False))["code"] == "not_new_customer"


@pytest.mark.django_db
def test_a_failed_step_is_explained_in_owner_words(account):
    wf = keyword_wf(account)
    contact, message = say(account, "price?")
    run = WorkflowRun.objects.create(workflow=wf, contact=contact, status="failed")
    run.step_runs.create(step_id="r", step_type="reply_text", status="error",
                         result={"error": "reply: the 24-hour window is closed"})
    v = only(explain_for(account, contact, message))
    text = explain.describe_verdict(v)
    assert text["tone"] == "problem" and "24-hour window" in text["detail"]


def test_error_wording_covers_the_common_causes():
    assert "opted out" in explain.humanise_step_error("customer opted out")
    assert "approved" in explain.humanise_step_error("template not approved by Meta")
    assert "no value" in explain.humanise_step_error("this customer has no value for 'name'").lower()
    assert "went wrong" in explain.humanise_step_error("boom")


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_page_explains_the_latest_message(owner, account):
    keyword_wf(account)
    say(account, "hello there")
    html = owner.get("/automations/why-not/").content.decode()
    assert "Answer pricing questions" in html and "hello there" in html and "Didn&#x27;t reply" in html


@pytest.mark.django_db
def test_the_page_never_shows_another_businesses_conversation(owner, account):
    other = Account.objects.create(company_name="Other")
    keyword_wf(other, slug="theirs")
    _, theirs = say(other, "secret words")
    html = owner.get(f"/automations/why-not/?conversation={theirs.conversation.public_id}").content.decode()
    assert "secret words" not in html


@pytest.mark.django_db
def test_the_page_has_an_empty_state(owner):
    assert b"No customer messages yet" in owner.get("/automations/why-not/").content


@pytest.mark.django_db
def test_the_conversation_shows_what_automations_did(owner, account):
    from apps.automation.api import activity_for_contact

    wf = keyword_wf(account)
    contact, message = say(account, "price?")
    run = WorkflowRun.objects.create(workflow=wf, contact=contact, status="completed")
    run.step_runs.create(step_id="r", step_type="reply_text", status="ok")
    items = activity_for_contact(account, contact)
    assert items[0]["automation"] == "Answer pricing questions"
    assert items[0]["lines"][0] == {"ok": True, "text": "Replied “hi”"}
    html = owner.get(f"/inbox/{message.conversation.public_id}/").content.decode()
    assert "What automations did" in html and "Replied" in html


@pytest.mark.django_db
def test_activity_is_scoped_to_the_business(account):
    from apps.automation.api import activity_for_contact

    other = Account.objects.create(company_name="Other")
    wf = keyword_wf(other, slug="theirs")
    contact, _ = say(other, "price?")
    WorkflowRun.objects.create(workflow=wf, contact=contact, status="completed")
    assert activity_for_contact(account, contact) == []


@pytest.mark.django_db
def test_the_list_shows_when_it_last_ran_and_who_it_helped(owner, account):
    wf = keyword_wf(account)
    a, _ = say(account, "price?", phone="+260971000010")
    b, _ = say(account, "cost?", phone="+260971000011")
    WorkflowRun.objects.create(workflow=wf, contact=a, status="completed")
    WorkflowRun.objects.create(workflow=wf, contact=b, status="failed")
    html = owner.get("/automations/").content.decode()
    assert "Last ran" in html and "Helped 2 customers" in html and "1 didn't send" in html


@pytest.mark.django_db
def test_an_automation_that_never_ran_says_so(owner, account):
    keyword_wf(account)
    assert "No customer has triggered it yet" in owner.get("/automations/").content.decode()


@pytest.mark.django_db
def test_a_failed_run_emails_the_owners_once_a_day(account, monkeypatch):
    from django.core.cache import cache

    from apps.automation import workflow_engine as we
    from apps.accounts import notifications

    cache.clear()
    user = User.objects.create_user("own", "own@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    sent = []
    monkeypatch.setattr(notifications, "notify_team", lambda *a, **k: sent.append(k) or 1)
    wf = keyword_wf(account)
    contact, _ = say(account, "price?")
    for _ in range(2):
        run = WorkflowRun.objects.create(workflow=wf, contact=contact, status="failed")
        we._alert_failure(run, "reply: the 24-hour window is closed")
    assert len(sent) == 1
    assert "24-hour window" in sent[0]["text"] and sent[0]["to"] == "owners"


# ---- pause / resume, the goal gallery, readiness, and "what does this do?" ----
@pytest.mark.django_db
def test_pausing_changes_only_the_on_off_state(owner, account):
    wf = keyword_wf(account)
    before = (wf.version, wf.definition)
    owner.post(f"/automations/{wf.slug}/pause/")
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.ARCHIVED
    owner.post(f"/automations/{wf.slug}/resume/")
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.PUBLISHED
    assert (wf.version, wf.definition) == before, "pause and resume must not create a new version"


@pytest.mark.django_db
def test_a_paused_automation_does_not_start_or_keep_sending(owner, account):
    from apps.automation.workflow_engine import run_due

    wf = keyword_wf(account)
    contact, _ = say(account, "price?")
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status="waiting", current_step="r", next_due_at=NOW - timedelta(minutes=1))
    owner.post(f"/automations/{wf.slug}/pause/")
    assert run_due() == 0, "a paused automation must not send what it had waiting"
    assert enroll_for_trigger(account.id, "conversation.message_received", contact,
                              context={"message": {"body": "price"}}) == 0
    run.refresh_from_db()
    assert run.status == "waiting"


@pytest.mark.django_db
def test_pause_and_resume_are_scoped_to_the_business(owner, account):
    other = Account.objects.create(company_name="Other")
    theirs = keyword_wf(other, slug="theirs")
    assert owner.post(f"/automations/{theirs.slug}/pause/").status_code == 404
    theirs.refresh_from_db()
    assert theirs.status == Workflow.Status.PUBLISHED


@pytest.mark.django_db
def test_the_home_is_a_goal_gallery_with_running_now(owner, account):
    keyword_wf(account)
    html = owner.get("/automations/").content.decode()
    for heading in ("What do you want Akilent to help with?", "Answer customer questions",
                    "Never miss a customer", "Keep your team informed", "Running now"):
        assert heading in html
    assert "Answer pricing questions" in html and "Pause" in html and "What does this do?" in html


@pytest.mark.django_db
def test_a_paused_card_says_it_will_not_run(owner, account):
    wf = keyword_wf(account)
    owner.post(f"/automations/{wf.slug}/pause/")
    html = owner.get("/automations/").content.decode()
    assert "Paused" in html and "won't run this automation until you turn it on again" in html and "Turn on" in html


@pytest.mark.django_db
def test_readiness_separates_blocked_from_never_used(account):
    from apps.automation import readiness
    from apps.automation.engagement_starters import STARTERS_BY_KEY

    starter = STARTERS_BY_KEY["quiet-customer-check-in"]  # needs an approved template
    blocked = readiness.check(account, starter, approved_template_count=0)
    assert blocked["ready"] is False
    assert any(i["blocking"] and i["fix"] for i in blocked["items"])
    reply = readiness.check(account, STARTERS_BY_KEY["answer-pricing-questions"], approved_template_count=0)
    assert not any("approved" in i["headline"] for i in reply["items"]), "a plain reply needs no template"


@pytest.mark.django_db
def test_hours_are_advice_not_a_blocker(account):
    from apps.automation import readiness
    from apps.automation.engagement_starters import STARTERS_BY_KEY

    result = readiness.check(account, STARTERS_BY_KEY["reply-when-closed"], approved_template_count=0)
    hours = next(i for i in result["items"] if "hours" in i["headline"])
    assert hours["ok"] is False and hours["blocking"] is False


def test_what_does_this_do_reads_the_stored_definition():
    from apps.automation.explain import explain_definition

    out = explain_definition({
        "trigger": {"type": "conversation.message_received", "match": {"mode": "contains", "any": ["price"]},
                    "cooldown_minutes": 60},
        "steps": [{"id": "r", "type": "reply_text", "text": "Hello there", "next": "t"},
                  {"id": "t", "type": "add_tag", "tag": "asked-prices", "next": "s"},
                  {"id": "s", "type": "stop"}],
    })
    assert "mentions" in out["when"] and "price" in out["when"]
    assert out["does"] == ["sends this reply: “Hello there”", "tags them “asked-prices”"]
    assert any("paused" in w for w in out["wont"]) and any("opted out" in w for w in out["wont"])
    assert any("24-hour" in w for w in out["wont"])


@pytest.mark.django_db
def test_every_starter_belongs_to_a_goal_and_installs_still_work(owner):
    from apps.automation.engagement_starters import GOAL_GROUPS, STARTERS_BY_KEY

    assert {k for _, keys in GOAL_GROUPS for k in keys} == set(STARTERS_BY_KEY)
    owner.post("/automations/starters/install/", {"starter": "greet-hello", "reply_text": "Hi"})
    assert Workflow.objects.filter(slug="greet-hello", status=Workflow.Status.PUBLISHED).exists()


# ---- the recipe screen: own words, then-options, preview and test ----
@pytest.mark.django_db
def test_the_owner_can_change_the_words_it_listens_for(owner, account):
    owner.post("/automations/starters/install/", {
        "starter": "answer-pricing-questions", "reply_text": "K500", "keywords": "cost,  how much , COST\nrates",
        "then_present": "1", "add_tag": "on"})
    definition = Workflow.objects.get(account=account, slug="answer-pricing-questions").definition
    assert definition["trigger"]["match"]["any"] == ["cost", "how much", "rates"]
    assert [s["type"] for s in definition["steps"]] == ["reply_text", "add_tag", "stop"]


@pytest.mark.django_db
def test_an_empty_word_list_is_refused(owner, account):
    owner.post("/automations/starters/install/", {
        "starter": "answer-pricing-questions", "reply_text": "K500", "keywords": " , "})
    assert not Workflow.objects.filter(account=account, slug="answer-pricing-questions").exists()


@pytest.mark.django_db
def test_the_then_options_control_the_tag_and_the_team_email(owner, account):
    owner.post("/automations/starters/install/", {
        "starter": "answer-pricing-questions", "reply_text": "K500", "then_present": "1", "tell_team": "on"})
    steps = Workflow.objects.get(account=account, slug="answer-pricing-questions").definition["steps"]
    assert [s["type"] for s in steps] == ["reply_text", "notify_team", "stop"], "tag unticked, team ticked"
    assert steps[1]["to"] == "owners"
    from apps.automation.workflow_engine import validate_definition
    assert not [e for e in validate_definition({"trigger": {"type": "conversation.message_received",
                "match": {"mode": "contains", "any": ["x"]}}, "steps": steps}, account=account)
                if e.get("severity", "error") != "warning"]


@pytest.mark.django_db
def test_an_old_form_without_the_then_options_still_tags(owner, account):
    owner.post("/automations/starters/install/", {"starter": "answer-pricing-questions", "reply_text": "K500"})
    types = [s["type"] for s in Workflow.objects.get(account=account, slug="answer-pricing-questions").definition["steps"]]
    assert types == ["reply_text", "add_tag", "stop"]


@pytest.mark.django_db
def test_the_setup_screen_reads_as_when_do_then_with_a_live_preview(owner):
    html = owner.get("/automations/").content.decode()
    for text in ("When", "Do", "Then", "Preview", "To Ada Mwape", "Send test to me", "Email my team when this happens"):
        assert text in html


def _owner_chat(account, *, window_open, phone="+260971234567"):
    from apps.whatsapp.models import Conversation as WaConversation, WhatsAppContact

    wa = WhatsAppContact.objects.create(account=account, phone_number=phone)
    WaConversation.objects.create(
        account=account, contact=wa, is_open=True,
        window_expires_at=timezone.now() + timedelta(hours=5 if window_open else -5))
    return wa


@pytest.mark.django_db
def test_send_test_goes_to_the_owners_own_number_with_the_name_filled_in(owner, account, monkeypatch):
    from apps.whatsapp import api as whatsapp_api

    _owner_chat(account, window_open=True)
    sent = []
    monkeypatch.setattr(whatsapp_api, "send_message", lambda acct, contact, text, *a, **k: sent.append((contact, text)))
    response = owner.post("/automations/starters/test/", {
        "starter": "greet-hello", "reply_text": "Hi {first_name}!", "test_phone": "260 97 1234567"}, follow=True)
    assert len(sent) == 1 and sent[0][1] == "Hi there!"   # the owner's contact has no name, so the fallback shows
    assert "Test sent to your WhatsApp" in response.content.decode()


@pytest.mark.django_db
def test_send_test_explains_why_it_cannot_send_when_the_window_is_closed(owner, account, monkeypatch):
    from apps.whatsapp import api as whatsapp_api

    _owner_chat(account, window_open=False)
    sent = []
    monkeypatch.setattr(whatsapp_api, "send_message", lambda *a, **k: sent.append(1))
    response = owner.post("/automations/starters/test/", {
        "starter": "greet-hello", "reply_text": "Hi", "test_phone": "+260971234567"}, follow=True)
    assert not sent and "in the last 24 hours" in response.content.decode()


@pytest.mark.django_db
def test_send_test_never_creates_a_contact_or_messages_a_stranger(owner, account, monkeypatch):
    from apps.whatsapp import api as whatsapp_api
    from apps.whatsapp.models import WhatsAppContact

    sent = []
    monkeypatch.setattr(whatsapp_api, "send_message", lambda *a, **k: sent.append(1))
    owner.post("/automations/starters/test/", {"starter": "greet-hello", "reply_text": "Hi", "test_phone": "+260999999999"})
    assert not sent and not WhatsAppContact.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_send_test_will_not_use_another_businesses_contact(owner, account, monkeypatch):
    from apps.whatsapp import api as whatsapp_api

    _owner_chat(Account.objects.create(company_name="Other"), window_open=True)
    sent = []
    monkeypatch.setattr(whatsapp_api, "send_message", lambda *a, **k: sent.append(1))
    owner.post("/automations/starters/test/", {"starter": "greet-hello", "reply_text": "Hi", "test_phone": "+260971234567"})
    assert not sent


# ---- WhatsApp first ----
@pytest.mark.django_db
def test_a_new_automation_starts_from_a_whatsapp_moment(owner, account):
    owner.post("/automations/create/", {"name": "Mine"})
    wf = Workflow.objects.get(account=account, name="Mine")
    assert wf.definition["trigger"]["type"] == "conversation.message_received"


def test_the_editor_offers_whatsapp_first_and_email_later():
    from apps.automation.views import _STEP_TYPES, _TRIGGER_TYPES

    assert _STEP_TYPES[0] == "reply_text"
    assert _STEP_TYPES.index("reply_text") < _STEP_TYPES.index("send_email")
    assert _STEP_TYPES.index("send_whatsapp") < _STEP_TYPES.index("send_email")
    assert _TRIGGER_TYPES[0] == "conversation.message_received"
    assert _TRIGGER_TYPES.index("conversation.message_received") < _TRIGGER_TYPES.index("email.opened")


@pytest.mark.django_db
def test_add_step_in_the_editor_defaults_to_a_whatsapp_reply(owner, account):
    wf = keyword_wf(account)
    html = owner.get(f"/automations/{wf.slug}/").content.decode()
    assert 'type: "reply_text"' in html and 'type: "send_email", _mode: "write"' not in html


@pytest.mark.django_db
def test_email_only_starters_are_labelled_as_email(owner):
    html = owner.get("/automations/").content.decode()
    assert "Start from scratch (WhatsApp)" in html and "Email: Welcome series" in html


@pytest.mark.django_db
def test_the_page_names_the_site_wide_switch_when_it_is_off(owner, account, monkeypatch):
    from apps.whatsapp import api as whatsapp_api

    keyword_wf(account)
    say(account, "What is the price for the solar installation package?")
    monkeypatch.setattr(whatsapp_api, "automations_start_from_messages", lambda: False)
    html = owner.get("/automations/why-not/").content.decode()
    assert "switched off for the whole site" in html


@pytest.mark.django_db
def test_no_site_note_when_the_switch_is_on(owner, account, monkeypatch):
    from apps.whatsapp import api as whatsapp_api

    keyword_wf(account)
    say(account, "price?")
    monkeypatch.setattr(whatsapp_api, "automations_start_from_messages", lambda: True)
    assert "switched off for the whole site" not in owner.get("/automations/why-not/").content.decode()
