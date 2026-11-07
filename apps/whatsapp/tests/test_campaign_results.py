"""Campaigns plan, Phase A: a finished campaign says what happened, not that it was queued.

- the badge and the history line come from delivery: Failed, Partly sent or Sent;
- failures read in plain words, grouped, with where to fix an account problem;
- Retry gives way to the fix while the account's own problem is unfixed;
- the composer warns about that problem, and Send needs the owner to confirm it.
"""

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact, ContactList
from apps.whatsapp import campaign_ui
from apps.whatsapp.health import account_problem
from apps.whatsapp.models import (
    MessageTemplate,
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)

pytestmark = pytest.mark.django_db
_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled")
BILLING = "131042"
TIMES = chr(0xD7)  # the groups' "2 × reason"
RAW = "131042: Business eligibility payment issue (Message failed to send because"


@pytest.fixture
def shop(client):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    template = MessageTemplate.objects.create(
        account=account,
        name="download_app",
        whatsapp_template_name="download_app",
        content="Get our app",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    clist = ContactList.objects.create(account=account, name="VIPs")
    return client, account, template, clist


def _campaign(account, template, clist, outcomes):
    """A finished campaign with one recipient per outcome: "failed" (billing), "bad_number"
    (a message-level failure), "sent", "delivered" or "read"."""
    from apps.whatsapp.models import Conversation, MessageLog

    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name="App Download",
        template=template,
        contact_list=clist,
        status=WhatsAppCampaign.Status.COMPLETED,
        recipient_count=len(outcomes),
        queued_count=len(outcomes),
        started_at=timezone.now(),
        completed_at=timezone.now(),
    )
    campaign.record("sent", None, save=False, recipients=len(outcomes))
    campaign.record("completed", None)
    for i, outcome in enumerate(outcomes):
        phone = f"+26097{WhatsAppContact.objects.count() + 1:07d}"
        contact = Contact.objects.create(account=account, phone=phone)
        wc = WhatsAppContact.objects.create(
            account=account, phone_number=phone, contact=contact
        )
        failed = outcome in ("failed", "bad_number")
        msg = OutboundMessage.objects.create(
            account=account,
            contact=wc,
            template=template,
            payload={},
            status=OutboundMessage.Status.FAILED
            if failed
            else OutboundMessage.Status.SENT,
            error_code={"failed": BILLING, "bad_number": "131026"}.get(outcome, ""),
            last_error=RAW if outcome == "failed" else "",
        )
        if not failed:
            conv = Conversation.objects.create(account=account, contact=wc)
            msg.message_log = MessageLog.objects.create(
                account=account,
                conversation=conv,
                contact=wc,
                direction=MessageLog.Direction.OUTBOUND,
                timestamp=timezone.now(),
                status=outcome,
            )
            msg.save(update_fields=["message_log"])
        WhatsAppCampaignRecipient.objects.create(
            campaign=campaign,
            contact=contact,
            message=msg,
            # As tasks._notify_terminal_failure leaves a recipient whose message failed.
            status=WhatsAppCampaignRecipient.Status.FAILED
            if failed
            else WhatsAppCampaignRecipient.Status.QUEUED,
            error=RAW if outcome == "failed" else "",
        )
    return campaign


def _stats(campaign):
    from apps.whatsapp.campaigns import campaign_delivery_stats

    return campaign_delivery_stats(campaign)


# --- The outcome -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcomes", "state", "label"),
    [
        (["failed", "failed"], "failed", "Finished: 0 of 2 sent, 2 failed"),
        (["failed", "read"], "partly", "Finished: 1 of 2 sent, 1 failed"),
        (["delivered", "read"], "sent", "Finished: 2 of 2 sent"),
    ],
)
def test_a_finished_campaign_says_what_happened(shop, outcomes, state, label):
    _, account, template, clist = shop
    campaign = _campaign(account, template, clist, outcomes)
    stats = _stats(campaign)
    assert campaign_ui.lifecycle_state(campaign, stats) == state
    assert campaign_ui.outcome_label(stats) == label
    # Without delivery facts (the public API), the stored status as before.
    assert campaign_ui.lifecycle_state(campaign) == "sent"


@_wa_urls
def test_the_list_and_the_page_say_failed_not_sent(shop):
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "failed"])

    page = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    live = page[page.index('id="campaign-live"') :]
    live = live[: live.index("grid")]
    assert "badge-danger" in live and "Failed" in live and "badge-success" not in live
    assert "Finished: 0 of 2 sent, 2 failed" in page
    assert "Messages prepared for 2 customers" in page
    assert "Every message handed to WhatsApp" not in page
    assert "Sent to 2 customers" not in page

    listing = client.get("/email/campaigns/?channel=whatsapp").content.decode()
    row = listing[listing.index("App Download") :]
    row = row[: row.index("</tr>")]
    assert "badge-danger" in row and "badge-success" not in row


@_wa_urls
def test_partly_sent_reads_as_partly(shop):
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "read"])
    page = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "Partly sent" in page


# --- Plain reasons ---------------------------------------------------------------------


@_wa_urls
def test_failures_read_plainly_grouped_with_their_fix(shop):
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "failed", "bad_number"])
    page = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()

    groups = page[page.index("data-failure-groups") :]
    groups = groups[: groups.index("</section>")]
    assert (
        f"2</span> {TIMES} Meta blocked this message because of a billing problem"
        in groups
    )
    assert f"1</span> {TIMES} WhatsApp couldn" in groups
    assert "Fix the payment method in Meta" in groups
    assert groups.index("billing") < groups.index("couldn")  # most first

    assert "131042:" not in page and "(error " not in page
    assert ">Fix it</a>" in page


def test_a_stored_sentence_with_metas_code_reads_plainly_too():
    from apps.whatsapp.friendly_errors import failure

    reason, fix = failure("", RAW)
    assert reason.startswith("Meta blocked this message because of a billing problem")
    assert fix["label"] == "Fix the payment method in Meta"
    assert failure("", "Something odd") == ("Something odd", None)


# --- Retry -----------------------------------------------------------------------------


@_wa_urls
def test_retry_gives_way_to_the_fix_while_the_account_problem_stands(shop):
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["failed", "failed"])
    page = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "data-campaign-fix" in page and "Retry anyway" in page
    assert "Retry 2 failed" not in page


@_wa_urls
def test_retry_stays_for_message_level_failures_or_once_a_send_works(shop):
    client, account, template, clist = shop
    campaign = _campaign(account, template, clist, ["bad_number"])
    page = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "data-campaign-fix" not in page

    billing = _campaign(account, template, clist, ["failed"])
    # A message went through since: the problem is over.
    _campaign(account, template, clist, ["sent"])
    page = client.get(f"/whatsapp/campaigns/{billing.pk}/").content.decode()
    assert "Retry anyway" not in page and "data-campaign-fix" not in page


def test_account_problem_needs_an_account_code_and_nothing_since(shop):
    _, account, template, clist = shop
    assert account_problem(account) is None
    _campaign(account, template, clist, ["bad_number"])
    assert account_problem(account) is None  # about one customer, not the account
    _campaign(account, template, clist, ["failed"])
    problem = account_problem(account)
    assert problem["fix"]["label"] == "Fix the payment method in Meta"
    _campaign(account, template, clist, ["delivered"])
    assert account_problem(account) is None


# --- Before sending --------------------------------------------------------------------


@pytest.fixture
def ready(shop):
    """A list someone can be sent to, a connected number, and a billing failure on record."""
    from apps.whatsapp.models import WhatsAppBusinessNumber

    client, account, template, clist = shop
    WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="PN1", waba_id="WABA1", is_active=True
    )
    contact = Contact.objects.create(account=account, phone="+260971111111")
    WhatsAppContact.objects.create(
        account=account,
        phone_number=contact.phone,
        contact=contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    clist.contacts.add(contact)
    _campaign(
        account,
        template,
        ContactList.objects.create(account=account, name="Old"),
        ["failed"],
    )
    return client, account, template, clist


@_wa_urls
def test_the_composer_warns_in_step_one_and_on_review(ready):
    client, _, _, _ = ready
    page = client.get("/whatsapp/campaigns/new/").content.decode()
    assert page.count("data-account-problem") == 2
    assert "Messages from your WhatsApp account are failing" in page
    assert "Fix the payment method in Meta" in page
    assert 'name="confirm_account_problem"' in page  # the no-script box on Review


@_wa_urls
def test_send_is_refused_without_the_confirm_and_goes_with_it(
    ready, django_capture_on_commit_callbacks
):
    client, account, template, clist = ready
    form = {"name": "Launch", "contact_list": clist.pk, "template_id": template.pk}

    resp = client.post("/whatsapp/campaigns/new/", form)
    assert resp.status_code == 400
    assert b"Confirm to send anyway." in resp.content
    assert not WhatsAppCampaign.objects.filter(account=account, name="Launch").exists()

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            "/whatsapp/campaigns/new/", {**form, "confirm_account_problem": "1"}
        )
    assert resp.status_code == 302
    assert WhatsAppCampaign.objects.filter(account=account, name="Launch").exists()


@_wa_urls
def test_the_send_button_asks_first(ready):
    client, _, template, clist = ready
    html = client.get(
        "/whatsapp/campaigns/estimate/",
        {"contact_list": clist.pk, "template_id": template.pk},
        HTTP_HX_REQUEST="true",
    ).content.decode()
    button = html[html.index('id="send-slot"') :]
    assert "data-account-problem-send" in button and "Send anyway?" in button
    assert "confirm_account_problem" in button
