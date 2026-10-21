"""Release 2 (audit 2026-10-19): drafts, scheduling, pause/resume/cancel,
duplicate, retry failed, test sends, export — and segments as an audience."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact, ContactList, Segment
from apps.whatsapp import campaigns as svc
from apps.whatsapp.models import (
    MessageTemplate,
    OutboundMessage,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)

_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled")
_S = WhatsAppCampaign.Status
_R = WhatsAppCampaignRecipient.Status
_Opt = WhatsAppContact.OptInStatus


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def template(account):
    return MessageTemplate.objects.create(
        account=account,
        name="Update",
        whatsapp_template_name="update",
        content="Hello!",
        category=MessageTemplate.Category.UTILITY,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


def _customer(account, i, status=_Opt.OPTED_IN):
    c = Contact.objects.create(account=account, phone=f"+26097700{i:04d}")
    WhatsAppContact.objects.filter(contact=c).delete()
    WhatsAppContact.objects.create(
        account=account, phone_number=c.phone, contact=c, opt_in_status=status
    )
    return c


@pytest.fixture
def vips(account):
    cl = ContactList.objects.create(account=account, name="VIPs")
    cl.contacts.add(_customer(account, 1), _customer(account, 2))
    return cl


def _sent_now(account, template, cl, **kw):
    return svc.create_and_queue_campaign(
        account=account, name="C", contact_list=cl, template_id=template.pk, **kw
    )


# --- Scheduling --------------------------------------------------------------


@pytest.mark.django_db
def test_scheduled_campaign_picks_recipients_when_it_fires(account, template, vips):
    later = timezone.now() + timedelta(hours=2)
    campaign = _sent_now(account, template, vips, send_at=later, tz="Africa/Lusaka")
    assert campaign.status == _S.SCHEDULED
    assert not campaign.recipients.exists()

    vips.contacts.add(_customer(account, 3))  # added after scheduling
    assert svc.dispatch_due_campaigns()["started"] == 0  # not due yet

    WhatsAppCampaign.objects.filter(pk=campaign.pk).update(
        scheduled_at=timezone.now() - timedelta(minutes=1)
    )
    assert svc.dispatch_due_campaigns()["started"] == 1
    assert svc.dispatch_due_campaigns()["started"] == 0  # never twice
    campaign.refresh_from_db()
    assert campaign.status == _S.QUEUED
    assert campaign.recipient_count == 3
    assert [e["event"] for e in campaign.history] == ["scheduled", "sent"]


@pytest.mark.django_db
def test_scheduled_campaign_whose_list_emptied_fails_with_the_reason(
    account, template, vips
):
    campaign = _sent_now(
        account, template, vips, send_at=timezone.now() + timedelta(hours=1)
    )
    vips.contacts.clear()
    WhatsAppCampaign.objects.filter(pk=campaign.pk).update(
        scheduled_at=timezone.now() - timedelta(minutes=1)
    )
    svc.dispatch_due_campaigns()
    campaign.refresh_from_db()
    assert campaign.status == _S.FAILED
    assert "empty" in campaign.error


@pytest.mark.django_db
def test_send_campaign_does_nothing_for_a_draft_or_scheduled_campaign(
    account, template, vips
):
    draft = svc.save_draft(
        account=account, name="D", template_id=template.pk, contact_list=vips
    )
    svc.send_campaign.run(draft.pk)
    draft.refresh_from_db()
    assert draft.status == _S.DRAFT
    assert not OutboundMessage.objects.exists()


# --- Pause / resume / cancel ---------------------------------------------------


@pytest.mark.django_db
def test_paused_campaign_stops_fanout_and_its_messages_wait(account, template, vips):
    from apps.whatsapp.tasks import _PAUSED_CAMPAIGN

    campaign = _sent_now(account, template, vips)
    assert svc.pause_campaign(campaign)
    svc.send_campaign.run(campaign.pk)
    assert campaign.recipients.filter(status=_R.PENDING).count() == 2  # untouched

    assert svc.resume_campaign(campaign)
    svc.send_campaign.run(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == _S.COMPLETED
    assert OutboundMessage.objects.count() == 2

    # Fan-out finished but messages still queued: pausing holds them back.
    assert svc.pause_campaign(campaign)
    assert not OutboundMessage.objects.exclude(_PAUSED_CAMPAIGN).exists()
    svc.resume_campaign(campaign)
    campaign.refresh_from_db()
    assert campaign.status == _S.COMPLETED
    assert OutboundMessage.objects.exclude(_PAUSED_CAMPAIGN).count() == 2


@pytest.mark.django_db
def test_cancel_withdraws_waiting_messages_and_unsent_recipients(
    account, template, vips
):
    vips.contacts.add(_customer(account, 3))
    campaign = _sent_now(account, template, vips)
    # One chunk of one: process one recipient, leave two pending.
    svc._CAMPAIGN_CHUNK_SIZE, saved = 1, svc._CAMPAIGN_CHUNK_SIZE
    try:
        svc.send_campaign.run(campaign.pk)
    finally:
        svc._CAMPAIGN_CHUNK_SIZE = saved

    n = svc.cancel_campaign(campaign)

    campaign.refresh_from_db()
    assert n == 3
    assert campaign.status == _S.CANCELLED
    assert campaign.queued_count == 0 and campaign.skipped_count == 3
    assert set(campaign.recipients.values_list("skip_reason", flat=True)) == {
        "cancelled"
    }
    assert OutboundMessage.objects.get().status == OutboundMessage.Status.CANCELLED
    assert svc.cancel_campaign(campaign) == 0


@pytest.mark.django_db
def test_cancel_a_scheduled_campaign(account, template, vips):
    campaign = _sent_now(
        account, template, vips, send_at=timezone.now() + timedelta(hours=1)
    )
    svc.cancel_campaign(campaign)
    campaign.refresh_from_db()
    assert campaign.status == _S.CANCELLED
    WhatsAppCampaign.objects.filter(pk=campaign.pk).update(
        scheduled_at=timezone.now() - timedelta(minutes=1)
    )
    assert svc.dispatch_due_campaigns()["due"] == 0


# --- Duplicate / retry -------------------------------------------------------


@pytest.mark.django_db
def test_duplicate_makes_an_editable_draft(account, template, vips):
    campaign = _sent_now(account, template, vips)
    copy = svc.duplicate_campaign(campaign)
    assert copy.status == _S.DRAFT and copy.contact_list == vips
    assert not copy.recipients.exists()


@pytest.mark.django_db
def test_retry_failed_reaches_only_failed_and_not_opted_out(account, template, vips):
    third = _customer(account, 3)
    vips.contacts.add(third)
    campaign = _sent_now(account, template, vips)
    svc.send_campaign.run(campaign.pk)
    rows = list(campaign.recipients.order_by("pk"))
    WhatsAppCampaignRecipient.objects.filter(pk__in=[rows[0].pk, rows[1].pk]).update(
        status=_R.FAILED
    )
    WhatsAppContact.objects.filter(contact_id=rows[1].contact_id).update(
        opt_in_status=_Opt.OPTED_OUT
    )

    retry = svc.retry_failed_campaign(campaign)

    assert retry.retry_of == campaign
    assert list(retry.recipients.values_list("contact_id", flat=True)) == [
        rows[0].contact_id
    ]


# --- Test send ----------------------------------------------------------------


@pytest.mark.django_db
def test_send_test_queues_one_message_and_no_campaign(account, template):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ada")
    msg = svc.send_test(
        account=account,
        template=template,
        mapping={},
        fallbacks={},
        phone="0977123456",
        user=user,
    )
    assert msg.contact.phone_number == "+260977123456"
    assert msg.contact.contact is None  # no customer record made
    assert not WhatsAppCampaign.objects.exists()

    for bad in ("", "12"):
        with pytest.raises(svc.CampaignError, match="country code"):
            svc.send_test(
                account=account,
                template=template,
                mapping={},
                fallbacks={},
                phone=bad,
                user=user,
            )

    msg.contact.record_opt_out("STOP")
    with pytest.raises(svc.CampaignError, match="STOP"):
        svc.send_test(
            account=account,
            template=template,
            mapping={},
            fallbacks={},
            phone="0977123456",
            user=user,
        )


# --- Screens -----------------------------------------------------------------


@pytest.fixture
def owner_client(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@_wa_urls
@pytest.mark.django_db
def test_composer_schedules_and_rejects_a_past_time(owner_client, template, vips):
    base = {
        "audience": f"list:{vips.pk}",
        "template_id": template.pk,
        "when": "later",
        "send_tz": "Africa/Lusaka",
        "send_time": "09:00",
    }
    resp = owner_client.post(
        "/whatsapp/campaigns/new/", {**base, "send_date": "2020-01-01"}
    )
    assert resp.status_code == 400
    assert "already passed" in resp.content.decode()

    day = (timezone.now() + timedelta(days=3)).date().isoformat()
    resp = owner_client.post("/whatsapp/campaigns/new/", {**base, "send_date": day})
    campaign = WhatsAppCampaign.objects.get()
    assert resp["Location"] == f"/whatsapp/campaigns/{campaign.pk}/"
    assert campaign.status == _S.SCHEDULED
    assert campaign.timezone == "Africa/Lusaka"
    local = campaign.scheduled_at.astimezone(
        __import__("zoneinfo").ZoneInfo("Africa/Lusaka")
    )
    assert (local.hour, local.minute) == (9, 0)
    body = owner_client.get(resp["Location"]).content.decode()
    assert "Who it would reach if it went now" in body


@_wa_urls
@pytest.mark.django_db
def test_member_can_draft_but_not_send(client, account, template, vips):
    user = User.objects.create_user("m", "m@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    client.force_login(user)
    data = {"audience": f"list:{vips.pk}", "template_id": template.pk}

    resp = client.post("/whatsapp/campaigns/new/", {**data, "action": "send"})
    assert resp.status_code == 400
    assert not WhatsAppCampaign.objects.exists()

    client.post("/whatsapp/campaigns/new/", {**data, "action": "draft"})
    draft = WhatsAppCampaign.objects.get()
    assert draft.status == _S.DRAFT
    client.post(f"/whatsapp/campaigns/{draft.pk}/send_now/")
    draft.refresh_from_db()
    assert draft.status == _S.DRAFT


@_wa_urls
@pytest.mark.django_db
def test_draft_opens_in_the_composer_and_sends(owner_client, template, vips):
    draft = svc.save_draft(
        account=vips.account,
        name="December",
        template_id=template.pk,
        contact_list=vips,
    )
    body = owner_client.get(
        f"/whatsapp/campaigns/new/?draft={draft.pk}"
    ).content.decode()
    assert "step: 4" in body and "December" in body and f"list:{vips.pk}" in body

    owner_client.post(
        "/whatsapp/campaigns/new/",
        {
            "draft_id": draft.pk,
            "audience": f"list:{vips.pk}",
            "template_id": template.pk,
            "name": "December",
        },
    )
    draft.refresh_from_db()
    assert draft.status == _S.QUEUED and draft.recipient_count == 2
    assert WhatsAppCampaign.objects.count() == 1


@_wa_urls
@pytest.mark.django_db
def test_detail_actions_pause_and_cancel(owner_client, account, template, vips):
    campaign = _sent_now(account, template, vips)
    body = owner_client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "Pause" in body and "Cancel campaign" in body

    owner_client.post(f"/whatsapp/campaigns/{campaign.pk}/pause/")
    campaign.refresh_from_db()
    assert campaign.status == _S.PAUSED
    owner_client.post(f"/whatsapp/campaigns/{campaign.pk}/cancel/")
    campaign.refresh_from_db()
    assert campaign.status == _S.CANCELLED
    body = owner_client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "Cancelled" in body and "History" in body


@_wa_urls
@pytest.mark.django_db
def test_export_escapes_formulas(owner_client, account, template, vips):
    vips.contacts.update(first_name="=HYPERLINK(1)")
    campaign = _sent_now(account, template, vips)
    body = owner_client.get(
        f"/whatsapp/campaigns/{campaign.pk}/export.csv"
    ).content.decode()
    assert "'=HYPERLINK(1)" in body and "+26097700" in body


@_wa_urls
@pytest.mark.django_db
def test_another_accounts_campaign_actions_are_404(owner_client, template, vips):
    other = Account.objects.create(company_name="Other")
    t = MessageTemplate.objects.create(
        account=other, name="T", whatsapp_template_name="t", content="x"
    )
    theirs = WhatsAppCampaign.objects.create(account=other, name="X", template=t)
    assert (
        owner_client.post(f"/whatsapp/campaigns/{theirs.pk}/cancel/").status_code == 404
    )
    assert (
        owner_client.get(f"/whatsapp/campaigns/{theirs.pk}/export.csv").status_code
        == 404
    )


# --- Segments ------------------------------------------------------------------


@pytest.mark.django_db
def test_segment_whatsapp_rules_match_the_reachability_states(account):
    from apps.contacts import segments

    opted = _customer(account, 1)
    updates = _customer(account, 2, status=_Opt.UNKNOWN)
    stopped = _customer(account, 3, status=_Opt.OPTED_OUT)
    email_only = Contact.objects.create(account=account, email="e@example.com")

    def ids(value, op="eq"):
        d = {
            "op": "and",
            "conditions": [{"field": "whatsapp", "operator": op, "value": value}],
        }
        return set(segments.contacts_for(d, account).values_list("pk", flat=True))

    assert ids("opted_in") == {opted.pk}
    assert ids("updates") == {updates.pk}
    assert ids("opted_out") == {stopped.pk}
    assert ids("none") == {email_only.pk}
    reachable = {
        "op": "and",
        "conditions": [
            {"field": "whatsapp_reachable", "operator": "eq", "value": True}
        ],
    }
    assert set(
        segments.contacts_for(reachable, account).values_list("pk", flat=True)
    ) == {
        opted.pk,
        updates.pk,
    }


@pytest.mark.django_db
def test_campaign_to_a_segment_reaches_whoever_matches_then(account, template):
    _customer(account, 1)
    _customer(account, 2, status=_Opt.OPTED_OUT)
    seg = Segment.objects.create(
        account=account,
        name="Opted in",
        definition={
            "op": "and",
            "conditions": [
                {"field": "whatsapp", "operator": "eq", "value": "opted_in"}
            ],
        },
    )
    campaign = svc.create_and_queue_campaign(
        account=account,
        name="S",
        segment=seg,
        template_id=template.pk,
        send_at=timezone.now() + timedelta(hours=1),
    )
    _customer(account, 3)
    WhatsAppCampaign.objects.filter(pk=campaign.pk).update(
        scheduled_at=timezone.now() - timedelta(minutes=1)
    )
    svc.dispatch_due_campaigns()
    campaign.refresh_from_db()
    assert campaign.contact_list is None
    assert campaign.recipient_count == 2


@pytest.mark.django_db
def test_segment_screens(owner_client, account):
    _customer(account, 1)
    _customer(account, 2, status=_Opt.UNKNOWN)
    rules = '{"op": "and", "conditions": [{"field": "whatsapp", "operator": "eq", "value": "opted_in"}]}'

    preview = owner_client.post("/contacts/segments/preview/", {"definition": rules})
    assert "Matches now" in preview.content.decode()

    resp = owner_client.post(
        "/contacts/segments/new/", {"name": "Opted in", "definition": rules}
    )
    assert resp["Location"] == "/contacts/segments/"
    seg = Segment.objects.get(account=account)
    assert "1" in owner_client.get("/contacts/segments/").content.decode()

    dupe = owner_client.post(
        "/contacts/segments/new/", {"name": "Opted in", "definition": rules}
    )
    assert dupe.status_code == 400

    bad = owner_client.post(
        "/contacts/segments/new/", {"name": "Bad", "definition": '{"conditions": []}'}
    )
    assert bad.status_code == 400

    resp = owner_client.post(f"/contacts/segments/{seg.pk}/to-list/")
    clist = ContactList.objects.get(account=account)
    assert clist.contacts.count() == 1

    body = owner_client.get(f"/contacts/?segment={seg.pk}").content.decode()
    assert "1 match" in body


@pytest.mark.django_db
def test_segment_used_by_a_campaign_is_kept(owner_client, account, template):
    seg = Segment.objects.create(
        account=account,
        name="All",
        definition={
            "op": "and",
            "conditions": [{"field": "has_phone", "operator": "eq", "value": True}],
        },
    )
    WhatsAppCampaign.objects.create(
        account=account, name="C", segment=seg, template=template
    )
    owner_client.post(f"/contacts/segments/{seg.pk}/delete/")
    assert Segment.objects.filter(pk=seg.pk).exists()


@pytest.mark.django_db
def test_save_filter_as_segment_prefills_the_rules(owner_client, account):
    body = owner_client.get(
        "/contacts/segments/new/?from=filter&wa=opted_in&q=Ada"
    ).content.decode()
    assert "opted_in" in body and "Ada" in body
