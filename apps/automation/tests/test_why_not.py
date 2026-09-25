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
    assert "Last ran" in html and "2 customers helped" in html and "1 didn't send" in html


@pytest.mark.django_db
def test_an_automation_that_never_ran_says_so(owner, account):
    keyword_wf(account)
    assert "Hasn&#x27;t run yet" in owner.get("/automations/").content.decode() or \
        "Hasn't run yet" in owner.get("/automations/").content.decode()


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
