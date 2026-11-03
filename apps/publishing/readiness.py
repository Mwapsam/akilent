"""Whether a post is ready to go out, checked before the user presses Publish.

``check`` returns every problem with what it blocks. The composer asks for it as the
user edits (``composer_readiness``), and ``post_new`` runs the same check before it
schedules, publishes or submits a post, so the page and the server can't disagree.
``publish_variant`` keeps its own checks as the final authority at publish time.

An issue blocks the actions in its ``blocks`` list and is only a warning for the rest:
- content problems (no media, caption too long, wrong file type) block everything
  except saving a draft;
- a channel that isn't connected blocks publishing now; a scheduled post only warns,
  since it can be reconnected before the time;
- a missing or past time blocks scheduling.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import constraints as limits
from .composer import (
    CONNECTED,
    OFF,
    PLATFORM_LABELS,
    RECONNECT,
    ComposerError,
    ComposerInput,
)

SUBMIT_REVIEW, SCHEDULE, PUBLISH_NOW = "submit_review", "schedule", "publish_now"
GATED_ACTIONS = (SUBMIT_REVIEW, SCHEDULE, PUBLISH_NOW)
_CONTENT = [SUBMIT_REVIEW, SCHEDULE, PUBLISH_NOW]
# How long a Facebook story reuse answer is kept while someone edits (seconds).
STORY_REUSE_CACHE_SECONDS = 60

_TYPE_NAMES = {
    limits.IMAGE: "An image post",
    limits.VIDEO: "A video post",
    limits.REEL: "A Reel",
    limits.STORY: "A story",
    limits.CAROUSEL: "A carousel",
    limits.TEXT: "A text post",
}
_KIND_NEEDED = {
    limits.IMAGE: "an image",
    limits.CAROUSEL: "images",
    limits.VIDEO: "a video",
    limits.REEL: "a video",
    limits.STORY: "an image or a video",
}

# Allow for the seconds between picking a time and pressing the button.
_PAST_GRACE = timedelta(minutes=1)


def _issue(code, message, field, blocks=(), platform="", fix_url="", warns=None):
    """``blocks``: the actions this stops. ``warns``: the actions it is shown on as a
    warning; by default a non-blocking issue warns on every action."""
    blocks = list(blocks)
    if warns is None:
        warns = [] if blocks else list(GATED_ACTIONS)
    return {
        "code": code,
        "level": "error" if blocks else "warning",
        "message": message,
        "field": field,
        "platform": platform,
        "blocks": blocks,
        "warns": list(warns),
        "fix_url": fix_url,
    }


def blocking(issues: list[dict], action: str) -> list[dict]:
    return [i for i in issues if action in i["blocks"]]


def published_caption_length(inp: ComposerInput, platform: str) -> int:
    """The caption's length as published: text, the CTA line, then hashtags."""
    from .links import CTA_LINK, CTA_WHATSAPP, INSTAGRAM_SUFFIX

    parts = [inp.platform_captions.get(platform) or inp.caption]
    cta = inp.cta or {}
    kind, text = cta.get("kind"), cta.get("text", "")
    if kind == CTA_LINK or (kind == CTA_WHATSAPP and inp.wa_links.get(platform)):
        if platform == "instagram":
            parts.append(f"{text}{INSTAGRAM_SUFFIX}")
        elif kind == CTA_LINK:
            parts.append(f"{text}: {cta.get('url', '')}")
        else:
            # The short link isn't made until the post is saved; it is the site
            # address plus /go/ and a six-character code.
            site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
            parts.append(f"{text}: {site}/go/XXXXXX")
    if inp.hashtags:
        parts.append(" ".join(f"#{t}" for t in inp.hashtags))
    return len("\n\n".join(p for p in parts if p))


def _content_issues(inp: ComposerInput) -> list[dict]:
    issues = []
    mt, urls = inp.media_type, inp.media_urls
    type_name = _TYPE_NAMES.get(mt, "This post")

    for p in inp.platforms:
        if mt not in limits.MEDIA_TYPES.get(p, set()):
            issues.append(
                _issue(
                    "type_unsupported",
                    f"{PLATFORM_LABELS[p]} can't publish a text-only post. Add an image "
                    f"or video, or untick {PLATFORM_LABELS[p]}.",
                    "media_type",
                    _CONTENT,
                    platform=p,
                )
            )

    if mt == limits.TEXT:
        if not inp.caption and not any(inp.platform_captions.values()):
            issues.append(
                _issue(
                    "caption_missing",
                    "Write a caption for a text post.",
                    "caption",
                    _CONTENT,
                )
            )
        if urls:
            issues.append(
                _issue(
                    "media_ignored",
                    "Text posts are published without media. Change the post type to "
                    "include your files.",
                    "media_type",
                )
            )
    elif not urls:
        issues.append(
            _issue(
                "media_missing",
                f"{type_name} needs {_KIND_NEEDED.get(mt, 'media')}. Add a file.",
                "media",
                _CONTENT,
            )
        )
    else:
        allowed = limits.MEDIA_KIND.get(mt, set())
        wrong = [u for u in urls if (k := limits.media_kind(u)) and k not in allowed]
        if wrong:
            issues.append(
                _issue(
                    "media_kind",
                    f"{type_name} needs {_KIND_NEEDED.get(mt, 'other media')}. Remove "
                    f"{'that file' if len(wrong) == 1 else 'those files'} or change the "
                    "post type.",
                    "media",
                    _CONTENT,
                )
            )
        if mt == limits.CAROUSEL:
            if not limits.CAROUSEL_MIN <= len(urls) <= limits.CAROUSEL_MAX:
                issues.append(
                    _issue(
                        "carousel_count",
                        f"A carousel takes {limits.CAROUSEL_MIN} to {limits.CAROUSEL_MAX} images; "
                        f"this one has {len(urls)}.",
                        "media",
                        _CONTENT,
                    )
                )
        elif len(urls) > 1:
            issues.append(
                _issue(
                    "extra_media",
                    "Only the first file is published. Switch to Carousel to post them all.",
                    "media",
                )
            )

    if mt == limits.STORY:
        if "instagram" in inp.platforms and (
            inp.caption or inp.platform_captions.get("instagram")
        ):
            issues.append(
                _issue(
                    "story_caption",
                    "Instagram stories don't show a caption.",
                    "caption",
                    platform="instagram",
                )
            )

    if "facebook" in inp.platforms:
        if limits.facebook_format_blocked(mt):
            issues.append(
                _issue(
                    "fb_format_unsupported",
                    limits.FB_UNSUPPORTED_MESSAGE,
                    "media_type",
                    _CONTENT,
                    platform="facebook",
                )
            )
        elif mt == limits.REEL and not limits.fb_formats_released():
            issues.append(
                _issue(
                    "fb_reel_as_video",
                    limits.FB_REEL_AS_VIDEO_MESSAGE,
                    "media_type",
                    platform="facebook",
                )
            )

    for p in inp.platforms:
        limit = limits.CAPTION_MAX.get(p)
        length = published_caption_length(inp, p)
        if limit and length > limit:
            issues.append(
                _issue(
                    "caption_too_long",
                    f"The {PLATFORM_LABELS[p]} caption is {length - limit:,} characters "
                    f"over its {limit:,} limit (counting the call to action and hashtags).",
                    f"caption_{p}" if inp.platform_captions.get(p) else "caption",
                    _CONTENT,
                    platform=p,
                )
            )
        tag_limit = limits.HASHTAGS_MAX.get(p)
        if tag_limit and len(inp.hashtags) > tag_limit:
            issues.append(
                _issue(
                    "too_many_hashtags",
                    f"{PLATFORM_LABELS[p]} allows up to {tag_limit} hashtags; this post "
                    f"has {len(inp.hashtags)}.",
                    "hashtags",
                    _CONTENT,
                    platform=p,
                )
            )
    return issues


MEDIA_CHECKING = "Checking your video. This takes a moment."
MEDIA_CHECKING_SLOW = (
    "Media checks are running slowly. You can save as a draft now and schedule once "
    "the check finishes."
)


def _media_issues(
    inp: ComposerInput, account, now, start_checks: bool = True
) -> list[dict]:
    """Phase 0a: what the server-side checks say (apps.publishing.media_checks). A check
    still running or failed blocks; a video outside a channel's limits blocks that
    channel's post; a Facebook story can't reuse media this Page already published.

    ``start_checks=False`` only reads: a file with no check yet counts as being checked,
    and nothing is saved or queued (the CSV preview)."""
    from . import media_checks
    from .direct_upload import is_unconfirmed
    from .models import MediaCheck

    issues = []
    mt = inp.media_type
    fb_story = "facebook" in inp.platforms and mt == limits.STORY
    checked = {}
    for url in inp.media_urls:
        if is_unconfirmed(url):
            # A direct upload never confirmed (Phase 0c): it is deleted within a day.
            issues.append(
                _issue(
                    "media_unconfirmed",
                    media_checks.UNCONFIRMED_MESSAGE,
                    "media",
                    _CONTENT,
                )
            )
            continue
        if limits.media_kind(url) != "video" and not fb_story:
            continue
        check = (
            media_checks.ensure(account, url)
            if start_checks
            else media_checks.find(account, url)
        )
        if check is None or check.status == MediaCheck.Status.PENDING:
            slow = check is not None and media_checks.is_slow(check, now)
            issues.append(
                _issue(
                    "media_checking",
                    MEDIA_CHECKING_SLOW if slow else MEDIA_CHECKING,
                    "media",
                    _CONTENT,
                )
            )
        elif check.status == MediaCheck.Status.FAILED:
            issues.append(
                _issue(
                    "media_check_failed",
                    f"We couldn't check this file: {check.error}",
                    "media",
                    _CONTENT,
                )
            )
        else:
            checked[url] = check

    type_name = _TYPE_NAMES.get(mt, "This post")
    for check in checked.values():
        for p in inp.platforms:
            problems = media_checks.violations(check, p, mt)
            if problems:
                issues.append(
                    _issue(
                        "video_limits",
                        f"{type_name} on {PLATFORM_LABELS[p]} must be "
                        f"{', '.join(problems)}.",
                        "media",
                        _CONTENT,
                        platform=p,
                    )
                )

    if fb_story and checked:
        if _already_published_on_facebook(
            account, {c.sha256 for c in checked.values()}
        ):
            issues.append(
                _issue(
                    "fb_story_reused",
                    "Facebook doesn't allow a story with a photo or video already used "
                    "in a published post. Use a new file for the story.",
                    "media",
                    _CONTENT,
                    platform="facebook",
                )
            )

    if "facebook" in inp.platforms and mt == limits.REEL:
        if _fb_reels_last_day(account, now) >= limits.FB_REELS_PER_DAY:
            issues.append(
                _issue(
                    "fb_reel_daily_limit",
                    f"This Page has sent {limits.FB_REELS_PER_DAY} Reels through Akilent "
                    "in the last 24 hours, Facebook's daily limit. This one waits and "
                    "goes out when Facebook allows it.",
                    "media_type",
                    platform="facebook",
                )
            )
    return issues


def _already_published_on_facebook(account, hashes: set[str]) -> bool:
    """Whether any of ``hashes`` is a file in a post already published on Facebook.

    Starts from the (indexed) hash: only the few files with that content are looked for
    among published Facebook posts, so the composer's repeated checks stay cheap."""
    from django.db.models import Max, Q

    from .models import ContentPostVariant, MediaCheck

    hashes = {h for h in hashes if h}
    if not hashes:
        return False
    urls = set(
        MediaCheck.objects.filter(account=account, sha256__in=hashes).values_list(
            "url", flat=True
        )
    )
    if not urls:
        return False
    published = ContentPostVariant.objects.filter(
        post__account=account,
        platform=ContentPostVariant.Platform.FACEBOOK,
        status=ContentPostVariant.Status.PUBLISHED,
    )
    # The composer asks again on every edit, so the answer is kept for a minute. The key
    # holds the latest Facebook publish, so anything published since asks afresh.
    latest = published.aggregate(latest=Max("published_at"))["latest"]
    digest = hashlib.sha256(" ".join(sorted(urls)).encode()).hexdigest()
    version = int(latest.timestamp() * 1_000_000) if latest else 0
    key = f"fb-story-reuse:{account.pk}:{version}:{digest}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    match = Q()
    for url in urls:
        match |= Q(media_urls__icontains=url) | Q(post__media_urls__icontains=url)
    candidates = published.filter(match).select_related("post")
    # The channel's own media, or the post's when it has none, as publishing uses them.
    reused = any(
        u in urls for v in candidates for u in (v.media_urls or v.post.media_urls or [])
    )
    cache.set(key, reused, STORY_REUSE_CACHE_SECONDS)
    return reused


def _ig_ratio_issues(inp: ComposerInput, account) -> list[dict]:
    """Instagram refuses pictures outside 4:5 portrait to 1.91:1 landscape. When the media
    library knows a picture's size, say so now, instead of failing at publish time."""
    from .media_library import url_digest
    from .models import MediaAsset

    if "instagram" not in inp.platforms or inp.media_type not in (
        limits.IMAGE,
        limits.CAROUSEL,
    ):
        return []
    urls = inp.media_urls if inp.media_type == limits.CAROUSEL else inp.media_urls[:1]
    if not urls:
        return []
    sizes = {
        a.url: (a.width, a.height)
        for a in MediaAsset.objects.filter(
            account=account, url_digest__in=[url_digest(u) for u in urls]
        )
        if a.width and a.height
    }
    bad = [
        (w, h)
        for u in urls
        if (size := sizes.get(u))
        for w, h in [size]
        if not limits.IG_RATIO_MIN <= w / h <= limits.IG_RATIO_MAX
    ]
    if not bad:
        return []
    w, h = bad[0]
    return [
        _issue(
            "ig_ratio",
            f"Instagram takes pictures from 4:5 portrait to 1.91:1 landscape; this one is "
            f"{w} by {h} pixels. Crop it (in Media → Edit, or upload it again).",
            "media",
            _CONTENT,
            platform="instagram",
        )
    ]


def _fb_reels_last_day(account, now) -> int:
    from .models import ContentPost, ContentPostVariant

    return ContentPostVariant.objects.filter(
        post__account=account,
        post__media_type=ContentPost.MediaType.REEL,
        platform=ContentPostVariant.Platform.FACEBOOK,
        status__in=(
            ContentPostVariant.Status.PUBLISHED,
            ContentPostVariant.Status.PROCESSING,
            ContentPostVariant.Status.PUBLISHING,
        ),
        last_attempt_at__gte=now - timedelta(hours=24),
    ).count()


def _connection_issues(inp: ComposerInput, dests: dict) -> list[dict]:
    issues = []
    for p in inp.platforms:
        d = dests.get(p)
        if d is None or d["status"] == CONNECTED:
            continue
        label = PLATFORM_LABELS[p]
        if d["status"] == OFF:
            issues.append(
                _issue(
                    f"{p}_off",
                    f"{label} publishing is switched off on Akilent right now. A "
                    "scheduled post waits and goes out once it's back on.",
                    "destinations",
                    [PUBLISH_NOW],
                    platform=p,
                    warns=[SCHEDULE, SUBMIT_REVIEW],
                )
            )
            continue
        if d["status"] == RECONNECT:
            message = f"{label} needs reconnecting before it can publish."
        else:
            message = f"{label} isn't connected yet."
        issues.append(
            _issue(
                f"{p}_{d['status']}",
                message,
                "destinations",
                [PUBLISH_NOW],
                platform=p,
                fix_url=d["connect_url"],
                warns=[SCHEDULE, SUBMIT_REVIEW],
            )
        )
    return issues


def _schedule_issues(inp: ComposerInput, now) -> list[dict]:
    if inp.per_channel:
        times = {p: inp.channel_times.get(p) for p in inp.platforms}
    else:
        times = {"": inp.scheduled_at}
    issues = []
    for p, when in times.items():
        field = f"scheduled_at_{p}" if p else "schedule"
        where = f" for {PLATFORM_LABELS[p]}" if p else ""
        if when is None:
            issues.append(
                _issue(
                    "time_missing",
                    f"Pick a date and time{where}.",
                    field,
                    [SCHEDULE],
                    platform=p,
                )
            )
        elif when < now - _PAST_GRACE:
            issues.append(
                _issue(
                    "time_past",
                    f"The time{where} has passed. Pick a time in the future.",
                    field,
                    [SCHEDULE],
                    platform=p,
                )
            )
    return issues


def check(
    inp: ComposerInput | None,
    account,
    *,
    error: ComposerError | None = None,
    dests: dict | None = None,
    wa_ready: bool | None = None,
    now=None,
    start_checks: bool = True,
) -> list[dict]:
    """Every problem with the post in ``inp``. Pass ``error`` when the form couldn't be
    read; it blocks everything until fixed. ``start_checks=False`` reads media checks
    without starting any (see ``_media_issues``)."""
    if error is not None:
        return [_issue("invalid", str(error), error.field or "caption", _CONTENT)]
    if inp is None:
        raise ValueError("check() needs the composer input, or the error reading it")
    if not inp.platforms:
        return [
            _issue(
                "no_destination", "Choose where to publish.", "destinations", _CONTENT
            )
        ]

    from .composer import destinations
    from .links import CTA_WHATSAPP, whatsapp_number_for

    now = now or timezone.now()
    issues = _content_issues(inp)
    issues += _ig_ratio_issues(inp, account)
    if limits.fb_formats_released():
        issues += _media_issues(inp, account, now, start_checks)
    issues += _connection_issues(
        inp, dests if dests is not None else destinations(account)
    )
    issues += _schedule_issues(inp, now)

    if (inp.cta or {}).get("kind") == CTA_WHATSAPP and any(inp.wa_links.values()):
        ready = wa_ready if wa_ready is not None else bool(whatsapp_number_for(account))
        if not ready:
            issues.append(
                _issue(
                    "no_whatsapp_number",
                    "Connect a WhatsApp number to add the chat link.",
                    "cta",
                )
            )
    return issues
