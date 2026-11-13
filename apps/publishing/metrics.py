"""Which performance numbers Content Studio collects, per channel and post type.

The one source of truth for metric names, labels, definitions and the permission each
needs. Collectors ask the registry what to request; the UI uses it for labels and
definitions. Verified against Meta's docs for Graph API v25.0 (2026-10-18):

- Instagram, Instagram Login (graph.instagram.com), ``/{media-id}/insights``: likes,
  comments, reach, views, saved, shares, total_interactions on feed posts and reels;
  reach, views, shares, replies, total_interactions on stories. ``impressions`` is
  retired for media created after 2024-07-02. Missing data comes back as an empty
  data set, not 0. Story insights last 24 hours, and a story with under 5 viewers
  returns error 10 "Not enough viewers".
- Facebook Page posts, ``/{post-id}/insights`` (read_insights + pages_read_engagement):
  post_media_view (views), post_total_media_view_unique (reach) and
  post_reactions_by_type_total. The ``post_impressions*`` metrics were removed on every
  version in 2025. Comments and shares are post fields.

A metric that can't be read is shown as "—" with the reason, never as 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

IG_BASIC = "instagram_business_basic"
IG_INSIGHTS = "instagram_business_manage_insights"
FB_ENGAGEMENT = "pages_read_engagement"
FB_INSIGHTS = "read_insights"

# Where a post lives on its channel, which decides the metrics it has.
FEED, REELS, STORY, FB_POST = "feed", "reels", "story", "fb_post"
# A Facebook Page story (Phase 0a). No metrics are read for it yet: Meta's docs don't say
# how long story insights stay readable, so nothing is claimed until that is verified.
FB_STORY = "fb_story"


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    definition: str
    platform: str
    surfaces: frozenset[str]
    source: str  # "field" (on the media/post object) or "insights"
    graph_name: str
    scope: str


def _m(key, label, definition, platform, surfaces, source, graph_name, scope):
    return MetricDef(
        key, label, definition, platform, frozenset(surfaces), source, graph_name, scope
    )


REGISTRY: tuple[MetricDef, ...] = (
    # Instagram
    _m(
        "reach",
        "Reach",
        "Accounts that saw this post at least once. Estimated by Instagram.",
        "instagram",
        (FEED, REELS, STORY),
        "insights",
        "reach",
        IG_INSIGHTS,
    ),
    _m(
        "views",
        "Views",
        "Times this post was played or shown on Instagram, including repeat views.",
        "instagram",
        (FEED, REELS, STORY),
        "insights",
        "views",
        IG_INSIGHTS,
    ),
    _m(
        "likes",
        "Likes",
        "Likes on this post.",
        "instagram",
        (FEED, REELS),
        "field",
        "like_count",
        IG_BASIC,
    ),
    _m(
        "comments",
        "Comments",
        "Comments on this post.",
        "instagram",
        (FEED, REELS),
        "field",
        "comments_count",
        IG_BASIC,
    ),
    _m(
        "saved",
        "Saves",
        "Times someone saved this post.",
        "instagram",
        (FEED, REELS),
        "insights",
        "saved",
        IG_INSIGHTS,
    ),
    _m(
        "shares",
        "Shares",
        "Times someone shared this post.",
        "instagram",
        (FEED, REELS, STORY),
        "insights",
        "shares",
        IG_INSIGHTS,
    ),
    _m(
        "replies",
        "Replies",
        "Replies to this story. Instagram doesn't count replies from Europe or Japan.",
        "instagram",
        (STORY,),
        "insights",
        "replies",
        IG_INSIGHTS,
    ),
    _m(
        "interactions",
        "Interactions",
        "Likes, saves, comments and shares, minus any that were undone.",
        "instagram",
        (FEED, REELS, STORY),
        "insights",
        "total_interactions",
        IG_INSIGHTS,
    ),
    # Facebook
    _m(
        "reach",
        "Reach",
        "People who saw this post at least once. Estimated by Facebook.",
        "facebook",
        (FB_POST,),
        "insights",
        "post_total_media_view_unique",
        FB_INSIGHTS,
    ),
    _m(
        "views",
        "Views",
        "Times this post was played or shown, including repeat views and ads.",
        "facebook",
        (FB_POST,),
        "insights",
        "post_media_view",
        FB_INSIGHTS,
    ),
    _m(
        "reactions",
        "Reactions",
        "Likes and other reactions on this post.",
        "facebook",
        (FB_POST,),
        "insights",
        "post_reactions_by_type_total",
        FB_INSIGHTS,
    ),
    _m(
        "comments",
        "Comments",
        "Comments on this post.",
        "facebook",
        (FB_POST,),
        "field",
        "comments.summary(true).limit(0)",
        FB_ENGAGEMENT,
    ),
    _m(
        "shares",
        "Shares",
        "Times this post was shared.",
        "facebook",
        (FB_POST,),
        "field",
        "shares",
        FB_ENGAGEMENT,
    ),
)


# Account-level numbers, one day at a time (Phase 2b, apps.publishing.account_metrics).
# Verified with Meta's docs (developer docs MCP, 2026-11-01):
# - Instagram, ``/{ig-user-id}/insights`` with ``period=day&metric_type=total_value``
#   and since/until for the day: reach, views, accounts_engaged, total_interactions
#   (instagram_business_manage_insights). Data can be up to 48 hours late. Followers
#   are the ``followers_count`` field (instagram_business_basic).
# - Facebook, ``/{page-id}/insights`` ``period=day``: page_total_media_view_unique
#   (reach) and page_media_view (views), read_insights. Several Page metrics were
#   retired on 15 June 2026 (marked in Meta's table; the mark isn't explained), so only
#   unmarked ones are used. Page insights need 100+ likes. Followers are the Page's
#   ``followers_count`` field (pages_read_engagement).
ACCOUNT = "account"
ACCOUNT_REGISTRY: tuple[MetricDef, ...] = (
    _m(
        "followers",
        "Followers",
        "Accounts following you when the numbers were read.",
        "instagram",
        (ACCOUNT,),
        "field",
        "followers_count",
        IG_BASIC,
    ),
    _m(
        "reach",
        "Reach",
        "Accounts that saw any of your content that day. Estimated by Instagram.",
        "instagram",
        (ACCOUNT,),
        "insights",
        "reach",
        IG_INSIGHTS,
    ),
    _m(
        "views",
        "Views",
        "Times your content was played or shown that day.",
        "instagram",
        (ACCOUNT,),
        "insights",
        "views",
        IG_INSIGHTS,
    ),
    _m(
        "engaged",
        "Accounts engaged",
        "Accounts that liked, saved, commented on, shared or replied to your content "
        "that day. Estimated by Instagram.",
        "instagram",
        (ACCOUNT,),
        "insights",
        "accounts_engaged",
        IG_INSIGHTS,
    ),
    _m(
        "interactions",
        "Interactions",
        "Likes, saves, comments, shares and replies on your content that day.",
        "instagram",
        (ACCOUNT,),
        "insights",
        "total_interactions",
        IG_INSIGHTS,
    ),
    _m(
        "followers",
        "Followers",
        "People following your Page when the numbers were read.",
        "facebook",
        (ACCOUNT,),
        "field",
        "followers_count",
        FB_ENGAGEMENT,
    ),
    _m(
        "reach",
        "Reach",
        "People who saw your Page's content that day. Estimated by Facebook.",
        "facebook",
        (ACCOUNT,),
        "insights",
        "page_total_media_view_unique",
        FB_INSIGHTS,
    ),
    _m(
        "views",
        "Views",
        "Times your Page's content was played or shown that day, including ads.",
        "facebook",
        (ACCOUNT,),
        "insights",
        "page_media_view",
        FB_INSIGHTS,
    ),
)


def account_metrics_for(platform: str) -> list[MetricDef]:
    return [m for m in ACCOUNT_REGISTRY if m.platform == platform]


class State:
    AVAILABLE = "available"
    PENDING = "pending"  # the channel has no number yet (new post, or still processing)
    UNSUPPORTED = "unsupported"  # this post type or account doesn't have this metric
    NEEDS_PERMISSION = "needs_permission"  # reconnect and grant the permission
    BELOW_THRESHOLD = "below_threshold"  # stories with under 5 viewers
    FAILED = "failed"
    # Never stored: shown in place of PENDING once a read made after NOT_REPORTED_AFTER
    # still had no number (see effective_state), so an old post doesn't say "wait".
    NOT_REPORTED = "not_reported"


# Meta says new numbers can take up to 48 hours. A read later than this that still has
# no number means the channel isn't reporting it for this post, not that it's coming.
NOT_REPORTED_AFTER = timedelta(hours=48)


STATE_REASONS = {
    State.PENDING: "No number from {channel} yet. New posts can take up to 48 hours.",
    State.UNSUPPORTED: "{channel} doesn't report this for this kind of post.",
    State.NEEDS_PERMISSION: "Reconnect {channel} and allow access to insights.",
    State.BELOW_THRESHOLD: "{channel} shows story numbers once 5 people have viewed it.",
    State.FAILED: "Couldn't read this from {channel}. We'll try again.",
    State.NOT_REPORTED: "{channel} hasn't returned this number for this post.",
}


def effective_state(state, published_at, collected_at) -> str:
    """The state to show for a stored one. Every screen that presents a metric's state
    goes through this, so they never disagree. A missing state is PENDING; PENDING on a
    read made more than NOT_REPORTED_AFTER after publish is NOT_REPORTED."""
    state = state or State.PENDING
    if (
        state == State.PENDING
        and published_at
        and collected_at
        and collected_at - published_at > NOT_REPORTED_AFTER
    ):
        return State.NOT_REPORTED
    return state


def last_known(variant, key: str):
    """``(value, collected_at)`` from the newest read that had a number for ``key``, or
    ``(None, None)``. The latest read can fail; what was last observed stays a fact."""
    from .models import VariantMetricSnapshot

    for snap in VariantMetricSnapshot.objects.filter(variant=variant).only(
        "values", "states", "collected_at"
    ):  # newest first (Meta ordering)
        value = (snap.values or {}).get(key)
        if isinstance(value, int | float) and (snap.states or {}).get(
            key, State.AVAILABLE
        ) == State.AVAILABLE:
            return value, snap.collected_at
    return None, None


def surface_for(platform: str, media_type: str) -> str:
    if platform == "facebook":
        from .constraints import fb_formats_released

        if media_type == "story" and fb_formats_released():
            return FB_STORY
        return FB_POST
    if media_type == "story":
        return STORY
    if media_type in ("reel", "video"):
        return REELS  # Content Studio publishes Instagram videos as reels
    return FEED


def metrics_for(platform: str, media_type: str) -> list[MetricDef]:
    surface = surface_for(platform, media_type)
    return [m for m in REGISTRY if m.platform == platform and surface in m.surfaces]


def label_for(platform: str, key: str) -> tuple[str, str]:
    for m in REGISTRY:
        if m.platform == platform and m.key == key:
            return m.label, m.definition
    return key.replace("_", " ").capitalize(), ""


# When to read the numbers again, counted from publish. Numbers settle within weeks, so
# collection stops after 28 days; stories stop when their insights expire at 24 hours.
CADENCE = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
    timedelta(days=28),
)
STORY_CADENCE = (timedelta(hours=1), timedelta(hours=12), timedelta(hours=23))


def next_collection_at(published_at, media_type: str, now):
    """The next scheduled read after ``now``, or None when collection is finished."""
    cadence = STORY_CADENCE if media_type == "story" else CADENCE
    for offset in cadence:
        at = published_at + offset
        if at > now:
            return at
    return None


def stale_after(published_at, media_type: str, collected_at) -> bool:
    """True when the last read is older than twice the gap to the next planned read."""
    from django.utils import timezone

    now = timezone.now()
    nxt = next_collection_at(published_at, media_type, collected_at)
    if nxt is None:
        return False
    return now > collected_at + 2 * (nxt - collected_at)


def performance_for(variant) -> dict:
    """Everything the Performance card shows for one published channel.

    Each row is a metric with its number, or "—" and the reason there is none.
    """
    channel = variant.get_platform_display()
    media_type = variant.post.media_type
    latest = variant.metrics_latest or {}
    values = latest.get("values") or {}
    states = latest.get("states") or {}
    collected = variant.metrics_collected_at
    rows = []
    for m in metrics_for(variant.platform, media_type):
        state = states.get(m.key, State.PENDING)
        value = values.get(m.key)
        if value is None and state == State.AVAILABLE:
            state = State.PENDING
        state = effective_state(state, variant.published_at, collected)
        # The latest read failed: keep showing what was last observed, dated.
        as_of = None
        if value is None and state in (State.FAILED, State.NEEDS_PERMISSION):
            value, as_of = last_known(variant, m.key)
        rows.append(
            {
                "key": m.key,
                "label": m.label,
                "definition": m.definition,
                "value": value,
                "state": state,
                "as_of": as_of,
                "reason": ""
                if value is not None
                else STATE_REASONS.get(state, "").format(channel=channel),
            }
        )
    # One line per reason ("Saves, Shares: …"); permission has its own reconnect prompt.
    notes: dict[str, list[str]] = {}
    for r in rows:
        if r["reason"] and r["state"] != State.NEEDS_PERMISSION:
            notes.setdefault(r["reason"], []).append(r["label"])
    return {
        "channel": channel,
        "rows": rows,
        "notes": [
            {"labels": ", ".join(labels), "reason": reason}
            for reason, labels in notes.items()
        ],
        "collected_at": collected,
        "first_read_at": variant.metrics_next_at if collected is None else None,
        "stale": bool(
            collected
            and variant.published_at
            and stale_after(variant.published_at, media_type, collected)
        ),
        "needs_permission": any(r["state"] == State.NEEDS_PERMISSION for r in rows),
        "error": latest.get("error", ""),
        "finished": collected is not None and variant.metrics_next_at is None,
        # Some numbers come from an earlier read because the latest one failed.
        "from_earlier": any(r["as_of"] for r in rows),
    }
