"""Content Studio walkthrough fixes (2026-11-13): sticky tabs, calendar empty days, the
composer's timezone, what stops Publish, titles and the bio page address."""

import datetime
import re

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Audit Co")
    user = User.objects.create_user("audit", "audit@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, user, client


def _member(account, role):
    user = User.objects.create_user(f"m-{role}", f"{role}@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=role)
    client = Client()
    client.force_login(user)
    return client


# --- 1. Tabs stay in reach ------------------------------------------------------------


def test_the_tab_bar_is_sticky(owner):
    _, _, client = owner
    html = client.get(reverse("publishing:calendar")).content.decode()
    assert re.search(r'<nav class="cs-tabs[^"]*"[^>]*aria-label="Content Studio"', html)


# --- 2. Calendar: "posted" only for days that are over --------------------------------


def test_only_past_days_say_nothing_posted(owner):
    _, _, client = owner
    today = timezone.localdate()
    monday = today - datetime.timedelta(days=today.weekday())
    past_days = (today - monday).days
    html = client.get(reverse("publishing:calendar")).content.decode()
    # One agenda per page (the week view's narrow layout): one line per empty day.
    assert html.count("Nothing posted") == past_days
    assert html.count("Nothing planned</span>") == 7 - past_days


def test_a_future_week_never_says_nothing_posted(owner):
    _, _, client = owner
    nxt = (timezone.localdate() + datetime.timedelta(days=7)).isocalendar()
    html = client.get(
        reverse("publishing:calendar"), {"week": f"{nxt[0]}-W{nxt[1]:02d}"}
    ).content.decode()
    assert "Nothing posted" not in html


# --- 3. Timezone in one click ---------------------------------------------------------


def test_composer_offers_the_device_timezone_to_an_admin(owner):
    _, _, client = owner
    html = client.get(reverse("publishing:post-new")).content.decode()
    assert reverse("settings-timezone") in html
    assert "(this device)" in html


def test_composer_tells_a_non_admin_who_can_set_it(owner):
    account, _, _ = owner
    client = _member(account, Membership.Role.MEMBER)
    html = client.get(reverse("publishing:post-new")).content.decode()
    assert reverse("settings-timezone") not in html
    assert "Ask an owner or admin" in html


def test_no_timezone_prompt_once_it_is_set(owner):
    account, _, client = owner
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka")
    html = client.get(reverse("publishing:post-new")).content.decode()
    assert "(this device)" not in html
    assert "Times are in Africa/Lusaka." in html


def test_setting_the_timezone_keeps_opening_hours(owner):
    account, _, client = owner
    schedule = {"mon": {"open": "08:00", "close": "17:00"}}
    BusinessHours.objects.create(account=account, timezone="UTC", schedule=schedule)

    resp = client.post(reverse("settings-timezone"), {"timezone": "Africa/Lusaka"})

    assert resp.json() == {"ok": True, "timezone": "Africa/Lusaka"}
    hours = BusinessHours.objects.get(account=account)
    assert hours.timezone == "Africa/Lusaka"
    assert hours.schedule == schedule


def test_setting_the_timezone_rejects_unknown_zones_and_non_admins(owner):
    account, _, client = owner
    assert client.post(reverse("settings-timezone"), {"timezone": "Mars/Base"}).status_code == 400
    member = _member(account, Membership.Role.MEMBER)
    assert member.post(reverse("settings-timezone"), {"timezone": "Africa/Lusaka"}).status_code == 403
    assert not BusinessHours.objects.filter(account=account).exists()


# --- 4. A blocked Publish says why ----------------------------------------------------


def test_publish_button_carries_its_reason(owner):
    _, _, client = owner
    html = client.get(reverse("publishing:post-new")).content.decode()
    assert ':title="blockReason(\'publish_now\')"' in html
    assert '@click="readyClick()"' in html
