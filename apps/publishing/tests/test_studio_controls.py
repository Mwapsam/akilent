"""Content Studio app feel, Phases 4 and 5 outside Media: segmented controls and switches,
the Library's date popover, the month calendar's "+" and dragging a planned idea, and the
page shortcuts' list in the command palette."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.publishing.models import ContentPlanItem
from apps.publishing.tests.test_studio_overlay import shop  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def scheduling(settings):
    settings.RELEASED_FEATURES = frozenset({"cs_scheduling"})


def _idea(account, day, **kw):
    kw.setdefault("topic", "Autumn drop")
    return ContentPlanItem.objects.create(account=account, planned_date=day, **kw)


def test_a_planned_idea_moves_to_another_day(shop, scheduling):  # noqa: F811
    account, client = shop
    today = timezone.localdate()
    item = _idea(account, today)
    target = today + datetime.timedelta(days=3)

    resp = client.post(
        reverse("publishing:plan-move"),
        {"item": item.pk, "date": target.isoformat(), "next": "/publishing/calendar/"},
    )

    item.refresh_from_db()
    assert item.planned_date == target
    assert resp.status_code == 302 and resp.url == "/publishing/calendar/"


@pytest.mark.parametrize("case", ["past", "published", "theirs", "bad date"])
def test_a_planned_idea_doesnt_move_when_it_shouldnt(shop, scheduling, case):  # noqa: F811
    from apps.accounts.models import Account

    account, client = shop
    today = timezone.localdate()
    owner = (
        Account.objects.create(company_name="Other") if case == "theirs" else account
    )
    status = (
        ContentPlanItem.PlanStatus.PUBLISHED
        if case == "published"
        else ContentPlanItem.PlanStatus.PLANNED
    )
    item = _idea(owner, today, plan_status=status)
    day = {
        "past": (today - datetime.timedelta(days=1)).isoformat(),
        "bad date": "soon",
    }.get(case, (today + datetime.timedelta(days=2)).isoformat())

    client.post(reverse("publishing:plan-move"), {"item": item.pk, "date": day})

    item.refresh_from_db()
    assert item.planned_date == today


def test_moving_ideas_is_gated_with_scheduling(shop):  # noqa: F811
    _, client = shop
    assert client.post(reverse("publishing:plan-move")).status_code == 404


def test_the_month_calendar_plans_from_a_day_and_drags_ideas(shop, scheduling):  # noqa: F811
    account, client = shop
    today = timezone.localdate()
    item = _idea(account, today)
    done = _idea(
        account, today, topic="Old", plan_status=ContentPlanItem.PlanStatus.PUBLISHED
    )

    html = client.get(
        reverse("publishing:calendar"), {"view": "month"}
    ).content.decode()

    assert f"planned_date={today.isoformat()}" in html and 'class="cal-m-add"' in html
    assert f'id="plan-move-{item.pk}"' in html
    assert f'id="plan-move-{done.pk}"' not in html


def test_switches_and_segmented_controls(shop, settings):  # noqa: F811
    account, client = shop
    settings.RELEASED_FEATURES = frozenset({"cs_sales_report"})

    channels = client.get(reverse("publishing:channels")).content.decode()
    composer = client.get(reverse("publishing:post-new")).content.decode()
    calendar = client.get(reverse("publishing:calendar")).content.decode()

    assert 'role="switch" class="switch" name="requires_review"' in channels
    assert 'id="media-type" tabindex="-1" class="seg seg-block' in composer
    assert composer.count('type="radio" name="media_type"') == 6
    assert '<nav class="seg" aria-label="View">' in calendar


def test_library_dates_are_one_popover_that_keeps_the_dates(shop):  # noqa: F811
    _, client = shop
    html = client.get(
        reverse("publishing:library"),
        {"date_from": "2026-10-01", "date_to": "2026-10-31"},
    ).content.decode()

    assert "2026-10-01 \N{EN DASH} 2026-10-31" in html
    assert 'name="date_from" id="library-from" value="2026-10-01"' in html
    assert "Any time" in html  # the preset that clears them


def test_the_palette_lists_the_page_shortcuts(shop):  # noqa: F811
    _, client = shop
    html = client.get(reverse("publishing:library")).content.decode()
    assert 'group: "Shortcut", label: "J / K  Next or previous row"' in html
    assert "data-search" in html  # "/" goes to the Library's search
