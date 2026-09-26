"""Starting point: a business's first week on WhatsApp, and the same numbers a month later.

WhatsApp can't show messages from before a business connected, so the honest "before" is the
first 7 days after it did (``starting``). Days 30-37 (``day30``) are measured the same way and
compared with it. Both are computed from stored messages once the window has closed and a day has
passed (so every enquiry had 24 hours to be answered), then kept unchanged. A late capture gives
the same numbers, so businesses that connected before this existed get theirs on the next run.

The comparison states counts, never causes: "9 enquiries went unanswered in your first week,
2 this week", not "Akilent saved 7".
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from apps.conversations.models import Benchmark

WINDOW = timedelta(days=7)
DAY30_START = timedelta(days=30)
SETTLE = timedelta(days=1)  # every enquiry in the window gets 24h to be answered before measuring


def windows(connected_at) -> dict:
    return {
        Benchmark.Kind.STARTING: (connected_at, connected_at + WINDOW),
        Benchmark.Kind.DAY30: (connected_at + DAY30_START, connected_at + DAY30_START + WINDOW),
    }


def capture(account, connected_at, now=None) -> list[str]:
    """Measure every window of this business that has closed and settled. Returns the kinds made."""
    from apps.conversations.api import window_metrics

    now = now or timezone.now()
    have = set(Benchmark.objects.filter(account=account).values_list("kind", flat=True))
    made = []
    for kind, (start, end) in windows(connected_at).items():
        if kind in have or now < end + SETTLE:
            continue
        try:
            Benchmark.objects.create(account=account, kind=kind, window_start=start, window_end=end,
                                     metrics=window_metrics(account, start, end))
            made.append(kind)
        except IntegrityError:
            pass  # another run got there first
    return made


def card(account, connected_at, now=None) -> dict | None:
    """What the Starting point card shows: measuring, starting numbers, or the comparison."""
    if connected_at is None:
        return None
    now = now or timezone.now()
    rows = {b.kind: b for b in Benchmark.objects.filter(account=account)}
    starting, later = rows.get(Benchmark.Kind.STARTING), rows.get(Benchmark.Kind.DAY30)
    ranges = windows(connected_at)
    if starting is None:
        day = min(7, max(1, (now - connected_at).days + 1))
        return {"state": "measuring", "day": day, "ready_on": ranges[Benchmark.Kind.STARTING][1] + SETTLE}
    if later is None:
        return {"state": "starting", "start": starting.metrics,
                "compare_on": ranges[Benchmark.Kind.DAY30][1] + SETTLE}
    return {"state": "compared", "start": starting.metrics, "now": later.metrics,
            "rows": _comparison(starting.metrics, later.metrics),
            "fewer_unanswered": max(0, (starting.metrics.get("unanswered") or 0) - (later.metrics.get("unanswered") or 0))}


def _comparison(before: dict, after: dict) -> list[dict]:
    def minutes(value):
        return "—" if value is None else f"{value} min"

    return [
        {"label": "Conversations started", "before": before.get("conversations", 0), "after": after.get("conversations", 0)},
        {"label": "Enquiries with no reply within 24 hours", "before": before.get("unanswered", 0), "after": after.get("unanswered", 0)},
        {"label": "Median time to first reply", "before": minutes(before.get("median_first_reply_minutes")),
         "after": minutes(after.get("median_first_reply_minutes"))},
        {"label": "Interested customers", "before": before.get("interested", 0), "after": after.get("interested", 0)},
        {"label": "Paid orders from conversations", "before": before.get("paid_orders", 0), "after": after.get("paid_orders", 0)},
    ]
