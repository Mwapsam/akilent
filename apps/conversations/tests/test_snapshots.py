"""Weekly snapshots for the Momentum trend: capture, idempotency, immutability, backfill cap.
See apps.conversations.snapshots and reporting.momentum."""

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations import reporting, snapshots
from apps.conversations.models import Conversation, Message, WeeklySnapshot
from apps.conversations.tasks import capture_weekly_snapshots
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _connect(account, days_ago):
    number = WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id=f"PN{account.pk}",
        waba_id="W",
        access_token="t",
        is_active=True,
    )
    at = timezone.now() - timedelta(days=days_ago)
    WhatsAppBusinessNumber.objects.filter(pk=number.pk).update(created_at=at)
    return at


def _enquiry(account, at, reply_after=None, phone=None):
    phone = phone or f"+26097{Contact.objects.count():07d}"
    contact = Contact.objects.create(account=account, phone=phone)
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="hi", timestamp=at
    )
    if reply_after is not None:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="ok",
            status="sent",
            timestamp=at + reply_after,
        )
    return conv


@pytest.mark.django_db
def test_closed_week_starts_only_includes_weeks_that_settled():
    now = timezone.now()
    connected = now - timedelta(days=20)
    weeks = snapshots.closed_week_starts(connected, now)
    # Every returned week's end + 1 day settle must be at or before now.
    for monday in weeks:
        assert monday + timedelta(days=8) <= now


@pytest.mark.django_db
def test_backfill_is_capped(account):
    now = timezone.now()
    connected = now - timedelta(weeks=60)  # far more than MAX_BACKFILL_WEEKS
    weeks = snapshots.closed_week_starts(connected, now)
    assert len(weeks) == snapshots.MAX_BACKFILL_WEEKS
    # It keeps the most recent weeks, not the oldest.
    assert weeks[-1] + timedelta(days=8) <= now
    assert weeks[-1] > connected + timedelta(weeks=30)


@pytest.mark.django_db
def test_capture_is_idempotent_and_immutable(account):
    connected = _connect(account, days_ago=20)
    now = timezone.now()
    made = snapshots.capture(account, connected, now)
    assert len(made) > 0
    again = snapshots.capture(account, connected, now)
    assert again == [], "each week is captured once and kept"

    row = WeeklySnapshot.objects.filter(account=account).first()
    row.metrics = {"tampered": True}
    with pytest.raises(ValueError):
        row.save()
    with pytest.raises(ValueError):
        row.delete()


@pytest.mark.django_db
def test_capture_counts_only_conversations_in_that_week(account):
    connected = _connect(account, days_ago=20)
    now = timezone.now()
    week1_start = snapshots.closed_week_starts(connected, now)[0]
    _enquiry(
        account, week1_start + timedelta(hours=1), reply_after=timedelta(minutes=5)
    )
    _enquiry(
        account, week1_start + timedelta(days=10), reply_after=timedelta(minutes=5)
    )
    snapshots.capture(account, connected, now)
    row = WeeklySnapshot.objects.get(account=account, week_start=week1_start)
    assert row.metrics["conversations"] == 1


@pytest.mark.django_db
def test_the_unique_constraint_prevents_a_duplicate_week(account):
    connected = _connect(account, days_ago=20)
    now = timezone.now()
    week1_start = snapshots.closed_week_starts(connected, now)[0]
    WeeklySnapshot.objects.create(
        account=account, week_start=week1_start, metrics={"conversations": 0}
    )
    with pytest.raises(IntegrityError):
        WeeklySnapshot.objects.create(
            account=account, week_start=week1_start, metrics={"conversations": 1}
        )


@pytest.mark.django_db
def test_capture_weekly_snapshots_task_runs_for_connected_accounts(account):
    _connect(account, days_ago=20)
    made = capture_weekly_snapshots()
    assert made > 0
    assert capture_weekly_snapshots() == 0


@pytest.mark.django_db
def test_momentum_shapes_series_that_match_labels_length(account):
    connected = _connect(account, days_ago=20)
    now = timezone.now()
    week1_start = snapshots.closed_week_starts(connected, now)[0]
    _enquiry(
        account, week1_start + timedelta(hours=1), reply_after=timedelta(minutes=5)
    )
    snapshots.capture(account, connected, now)

    m = reporting.momentum(account)
    assert m["has_data"] is True
    assert len(m["labels"]) == len(m["rows"])
    for _label, values in m["reply_series"] + m["answered_series"] + m["paid_series"]:
        assert len(values) == len(m["labels"])


@pytest.mark.django_db
def test_momentum_has_no_data_before_any_week_settles(account):
    m = reporting.momentum(account)
    assert m["has_data"] is False
    assert m["rows"] == []
