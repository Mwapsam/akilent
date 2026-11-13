"""Visibility diagnosis: has anyone had a chance to see this post yet?

Content Performance Recovery, Phase 1 (docs/strategy/content-performance-recovery.md).
Akilent judges the earliest broken stage first. Nobody judges content or offer until
people have seen the post. This module only reads what Akilent stored, and it states
what was observed, never a cause:

- "zero reported reach" is a number the channel returned. It is not a claim about how
  the channel ranks or shows posts, which Akilent can't see.
- Missing numbers (not read yet, read failed, not reported) are never treated as zero.
- 24 hours is Akilent's own check window, not a promise from Meta.
- Reach is compared only with reads of the same age, from the same channel and kind of
  post (see ``reach_baseline``).
- Account restrictions are always "unknown": Meta doesn't share them with apps.

Behind the ``cs_visibility`` release flag. Nothing here acts on the owner's behalf.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from django.utils import timezone

from .metrics import (
    CADENCE,
    STATE_REASONS,
    STORY,
    State,
    effective_state,
    last_known,
    surface_for,
)
from .models import ContentPostVariant, VariantMetricSnapshot

FLAG = "cs_visibility"

CHECK_WINDOW = timedelta(hours=24)  # Akilent's choice, not a Meta guarantee
BASELINE_WINDOW = timedelta(days=90)
BASELINE_MIN_POSTS = 5
LOW_REACH_SHARE = 0.25  # "well below usual": under a quarter of the median
LOW_REACH_MIN_AGE = timedelta(days=1)

ATTENTION_STAGES = (
    "not_published",
    "post_missing",
    "cant_read",
    "zero_reported_reach",
    "low_reach",
)
OK, PROBLEM, UNKNOWN = "ok", "problem", "unknown"

_BUCKET_LABELS = ("1-hour", "6-hour", "1-day", "3-day", "7-day", "28-day")
_SURFACE_LABELS = {"feed": "feed posts", "reels": "reels", "fb_post": "posts"}


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def age_bucket(published_at, collected_at) -> int | None:
    """Which planned read (index into CADENCE) a read made at ``collected_at`` counts
    as: the latest planned offset it had passed. None for a read under an hour old."""
    if not published_at or not collected_at:
        return None
    age = collected_at - published_at
    found = None
    for i, offset in enumerate(CADENCE):
        if age >= offset:
            found = i
    return found


def _reach_read(variant):
    """``(reach, collected_at, state, from_earlier)`` for the read to judge. The latest
    read, or the last read with a number when the latest one failed."""
    latest = variant.metrics_latest or {}
    collected = variant.metrics_collected_at
    value = (latest.get("values") or {}).get("reach")
    state = (latest.get("states") or {}).get("reach")
    if value is not None:
        return value, collected, State.AVAILABLE, False
    state = effective_state(state, variant.published_at, collected)
    if collected and state in (State.FAILED, State.NEEDS_PERMISSION):
        earlier, at = last_known(variant, "reach")
        if earlier is not None:
            return earlier, at, State.AVAILABLE, True
    return None, collected, state, False


def reach_baseline(variant, bucket: int | None, now=None) -> dict | None:
    """The account's usual reach for reads of this age: the median of the same-age reach
    reads of its other published posts on the same channel and kind of post in the last
    90 days. None below BASELINE_MIN_POSTS such posts, or for stories."""
    if bucket is None:
        return None
    post = variant.post
    surface = surface_for(variant.platform, post.media_type)
    if surface == STORY or surface not in _SURFACE_LABELS:
        return None
    now = now or timezone.now()
    others = {
        v.pk: v
        for v in ContentPostVariant.objects.filter(
            post__account_id=post.account_id,
            platform=variant.platform,
            status=ContentPostVariant.Status.PUBLISHED,
            published_at__gte=now - BASELINE_WINDOW,
        )
        .exclude(pk=variant.pk)
        .select_related("post")
        if surface_for(v.platform, v.post.media_type) == surface
    }
    if len(others) < BASELINE_MIN_POSTS:
        return None
    reach: dict[int, int] = {}
    for snap in VariantMetricSnapshot.objects.filter(
        variant_id__in=list(others)
    ).order_by("variant_id", "-collected_at"):
        if snap.variant_id in reach:
            continue
        value = (snap.values or {}).get("reach")
        if not isinstance(value, int | float):
            continue
        if (snap.states or {}).get("reach", State.AVAILABLE) != State.AVAILABLE:
            continue
        other = others[snap.variant_id]
        if age_bucket(other.published_at, snap.collected_at) == bucket:
            reach[snap.variant_id] = value
    if len(reach) < BASELINE_MIN_POSTS:
        return None
    return {
        "median": round(statistics.median(reach.values())),
        "posts": len(reach),
        "age": _BUCKET_LABELS[bucket],
        "kind": _SURFACE_LABELS[surface],
    }


def _number(values: dict, key: str):
    value = values.get(key)
    return value if isinstance(value, int | float) else None


def _interactions(variant) -> int | None:
    values = (variant.metrics_latest or {}).get("values") or {}
    if variant.platform == ContentPostVariant.Platform.INSTAGRAM:
        total = _number(values, "interactions")
        if total is not None:
            return int(total)
        keys = ("likes", "comments", "saved", "shares")
    else:
        keys = ("reactions", "comments", "shares")
    parts = [n for n in (_number(values, k) for k in keys) if n is not None]
    return int(sum(parts)) if parts else None


def _funnel_condition(reach, interactions, result) -> str:
    """What was observed after people saw it, as plain facts (never a cause)."""
    parts = [f"Seen by {reach:,}"]
    if interactions is None:
        return parts[0]
    if interactions == 0:
        parts.append("no interactions recorded yet")
        return " · ".join(parts)
    parts.append(f"{interactions:,} interaction{'s' if interactions != 1 else ''}")
    if result is None:  # chats from this post aren't tracked (no link, no comment DM)
        return " · ".join(parts)
    chats, orders = result["chats"], result["orders"]
    if not chats:
        parts.append("no chats from this post yet")
    else:
        parts.append(f"{chats} chat{'s' if chats != 1 else ''}")
        parts.append(
            f"{orders} order{'s' if orders != 1 else ''}" if orders else "no orders yet"
        )
    return " · ".join(parts)


def _checks(variant, destination: dict | None, state: str, missing: bool) -> list:
    channel = variant.get_platform_display()
    S = ContentPostVariant.Status
    checks = []

    if variant.status == S.PUBLISHED and variant.publication_id:
        checks.append((OK, f"Published on {channel}", ""))
    elif variant.status == S.FAILED:
        checks.append((PROBLEM, f"Published on {channel}", "Publishing failed."))
    else:
        checks.append(
            (UNKNOWN, f"Published on {channel}", f"{channel} hasn't confirmed it yet.")
        )

    status = (destination or {}).get("status")
    if status == "connected":
        checks.append((OK, "Connection", f"{channel} is connected."))
    elif status in ("reconnect", "not_connected"):
        checks.append(
            (PROBLEM, "Connection", f"Reconnect {channel} so Akilent can read it.")
        )
    else:
        checks.append((UNKNOWN, "Connection", "Couldn't check the connection."))

    if state == State.AVAILABLE:
        checks.append((OK, "Numbers readable for this post", ""))
    elif state == State.PENDING:
        checks.append(
            (
                UNKNOWN,
                "Numbers readable for this post",
                f"No number from {channel} yet.",
            )
        )
    else:
        checks.append(
            (
                PROBLEM,
                "Numbers readable for this post",
                STATE_REASONS.get(state, "").format(channel=channel),
            )
        )

    if missing:
        checks.append(
            (
                PROBLEM,
                "Post still exists",
                f"{channel} says it can't find this post. It may have been deleted.",
            )
        )
    elif variant.metrics_collected_at:
        checks.append(
            (OK, "Post still exists", "The last read didn't report it removed.")
        )
    else:
        checks.append((UNKNOWN, "Post still exists", "Not read yet."))

    checks.append(
        (
            UNKNOWN,
            "Account restrictions",
            f"Meta doesn't share this with apps. Check Account Status in the "
            f"{channel} app.",
        )
    )
    return [{"status": s, "label": label, "detail": d} for s, label, d in checks]


def _recovery_steps(channel: str, zero: bool) -> list[str]:
    steps = [
        f"Open your {channel} profile and check the post shows there.",
        f"Check Account Status in the {channel} app for any restrictions.",
        "Share it to your Story yourself.",
        "Send it to customers who agreed to get WhatsApp messages from you.",
        "Try a different picture or video for the same product.",
    ]
    if zero:
        steps.append(
            "If its reach stays at zero, you could try a small boost in Meta. "
            "You choose and pay Meta directly."
        )
    return steps


def diagnose(variant, destination: dict | None = None, result=None, now=None) -> dict:
    """One channel's visibility stage, what Akilent checked, and manual next steps.

    ``destination`` is this channel's entry from ``composer.destinations``; ``result``
    is its row from ``results.results_for`` (None when chats aren't tracked).
    """
    now = now or timezone.now()
    S = ContentPostVariant.Status
    channel = variant.get_platform_display()
    latest = variant.metrics_latest or {}
    missing = bool(latest.get("post_missing"))
    reach, read_at, state, from_earlier = _reach_read(variant)
    out = {
        "variant": variant,
        "channel": channel,
        "platform": variant.platform,
        "reach": reach,
        "read_at": read_at,
        "from_earlier": from_earlier,
        "baseline": None,
        "possible_factors": "",
        "next_read_at": variant.metrics_next_at,
        "steps": [],
    }

    def done(stage, headline, detail=""):
        out.update(stage=stage, headline=headline, detail=detail)
        out["attention"] = stage in ATTENTION_STAGES
        out["checks"] = _checks(variant, destination, state, missing)
        return out

    if variant.status != S.PUBLISHED or not variant.published_at:
        failed = variant.status == S.FAILED
        out["steps"] = (
            ["Use Edit and fix on this page, then publish it again."] if failed else []
        )
        return done(
            "not_published",
            f"Not live on {channel}" if failed else f"Still going live on {channel}",
            "Reach can only be read once the post is live.",
        )

    if missing:
        out["steps"] = [
            f"Check the post on {channel}. Akilent has stopped reading its numbers."
        ]
        return done("post_missing", f"{channel} can't find this post")

    age = now - variant.published_at
    if reach is None:
        if state in (
            State.NEEDS_PERMISSION,
            State.FAILED,
            State.UNSUPPORTED,
            State.NOT_REPORTED,
        ):
            if state == State.NEEDS_PERMISSION:
                out["steps"] = [f"Reconnect {channel} and allow access to insights."]
            elif state == State.FAILED:
                out["steps"] = ["Akilent will try again at the next read."]
            else:
                out["steps"] = [f"Check this post's insights in the {channel} app."]
            return done(
                "cant_read",
                f"Akilent can't read this post's reach from {channel}",
                "Missing numbers aren't zero, so Akilent doesn't judge this post yet.",
            )
        return done(
            "too_early",
            "Too early to judge reach",
            f"Akilent looks at reach after 24 hours. That's its own check window, "
            f"not a {channel} rule.",
        )

    read_age = read_at - variant.published_at if read_at else age
    if reach == 0:
        if read_age < CHECK_WINDOW:
            return done(
                "too_early",
                "Too early to judge reach",
                f"Akilent looks at reach after 24 hours. That's its own check "
                f"window, not a {channel} rule.",
            )
        out["steps"] = _recovery_steps(channel, zero=True)
        return done(
            "zero_reported_reach",
            "This post has zero reported reach so far",
            f"{channel} reports that nobody has seen it yet. Akilent can't judge the "
            f"content until people have had a chance to see it.",
        )

    if read_age >= LOW_REACH_MIN_AGE:
        baseline = reach_baseline(
            variant, age_bucket(variant.published_at, read_at), now
        )
        out["baseline"] = baseline
        if baseline and reach < LOW_REACH_SHARE * baseline["median"]:
            out["steps"] = _recovery_steps(channel, zero=False)
            return done(
                "low_reach",
                "Reported reach is well below your usual for posts this age",
                f"Seen by {reach:,}. Your usual {baseline['age']} reach on {channel} "
                f"{baseline['kind']} is {baseline['median']:,}, from "
                f"{baseline['posts']} posts.",
            )

    interactions = _interactions(variant)
    if interactions == 0:
        out["possible_factors"] = (
            "Possible factors include a small audience, the post being recent, or "
            "people not having responded yet."
        )
    return done("reached", _funnel_condition(reach, interactions, result))


def for_post(post, account, results=None) -> list[dict] | None:
    """The post page's Visibility card: one diagnosis per Facebook/Instagram channel
    that was meant to go live. None when the flag is off or nothing is to show.
    ``results`` is ``results_for(post)`` when the caller already has it."""
    if not is_released():
        return None
    from .composer import destinations
    from .results import results_for

    S = ContentPostVariant.Status
    variants = [
        v
        for v in post.variants.all()
        if v.platform
        in (ContentPostVariant.Platform.FACEBOOK, ContentPostVariant.Platform.INSTAGRAM)
        and v.status
        in (S.PUBLISHED, S.PUBLISHING, S.PROCESSING, S.RECONCILING, S.FAILED)
    ]
    if not variants:
        return None
    dests = destinations(account)
    rows = results if results is not None else results_for(post)
    results = {r["variant"].pk: r for r in rows}
    return [diagnose(v, dests.get(v.platform), results.get(v.pk)) for v in variants]
