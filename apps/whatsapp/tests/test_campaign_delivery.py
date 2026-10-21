"""Release 3 (audit 2026-10-19), phase 10: pacing campaign sends — Meta's daily
limit, quiet hours, priority and fairness, stale holds, pre-send checks."""

import time as wallclock
from datetime import datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact, ContactList, ContactPhone
from apps.whatsapp import campaigns as svc
from apps.whatsapp import delivery
from apps.whatsapp.models import (
    CampaignSendWindow,
    MessageTemplate,
    OutboundMessage,
    WhatsAppBusinessNumber,
    WhatsAppCampaign,
    WhatsAppCampaignRecipient,
    WhatsAppContact,
)

_R = WhatsAppCampaignRecipient.Status
UTC = ZoneInfo("UTC")


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


def _list_of(account, n, start=0):
    cl = ContactList.objects.create(account=account, name=f"L{start}-{n}")
    contacts = Contact.objects.bulk_create(
        [Contact(account=account, phone=f"+2609{start + i:08d}") for i in range(n)]
    )
    WhatsAppContact.objects.bulk_create(
        [
            WhatsAppContact(account=account, phone_number=c.phone, contact=c)
            for c in contacts
        ]
    )
    cl.contacts.add(*contacts)
    return cl


def _send(account, template, cl):
    campaign = svc.create_and_queue_campaign(
        account=account, name="C", contact_list=cl, template_id=template.pk
    )
    svc.send_campaign.run(campaign.pk)
    campaign.refresh_from_db()
    return campaign


# --- Quiet hours ------------------------------------------------------------------


def test_next_open_waits_for_the_window():
    window = CampaignSendWindow(start=time(8, 0), end=time(20, 0), enabled=True)
    zone = ZoneInfo("Africa/Lusaka")  # UTC+2
    late = datetime(2026, 10, 20, 19, 30, tzinfo=UTC)  # 21:30 in Lusaka
    assert delivery.next_open(late, window, zone) == datetime(
        2026, 10, 21, 6, 0, tzinfo=UTC
    )
    early = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)  # 06:00 in Lusaka
    assert delivery.next_open(early, window, zone) == datetime(
        2026, 10, 20, 6, 0, tzinfo=UTC
    )
    noon = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert delivery.next_open(noon, window, zone) == noon
    window.enabled = False
    assert delivery.next_open(late, window, zone) == late


@pytest.mark.django_db
def test_campaign_messages_outside_the_window_wait(account, template):
    CampaignSendWindow.objects.create(account=account, start=time(8), end=time(20))
    at_night = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
    with mock.patch("django.utils.timezone.now", return_value=at_night):
        campaign = _send(account, template, _list_of(account, 2))
    msg = OutboundMessage.objects.first()
    assert msg.scheduled_at == datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    assert msg.priority == OutboundMessage.PRIORITY_BULK
    assert campaign.send_plan["window"] == ["08:00", "20:00"]


# --- Meta's daily limit -----------------------------------------------------------


@pytest.mark.django_db
def test_campaign_over_the_daily_limit_is_spread_over_days(account, template):
    WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="pn1", messaging_limit="TIER_50"
    )
    campaign = _send(account, template, _list_of(account, 60))
    assert campaign.send_plan["limit"] == 50 and campaign.send_plan["room"] == 50
    now = timezone.now()
    later = OutboundMessage.objects.filter(scheduled_at__gt=now + timedelta(hours=23))
    assert later.count() == 10
    assert OutboundMessage.objects.count() == 60

    f = delivery.forecast(account, 120)
    assert f["days"] == 3  # 50 today, 50 tomorrow, 20 the day after


@pytest.mark.django_db
def test_quality_webhook_stores_the_tier(account):
    from apps.whatsapp.tasks import _store_messaging_limit

    n = WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="pn1",
        waba_id="w1",
        display_number="+260 97 1234567",
    )
    _store_messaging_limit("w1", "260971234567", "TIER_1K")
    n.refresh_from_db()
    assert n.messaging_limit == "TIER_1K" and n.daily_limit == 1000


# --- Priority and fairness ---------------------------------------------------------


def test_fair_slice_shares_a_pass_between_businesses():
    class M:
        def __init__(self, account_id):
            self.account_id = account_id

    big = [M(1) for _ in range(90)]
    small = [M(2) for _ in range(10)]
    picked = delivery.fair_slice(big + small, 20)
    assert sum(1 for m in picked if m.account_id == 2) == 10
    assert len(picked) == 20


@pytest.mark.django_db
def test_drain_takes_replies_before_campaign_messages(account, template):
    _send(account, template, _list_of(account, 3))
    wc = WhatsAppContact.objects.first()
    reply = OutboundMessage.objects.create(
        account=account, contact=wc, payload={"type": "text", "text": "Hi"}
    )
    first = (
        OutboundMessage.objects.filter(status=OutboundMessage.Status.QUEUED)
        .order_by("-priority", "scheduled_at", "pk")
        .first()
    )
    assert first == reply


# --- Stale holds, unreachable numbers, template stopped -----------------------------------


@pytest.mark.django_db
def test_stale_held_campaign_messages_are_dropped_not_sent_late(account, template):
    campaign = _send(account, template, _list_of(account, 2))
    old = timezone.now() - timedelta(hours=80)
    OutboundMessage.objects.filter(pk=OutboundMessage.objects.first().pk).update(
        status=OutboundMessage.Status.HELD, created_at=old
    )
    assert svc.cancel_stale_campaign_holds()["cancelled"] == 1
    campaign.refresh_from_db()
    assert campaign.recipients.filter(skip_reason="plan_limit").count() == 1
    assert (campaign.queued_count, campaign.skipped_count) == (1, 1)


@pytest.mark.django_db
def test_numbers_known_not_to_be_on_whatsapp_are_skipped(account, template):
    cl = _list_of(account, 2)
    first = cl.contacts.order_by("pk").first()
    ContactPhone.objects.create(
        contact=first,
        raw_value=first.phone,
        normalized_value=first.phone,
        whatsapp_status=ContactPhone.WhatsAppStatus.UNREACHABLE,
    )
    campaign = _send(account, template, cl)
    assert campaign.recipients.get(contact=first).skip_reason == "not_on_whatsapp"
    assert campaign.queued_count == 1


@pytest.mark.django_db
def test_template_stopped_by_meta_halts_the_campaign(account, template):
    campaign = _send(account, template, _list_of(account, 2))
    assert svc.stop_campaigns_for_template([template.pk]) == 1
    campaign.refresh_from_db()
    assert campaign.status == WhatsAppCampaign.Status.FAILED
    assert "paused or rejected" in campaign.error
    assert not OutboundMessage.objects.filter(
        status=OutboundMessage.Status.QUEUED
    ).exists()


@pytest.mark.django_db
def test_high_failure_rate_alerts_the_owners_once(account, template):
    campaign = _send(account, template, _list_of(account, 60))
    campaign.recipients.update(status=_R.FAILED)
    with mock.patch("apps.accounts.notifications.notify_team") as notify:
        assert svc.alert_on_campaign_failure_rate()["alerted"] == 1
        assert svc.alert_on_campaign_failure_rate()["alerted"] == 0
    assert notify.call_count == 1


@pytest.mark.django_db
def test_only_one_chain_waits_while_whatsapp_is_off(account, template):
    campaign = svc.create_and_queue_campaign(
        account=account,
        name="C",
        contact_list=_list_of(account, 1),
        template_id=template.pk,
    )
    cache.set(f"wa_campaign_waiting:{campaign.pk}", "another-task", 600)
    with mock.patch("apps.core.channels.channel_state", return_value="disabled"):
        assert svc.send_campaign.run(campaign.pk) is None  # bows out, no retry


# --- Load ----------------------------------------------------------------------------


@pytest.mark.performance
@pytest.mark.django_db
def test_ten_thousand_recipient_fanout_and_a_reply_still_goes_first(account, template):
    cl = _list_of(account, 10_000)
    started = wallclock.monotonic()
    campaign = svc.create_and_queue_campaign(
        account=account, name="Big", contact_list=cl, template_id=template.pk
    )
    while (
        svc.send_campaign.run(campaign.pk) is None
        and campaign.recipients.filter(status=_R.PENDING).exists()
    ):
        pass
    elapsed = wallclock.monotonic() - started
    assert OutboundMessage.objects.count() == 10_000
    assert elapsed < 180, f"fan-out took {elapsed:.0f}s"

    wc = WhatsAppContact.objects.first()
    reply = OutboundMessage.objects.create(
        account=account,
        contact=wc,
        payload={"type": "text", "text": "Your order is ready"},
    )
    t0 = wallclock.monotonic()
    first = (
        OutboundMessage.objects.filter(status=OutboundMessage.Status.QUEUED)
        .order_by("-priority", "scheduled_at", "pk")
        .first()
    )
    assert first == reply
    assert wallclock.monotonic() - t0 < 2
