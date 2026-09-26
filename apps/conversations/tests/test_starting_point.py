"""Starting point: the first week after WhatsApp connects, compared with a week a month later."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import api as conversations_api
from apps.conversations import benchmarks
from apps.conversations.models import Benchmark, Conversation, Message
from apps.conversations.tasks import capture_benchmarks
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber


def _connect(account, days_ago):
    number = WhatsAppBusinessNumber.objects.create(account=account, phone_number_id=f"PN{account.pk}",
                                                   waba_id="W", access_token="t", is_active=True)
    at = timezone.now() - timedelta(days=days_ago)
    WhatsAppBusinessNumber.objects.filter(pk=number.pk).update(created_at=at)
    return at


def _enquiry(account, at, reply_after=None, phone="+260971234567"):
    contact = Contact.objects.create(account=account, phone=phone)
    conv = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    Message.objects.create(account=account, conversation=conv, direction="inbound", body="Price?", timestamp=at)
    if reply_after is not None:
        Message.objects.create(account=account, conversation=conv, direction="outbound", body="K50",
                               timestamp=at + reply_after)
    return conv


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.mark.django_db
def test_window_metrics_count_only_conversations_that_started_in_the_week(account):
    start = timezone.now() - timedelta(days=20)
    _enquiry(account, start + timedelta(hours=1), reply_after=timedelta(minutes=10), phone="+260971000001")
    _enquiry(account, start + timedelta(days=2), reply_after=timedelta(minutes=30), phone="+260971000002")
    _enquiry(account, start + timedelta(days=3), reply_after=timedelta(hours=30), phone="+260971000003")
    _enquiry(account, start + timedelta(days=4), phone="+260971000004")
    _enquiry(account, start + timedelta(days=9), phone="+260971000005")  # after the week
    m = conversations_api.window_metrics(account, start, start + timedelta(days=7))
    assert m["conversations"] == 4
    assert m["unanswered"] == 2, "no reply, and a reply after 24h, both count as unanswered"
    assert m["median_first_reply_minutes"] == 30
    assert m["revenue"] == [] and m["paid_orders"] == 0


@pytest.mark.django_db
def test_nothing_is_captured_until_the_week_has_closed_and_settled(account):
    connected = _connect(account, days_ago=7)
    assert benchmarks.capture(account, connected) == []
    card = conversations_api.starting_point(account)
    assert card["state"] == "measuring" and card["day"] == 7


@pytest.mark.django_db
def test_a_business_that_connected_long_ago_gets_both_weeks_on_the_first_run_once(account):
    connected = _connect(account, days_ago=60)
    _enquiry(account, connected + timedelta(days=1))
    _enquiry(account, connected + timedelta(days=31), reply_after=timedelta(minutes=5), phone="+260971000009")
    assert capture_benchmarks() == 2
    assert capture_benchmarks() == 0, "each week is measured once and kept"
    card = conversations_api.starting_point(account)
    assert card["state"] == "compared" and card["fewer_unanswered"] == 1
    assert card["start"]["unanswered"] == 1 and card["now"]["unanswered"] == 0


@pytest.mark.django_db
def test_after_the_first_week_the_card_shows_the_starting_numbers(account):
    connected = _connect(account, days_ago=12)
    _enquiry(account, connected + timedelta(days=2))
    benchmarks.capture(account, connected)
    card = conversations_api.starting_point(account)
    assert card["state"] == "starting" and card["start"]["conversations"] == 1


@pytest.mark.django_db
def test_no_card_before_whatsapp_is_connected_and_no_cross_business_numbers(account):
    assert conversations_api.starting_point(account) is None
    other = Account.objects.create(company_name="Other")
    connected = _connect(account, days_ago=60)
    _enquiry(other, connected + timedelta(days=1))
    capture_benchmarks()
    assert Benchmark.objects.get(account=account, kind="starting").metrics["conversations"] == 0


@pytest.mark.django_db
def test_insights_shows_the_card(client, account):
    user = User.objects.create_user("amara", "amara@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    _connect(account, days_ago=3)
    html = client.get("/email/insights/").content.decode()
    assert "Your starting point" in html and "day 4 of 7" in html
