"""One-click engagement follow-ups.

The point of a starter is that it is *on* afterwards. These cover the two
things that would quietly make it useless: installing something that can't
send, and chasing a customer who has already replied.
"""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation.engagement_starters import STARTERS_BY_KEY, build_definition
from apps.automation.models import Workflow
from apps.automation.workflow_engine import validate_definition
from apps.whatsapp.models import MessageTemplate

LIST_URL = "/automations/"
INSTALL_URL = "/automations/starters/install/"


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


@pytest.fixture
def approved_template(logged_in):
    _, account = logged_in
    return MessageTemplate.objects.create(
        account=account, name="Checking in", whatsapp_template_name="checking_in",
        content="Hi {{1}}, just checking in about {{2}}.",
        variables=["1", "2"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


@pytest.mark.django_db
def test_a_starter_installs_published_and_running(logged_in, approved_template):
    client, account = logged_in
    resp = client.post(INSTALL_URL, {
        "starter": "quiet-customer-check-in",
        "template_id": approved_template.pk,
        f"var__{approved_template.pk}__1": "there",
        f"var__{approved_template.pk}__2": "your enquiry",
    })
    assert resp.status_code == 302

    workflow = Workflow.objects.get(account=account, slug="quiet-customer-check-in")
    assert workflow.status == Workflow.Status.PUBLISHED, "a starter left as a draft does nothing"
    assert not validate_definition(workflow.definition, account=account)


@pytest.mark.django_db
def test_installing_twice_updates_rather_than_duplicates(logged_in, approved_template):
    """Two workflows chasing the same customers would double every message."""
    client, account = logged_in
    payload = {
        "starter": "quiet-customer-check-in",
        "template_id": approved_template.pk,
        f"var__{approved_template.pk}__1": "there",
        f"var__{approved_template.pk}__2": "your enquiry",
    }
    client.post(INSTALL_URL, payload)
    client.post(INSTALL_URL, payload)
    assert Workflow.objects.filter(account=account, slug="quiet-customer-check-in").count() == 1


@pytest.mark.django_db
def test_an_unapproved_template_cannot_be_installed(logged_in):
    """WhatsApp only delivers approved templates, so installing this would
    produce a workflow that fails every time it runs."""
    client, account = logged_in
    draft = MessageTemplate.objects.create(
        account=account, name="Draft", whatsapp_template_name="draft_one",
        content="Hi", variables=[],
        approval_status=MessageTemplate.ApprovalStatus.PENDING,
    )
    resp = client.post(INSTALL_URL, {"starter": "quiet-customer-check-in", "template_id": draft.pk})
    assert resp.status_code == 302
    assert not Workflow.objects.filter(account=account, slug="quiet-customer-check-in").exists()


@pytest.mark.django_db
def test_another_accounts_template_cannot_be_used(logged_in):
    client, _ = logged_in
    other = Account.objects.create(company_name="Someone Else")
    theirs = MessageTemplate.objects.create(
        account=other, name="Theirs", whatsapp_template_name="theirs",
        content="Hi", variables=[],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    resp = client.post(INSTALL_URL, {"starter": "quiet-customer-check-in", "template_id": theirs.pk})
    assert resp.status_code == 302
    assert not Workflow.objects.filter(slug="quiet-customer-check-in").exists()


@pytest.mark.django_db
def test_the_gallery_explains_itself_when_nothing_is_approved(logged_in):
    """No approved message means nothing can be sent. Say so, rather than
    offering a button that installs something broken."""
    client, _ = logged_in
    body = client.get(LIST_URL).content.decode()
    assert "Check in when a customer goes quiet" in body
    assert "Needs a message WhatsApp has approved" in body
    # No template picker is offered: the follow-ups that need a template can't be installed.
    assert 'name="template_id"' not in body


@pytest.mark.django_db
def test_the_gallery_shows_a_starter_that_is_already_on(logged_in, approved_template):
    client, _ = logged_in
    client.post(INSTALL_URL, {
        "starter": "quiet-customer-check-in",
        "template_id": approved_template.pk,
        f"var__{approved_template.pk}__1": "there",
        f"var__{approved_template.pk}__2": "your enquiry",
    })
    body = client.get(LIST_URL).content.decode()
    assert "See what it does" in body


@pytest.mark.parametrize("key", [k for k, s in STARTERS_BY_KEY.items() if "quiet_days" in s])
def test_every_starter_waits_then_checks_the_customer_is_still_quiet(key):
    """The branch is what makes these safe to turn on — without it they would
    message customers who are mid-conversation."""
    definition = build_definition(STARTERS_BY_KEY[key], template_name="anything")
    steps = {s["id"]: s for s in definition["steps"]}
    assert steps["wait"]["type"] == "wait"
    assert steps["still_quiet"]["field"] == "last_engaged_days"
    assert steps["still_quiet"]["on_false"] == "stop"
    assert steps["still_quiet"]["value"] == STARTERS_BY_KEY[key]["quiet_days"]


# --- end to end through the real engine --------------------------------------
#
# The shape tests above only prove the definition looks right. What an owner is
# actually relying on is behaviour: a quiet customer is checked in on, and one
# who has replied is not.

from datetime import timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from apps.automation.models import WorkflowRun  # noqa: E402
from apps.automation.workflow_engine import enroll, run_due  # noqa: E402
from apps.contacts.models import Contact  # noqa: E402
from apps.contacts.services import record_contact_event  # noqa: E402


def _sent_step_ids(run):
    return list(run.step_runs.values_list("step_id", flat=True))


@pytest.fixture
def running_check_in(logged_in, approved_template):
    """The quiet-customer starter, published and ready to run."""
    _, account = logged_in
    from apps.automation import api as automation_api

    definition = build_definition(
        STARTERS_BY_KEY["quiet-customer-check-in"],
        template_name=approved_template.whatsapp_template_name,
        variable_mapping={"1": "there", "2": "your enquiry"},
    )
    return automation_api.upsert_published_workflow(
        account, slug="quiet-customer-check-in", name="Check in", definition=definition,
    )


def _enroll_and_let_the_wait_elapse(workflow, contact):
    run = enroll(workflow, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    WorkflowRun.objects.filter(pk=run.pk).update(next_due_at=timezone.now() - timedelta(seconds=1))
    return run


@pytest.mark.django_db
def test_a_customer_who_replied_is_not_chased(logged_in, running_check_in):
    """The promise on the card: "Nothing is sent if the customer writes back."
    A reply stamps last_engaged_at, the branch sees it, and the run ends
    without ever reaching the send step."""
    _, account = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    run = _enroll_and_let_the_wait_elapse(running_check_in, contact)

    # The customer writes back while the wait is running.
    record_contact_event(contact, "conversation.message_received", data={"body": "Yes please"})

    run_due()
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert "send" not in _sent_step_ids(run)


@pytest.mark.django_db
def test_a_customer_who_stayed_quiet_reaches_the_send_step(logged_in, running_check_in):
    """The counterpart: with no reply and their last contact long ago, the
    branch says "still quiet" and the run goes on to send. (Delivery itself is
    the WhatsApp pipeline's job and is covered there; what matters here is that
    the check-in is attempted.)"""
    _, account = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    Contact.objects.filter(pk=contact.pk).update(
        last_engaged_at=timezone.now() - timedelta(days=10)
    )
    run = _enroll_and_let_the_wait_elapse(running_check_in, contact)

    run_due()
    run.refresh_from_db()
    assert "send" in _sent_step_ids(run)


# --- welcome starter -----------------------------------------------------------


def _welcome_workflow(account):
    from apps.automation import api as automation_api

    MessageTemplate.objects.create(
        account=account, name="Welcome", whatsapp_template_name="welcome_new_customer",
        content="Hi {{1}}, thanks for contacting {{2}}.", variables=["1", "2"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )

    definition = build_definition(
        STARTERS_BY_KEY["welcome-new-enquiry"], template_name="welcome_new_customer",
        variable_mapping={"1": "there", "2": "Mwamba Kitchen"},
    )
    return automation_api.upsert_published_workflow(
        account, slug="welcome-new-enquiry", name="Welcome", definition=definition)


@pytest.mark.django_db
def test_welcome_definition_is_valid_and_only_for_customers_who_messaged_first(logged_in):
    _, account = logged_in
    workflow = _welcome_workflow(account)
    assert not validate_definition(workflow.definition, account=account)
    assert workflow.definition["trigger"]["type"] == "contact.created"
    steps = {s["id"]: s for s in workflow.definition["steps"]}
    assert steps["messaged_first"]["field"] == "source" and steps["messaged_first"]["value"] == "whatsapp"
    assert steps["messaged_first"]["on_false"] == "stop"


@pytest.mark.django_db
def test_welcome_is_sent_to_a_whatsapp_enquirer_but_not_an_imported_contact(logged_in):
    _, account = logged_in
    workflow = _welcome_workflow(account)
    enquirer = Contact.objects.create(account=account, phone="+260971111111", source="whatsapp")
    imported = Contact.objects.create(account=account, phone="+260972222222", source="import")
    assert "send" in _sent_step_ids(enroll(workflow, enquirer))
    assert "send" not in _sent_step_ids(enroll(workflow, imported))


# --- keyword reply starters -------------------------------------------------------


@pytest.mark.django_db
def test_a_keyword_reply_installs_without_any_approved_template(logged_in):
    """These reply inside the 24h window, so no Meta-approved template is involved."""
    client, account = logged_in
    body = client.get(LIST_URL).content.decode()
    assert "Answer pricing questions" in body and 'name="reply_text"' in body

    resp = client.post(INSTALL_URL, {"starter": "answer-pricing-questions", "reply_text": "Prices start at K50."})
    assert resp.status_code == 302
    workflow = Workflow.objects.get(account=account, slug="answer-pricing-questions")
    assert workflow.status == Workflow.Status.PUBLISHED
    assert not validate_definition(workflow.definition, account=account)
    trigger = workflow.definition["trigger"]
    assert trigger["type"] == "conversation.message_received"
    assert "price" in trigger["match"]["any"] and trigger["cooldown_minutes"] == 60
    assert workflow.definition["steps"][0]["text"] == "Prices start at K50."


@pytest.mark.django_db
@pytest.mark.parametrize("text", ["", "   ", "x" * 1001])
def test_a_keyword_reply_needs_sensible_text(logged_in, text):
    client, account = logged_in
    client.post(INSTALL_URL, {"starter": "answer-pricing-questions", "reply_text": text})
    assert not Workflow.objects.filter(account=account, slug="answer-pricing-questions").exists()


@pytest.mark.django_db
def test_an_installed_keyword_reply_shows_as_on_and_updates_in_place(logged_in):
    client, account = logged_in
    for text in ("First answer", "Second answer"):
        client.post(INSTALL_URL, {"starter": "greet-hello", "reply_text": text})
    assert Workflow.objects.filter(account=account, slug="greet-hello").count() == 1
    assert "See what it does" in client.get(LIST_URL).content.decode()


@pytest.mark.django_db
def test_an_installed_keyword_reply_answers_end_to_end(logged_in):
    """Through the real trigger path: a matching message gets one reply, another does not."""
    from apps.conversations.models import Conversation
    from apps.whatsapp.models import Conversation as WhatsAppConversation
    from apps.whatsapp.models import OutboundMessage, WhatsAppContact

    client, account = logged_in
    client.post(INSTALL_URL, {"starter": "answer-where-are-you", "reply_text": "Cairo Road, Lusaka."})
    contact = Contact.objects.create(account=account, phone="+260971234567", source="whatsapp")
    wa = WhatsAppContact.objects.create(account=account, phone_number="+260971234567", contact=contact)
    conversation = Conversation.get_or_create_for_whatsapp(WhatsAppConversation.get_or_open(wa))

    from apps.automation.workflow_engine import enroll_for_trigger

    def customer_says(body):
        return enroll_for_trigger(account.id, "conversation.message_received", contact, context={
            "conversation_id": conversation.public_id, "message": {"body": body, "type": "text"}})

    assert customer_says("Hello, where are you located?") == 1
    assert customer_says("What time do you open?") == 0
    assert [m.payload["body"] for m in OutboundMessage.objects.all()] == ["Cairo Road, Lusaka."]
