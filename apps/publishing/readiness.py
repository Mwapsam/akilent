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

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import constraints as limits
from .composer import (
    CONNECTED,
    PLATFORM_LABELS,
    RECONNECT,
    ComposerError,
    ComposerInput,
)

SUBMIT_REVIEW, SCHEDULE, PUBLISH_NOW = "submit_review", "schedule", "publish_now"
GATED_ACTIONS = (SUBMIT_REVIEW, SCHEDULE, PUBLISH_NOW)
_CONTENT = [SUBMIT_REVIEW, SCHEDULE, PUBLISH_NOW]

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
            if "facebook" in inp.platforms:
                issues.append(
                    _issue(
                        "fb_first_image_only",
                        "Facebook publishes the first image only.",
                        "media",
                        platform="facebook",
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
            issues.append(
                _issue(
                    "fb_story",
                    "Facebook publishes this story as a regular post.",
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


def _connection_issues(inp: ComposerInput, dests: dict) -> list[dict]:
    issues = []
    for p in inp.platforms:
        d = dests.get(p)
        if d is None or d["status"] == CONNECTED:
            continue
        label = PLATFORM_LABELS[p]
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
) -> list[dict]:
    """Every problem with the post in ``inp``. Pass ``error`` when the form couldn't be
    read; it blocks everything until fixed."""
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

    issues = _content_issues(inp)
    issues += _connection_issues(
        inp, dests if dests is not None else destinations(account)
    )
    issues += _schedule_issues(inp, now or timezone.now())

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
