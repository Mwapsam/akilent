"""Weekly snapshots: one immutable row per closed week since a business connected WhatsApp, so
Business Health's Momentum trend never has to re-derive history on every page view.

Like ``benchmarks`` (the Starting point card), a week is only measured once it has closed and
every enquiry in it has had 24 hours to be answered, then never re-measured. Weeks are aligned to
Monday (server time; see ``reporting.week_of``) rather than to the connection date, so every
business's weeks land on the same calendar boundary and can be compared or graphed side by side.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from apps.conversations.models import WeeklySnapshot
from apps.conversations.reporting import week_of

SETTLE = timedelta(
    days=1
)  # every enquiry in the week gets 24h to be answered before measuring
# How far back the first run backfills for a business connected long ago. A report doesn't need
# more than this to draw a meaningful trend, and it keeps one Celery run from doing unbounded work
# for an old account.
MAX_BACKFILL_WEEKS = 26


def closed_week_starts(connected_at, now) -> list:
    """Monday-aligned week starts from ``connected_at`` to the latest week that has closed and
    settled, capped to the most recent ``MAX_BACKFILL_WEEKS``."""
    first_monday = week_of(connected_at).start
    weeks = []
    monday = first_monday
    while monday + timedelta(days=7) + SETTLE <= now:
        weeks.append(monday)
        monday += timedelta(days=7)
    return weeks[-MAX_BACKFILL_WEEKS:]


def capture(account, connected_at, now=None) -> list:
    """Measure every closed, settled week that hasn't been captured yet. Returns the week starts
    made."""
    from apps.conversations.reporting import period_metrics

    now = now or timezone.now()
    have = set(
        WeeklySnapshot.objects.filter(account=account).values_list(
            "week_start", flat=True
        )
    )
    made = []
    for monday in closed_week_starts(connected_at, now):
        if monday in have:
            continue
        try:
            WeeklySnapshot.objects.create(
                account=account,
                week_start=monday,
                metrics=period_metrics(account, monday, monday + timedelta(days=7)),
            )
            made.append(monday)
        except IntegrityError:
            pass  # another run got there first
    return made


def trend(account, *, weeks: int = 12) -> list:
    """The most recent ``weeks`` captured snapshots, oldest first: ``[{"week_start", **metrics}]``."""
    rows = WeeklySnapshot.objects.filter(account=account).order_by("-week_start")[
        :weeks
    ]
    return [{"week_start": r.week_start, **r.metrics} for r in reversed(list(rows))]
