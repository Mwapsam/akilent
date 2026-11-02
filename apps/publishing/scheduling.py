"""Scheduling (Phase 2a, ``cs_scheduling``): moving posts on the month calendar, weekly
posting slots, and when a business's posts have done best.

Every time here is on the business's own clock (``workspace_timezone``). Moving a post
or filling in a slot never skips approval: a move needs a valid approval like any
reschedule, and a slot only fills in the composer's time, which is then checked and
saved by the composer as usual.
"""

from __future__ import annotations

import datetime
import statistics
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.business_hours import workspace_zone as _tz

from .models import (
    ContentPost,
    ContentPostVariant,
    PostingSlot,
    VariantMetricSnapshot,
)

FLAG = "cs_scheduling"
SLOT_WEEKS = 8  # how far ahead "Next free slot" looks
SLOT_MIN_LEAD = datetime.timedelta(minutes=5)  # a slot closer than this is skipped
BEST_WINDOW = datetime.timedelta(days=90)
BEST_SETTLED = datetime.timedelta(days=3)  # numbers this old have mostly settled
BEST_MIN_POSTS = 10  # measured posts on a channel before any time is suggested
BEST_MIN_PER_TIME = 3  # posts a time of week needs before it can be suggested
BLOCK_HOURS = 3  # times of day are compared in 3-hour blocks
DAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class SchedulingError(ValueError):
    """The move or slot isn't allowed; the message is for the person."""


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


# --- Moving a post ---------------------------------------------------------------------


def can_move(variant: ContentPostVariant, post: ContentPost) -> bool:
    """A channel that hasn't gone out: scheduled, or a draft channel of a post that is
    scheduled or partly publishing. Published, publishing, failed and checking
    channels are locked."""
    Status = ContentPostVariant.Status
    if variant.platform not in (post.platforms or []) or variant.scheduled_at is None:
        return False
    return variant.status == Status.SCHEDULED or (
        variant.status == Status.DRAFT
        and post.status in (ContentPost.Status.SCHEDULED, ContentPost.Status.PUBLISHING)
    )


def move_to_day(account, post_pk: int, platforms, day: datetime.date, now=None):
    """Move these channels of a post to ``day``, each keeping its time of day. Returns
    the moved variants."""
    from .services import reschedule_variant

    now = now or timezone.now()
    tz = _tz(account)
    with transaction.atomic():
        post = (
            ContentPost.objects.select_for_update()
            .filter(pk=post_pk, account=account)
            .first()
        )
        if post is None:
            raise SchedulingError("That post isn't in this workspace.")
        variants = list(
            post.variants.select_for_update().filter(platform__in=list(platforms))
        )
        if not variants or len(variants) != len(set(platforms)):
            raise SchedulingError("Choose the post's channels to move.")
        if not all(can_move(v, post) for v in variants):
            raise SchedulingError("Only channels that haven't gone out can be moved.")
        if not post.is_approval_valid():
            raise SchedulingError(
                "This post needs an approved review before it can be scheduled."
            )
        moves = []
        for v in variants:
            local = timezone.localtime(v.scheduled_at, tz)
            when = datetime.datetime.combine(day, local.time(), tzinfo=tz)
            if when <= now:
                raise SchedulingError("Choose a day that's still ahead.")
            moves.append((v, when))
        for v, when in moves:
            v.last_error = ""
            v.save(update_fields=["last_error"])
            reschedule_variant(v, when)
    return [v for v, _ in moves]


# --- Posting slots ---------------------------------------------------------------------


def add_slot(account, weekday, time_value) -> PostingSlot:
    try:
        weekday = int(weekday)
        time_value = (
            time_value
            if isinstance(time_value, datetime.time)
            else datetime.time.fromisoformat(str(time_value))
        )
    except (TypeError, ValueError) as exc:
        raise SchedulingError("Choose a day and a time.") from exc
    if not 0 <= weekday <= 6:
        raise SchedulingError("Choose a day of the week.")
    time_value = time_value.replace(second=0, microsecond=0)
    slot, created = PostingSlot.objects.get_or_create(
        account=account, weekday=weekday, time=time_value
    )
    if not created:
        raise SchedulingError("That slot is already in your schedule.")
    return slot


def next_free_slot(account, platforms, after=None) -> datetime.datetime | None:
    """The next slot at which none of ``platforms`` already has a channel scheduled,
    within ``SLOT_WEEKS``. None when there are no slots, or none is free."""
    slots = list(PostingSlot.objects.filter(account=account))
    if not slots:
        return None
    after = (after or timezone.now()) + SLOT_MIN_LEAD
    tz = _tz(account)
    start = timezone.localtime(after, tz).date()
    horizon = datetime.datetime.combine(
        start + datetime.timedelta(weeks=SLOT_WEEKS + 1), datetime.time.min, tzinfo=tz
    )
    taken = set(
        ContentPostVariant.objects.filter(
            post__account=account,
            platform__in=list(platforms),
            status__in=(
                ContentPostVariant.Status.SCHEDULED,
                ContentPostVariant.Status.DRAFT,
            ),
            post__status__in=(
                ContentPost.Status.SCHEDULED,
                ContentPost.Status.PUBLISHING,
            ),
            scheduled_at__gte=after,
            scheduled_at__lt=horizon,
        ).values_list("platform", "scheduled_at")
    )
    by_day = defaultdict(list)
    for slot in slots:
        by_day[slot.weekday].append(slot.time)
    for offset in range(SLOT_WEEKS * 7 + 1):
        day = start + datetime.timedelta(days=offset)
        for t in sorted(by_day.get(day.weekday(), [])):
            at = datetime.datetime.combine(day, t, tzinfo=tz)
            if at < after:
                continue
            if any((p, at) in taken for p in platforms):
                continue
            return at
    return None


# --- Best times to post ----------------------------------------------------------------


def _number(values: dict, key: str):
    value = values.get(key)
    return value if isinstance(value, int | float) else None


def _engagement(platform: str, values: dict):
    """Interactions per person reached, or None when the numbers aren't there."""
    reach = _number(values, "reach")
    if not reach:
        return None
    if platform == ContentPostVariant.Platform.INSTAGRAM:
        total = _number(values, "interactions")
        if total is None:
            total = _sum(values, ("likes", "comments", "saved", "shares"))
    else:
        total = _sum(values, ("reactions", "comments", "shares"))
    return None if total is None else total / reach


def _sum(values: dict, keys) -> float | None:
    parts = [n for n in (_number(values, k) for k in keys) if n is not None]
    return sum(parts) if parts else None


def best_times(account, platform: str, now=None) -> dict:
    """When this channel's posts have reached people best, by time of week.

    Uses each published post's latest numbers (stories left out: they work
    differently), from posts out for at least ``BEST_SETTLED`` within ``BEST_WINDOW``.
    Engagement is interactions per person reached, so a post isn't favoured just for
    being seen by more people. Below ``BEST_MIN_POSTS`` measured posts nothing is
    suggested; a time of week needs ``BEST_MIN_PER_TIME`` posts of its own.
    """
    now = now or timezone.now()
    tz = _tz(account)
    variants = {
        v.pk: v
        for v in ContentPostVariant.objects.filter(
            post__account=account,
            platform=platform,
            status=ContentPostVariant.Status.PUBLISHED,
            published_at__gte=now - BEST_WINDOW,
            published_at__lte=now - BEST_SETTLED,
        ).exclude(post__media_type=ContentPost.MediaType.STORY)
    }
    latest: dict[int, dict] = {}
    for snap in VariantMetricSnapshot.objects.filter(
        variant_id__in=list(variants)
    ).order_by("variant_id", "-collected_at"):
        latest.setdefault(snap.variant_id, snap.values or {})

    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    rates = []
    for pk, values in latest.items():
        rate = _engagement(platform, values)
        if rate is None:
            continue
        local = timezone.localtime(variants[pk].published_at, tz)
        buckets[(local.weekday(), local.hour // BLOCK_HOURS)].append(rate)
        rates.append(rate)

    out: dict[str, Any] = {
        "platform": platform,
        "measured": len(rates),
        "needed": BEST_MIN_POSTS,
        "enough": len(rates) >= BEST_MIN_POSTS,
        "top": [],
        "typical": None,
    }
    if not out["enough"]:
        return out
    out["typical"] = round(statistics.median(rates) * 100, 1)
    ranked = sorted(
        (
            (statistics.median(r), len(r), key)
            for key, r in buckets.items()
            if len(r) >= BEST_MIN_PER_TIME
        ),
        reverse=True,
    )
    for rate, n, (weekday, block) in ranked[:3]:
        start = block * BLOCK_HOURS
        out["top"].append(
            {
                "weekday": weekday,
                "day": DAY_NAMES[weekday],
                "start_hour": start,
                "label": f"{start:02d}:00 to {start + BLOCK_HOURS:02d}:00",
                "percent": round(rate * 100, 1),
                "posts": n,
            }
        )
    return out
