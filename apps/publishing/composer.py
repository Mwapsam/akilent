"""The post composer's server side: reading its form, saving it, and the state of each
destination it can publish to.

``post_new`` (the full submit) and ``composer_autosave`` both save through
``save_post``, so a background save and a button press can't drift apart.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.core.channels import channel_state

from .models import ContentPost, ContentPostVariant, FacebookPageConnection

PLATFORM_LABELS = {"instagram": "Instagram", "facebook": "Facebook"}

# Posts the composer may still change; later states belong to the publishing pipeline.
EDITABLE_STATUSES = (
    ContentPost.Status.IDEA,
    ContentPost.Status.DRAFT,
    ContentPost.Status.IN_REVIEW,
    ContentPost.Status.APPROVED,
    ContentPost.Status.SCHEDULED,
)
# Posts a background save may change. A save always leaves the post a draft, so a
# scheduled or in-review post is only changed by an explicit button press.
AUTOSAVE_STATUSES = (ContentPost.Status.IDEA, ContentPost.Status.DRAFT)

BRIEF_FIELDS = (
    "brief_objective",
    "brief_audience",
    "brief_problem",
    "brief_offer",
    "brief_message",
    "brief_cta",
)

# Variants whose content the composer may still change.
_CHANGEABLE_VARIANTS = (
    ContentPostVariant.Status.DRAFT,
    ContentPostVariant.Status.SCHEDULED,
    ContentPostVariant.Status.FAILED,
)


class ComposerError(ValueError):
    """A value in the composer form isn't valid. ``field`` names the control to fix."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


# --- Destinations -----------------------------------------------------------------------

CONNECTED, RECONNECT, NOT_CONNECTED = "connected", "reconnect", "not_connected"
# The channel is switched off on Akilent (or its state can't be read yet).
OFF = "off"


def destinations(account) -> dict[str, dict]:
    """Each channel's connected account and whether it can publish right now."""
    connect_url = reverse("publishing:channels")
    out = {}

    ig = account.instagram_accounts.filter(is_active=True).first()
    if ig is None:
        ig_state, ig_name = NOT_CONNECTED, ""
    else:
        ig_state = RECONNECT if ig.token_expired or not ig.access_token else CONNECTED
        ig_name = f"@{ig.username.lstrip('@')}" if ig.username else ig.name
    out["instagram"] = {
        "platform": "instagram",
        "label": "Instagram",
        "account_name": ig_name,
        "status": ig_state,
        "connect_url": connect_url,
    }

    conn = FacebookPageConnection.objects.filter(account=account).first()
    if conn is None or not conn.page_id:
        fb_state = NOT_CONNECTED
    elif conn.status == FacebookPageConnection.Status.EXPIRED:
        fb_state = RECONNECT
    elif conn.status == FacebookPageConnection.Status.ACTIVE and conn.access_token:
        fb_state = CONNECTED
    else:
        fb_state = NOT_CONNECTED
    out["facebook"] = {
        "platform": "facebook",
        "label": "Facebook",
        "account_name": conn.page_name if conn and fb_state != NOT_CONNECTED else "",
        "status": fb_state,
        "connect_url": connect_url,
    }
    for platform, dest in out.items():
        if channel_state(platform) != "enabled":
            dest["status"] = OFF
    return out


# --- Reading the form -------------------------------------------------------------------


@dataclass
class ComposerInput:
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    media_type: str = ContentPost.MediaType.IMAGE
    media_urls: list[str] = field(default_factory=list)
    platform_captions: dict[str, str] = field(default_factory=dict)
    scheduled_at: object = None  # aware datetime or None
    channel_times: dict = field(default_factory=dict)  # platform -> aware datetime
    per_channel: bool = False
    cta: dict = field(default_factory=dict)
    wa_links: dict[str, bool] = field(default_factory=dict)  # channel -> link ticked
    channel_options: dict = field(default_factory=dict)
    brief: dict[str, str] = field(default_factory=dict)


def parse_schedule(raw: str):
    """A datetime-local value read as wall-clock time in the active timezone."""
    value = parse_datetime((raw or "").strip()) if raw else None
    if value and timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def _media_urls(data) -> list[str]:
    # A JSON array from the uploader, or newline-separated URLs (older forms).
    raw = (data.get("media_urls_json") or "").strip()
    if raw:
        try:
            return [u for u in json.loads(raw) if isinstance(u, str) and u.strip()]
        except (ValueError, TypeError):
            return []
    return [u.strip() for u in (data.get("media_urls") or "").splitlines() if u.strip()]


def parse_form(data) -> ComposerInput:
    """The composer's fields from ``data`` (a QueryDict). Raises ComposerError."""
    from .links import CtaError, parse_cta
    from .post_options import OptionsError, parse_options

    platforms = data.getlist("platforms")
    media_type = data.get("media_type") or ContentPost.MediaType.IMAGE
    inp = ComposerInput(
        caption=(data.get("caption") or "").strip(),
        hashtags=[
            t.lstrip("#").strip()
            for t in (data.get("hashtags") or "").split()
            if t.strip()
        ],
        platforms=platforms,
        media_type=media_type,
        media_urls=_media_urls(data),
        # A blank override means "use the shared caption", so it isn't stored.
        platform_captions={
            p: o for p in platforms if (o := (data.get(f"caption_{p}") or "").strip())
        },
        scheduled_at=parse_schedule(data.get("scheduled_at", "")),
        wa_links={p: bool(data.get(f"wa_link_{p}")) for p in platforms},
        brief={f: data[f].strip() for f in BRIEF_FIELDS if f in data},
    )

    # Per-channel times: each channel publishes at its own time, and the post's time is
    # the earliest of them.
    if data.get("schedule_mode") == "per_channel" and len(platforms) > 1:
        inp.per_channel = True
        times = {
            p: parse_schedule(data.get(f"scheduled_at_{p}", "")) for p in platforms
        }
        inp.scheduled_at = min(times.values()) if all(times.values()) else None
        inp.channel_times = {p: t for p, t in times.items() if t}

    try:
        inp.cta = parse_cta(data)
    except CtaError as exc:
        raise ComposerError(str(exc), "cta") from None
    try:
        inp.channel_options = parse_options(data, platforms, media_type)
    except OptionsError as exc:
        raise ComposerError(str(exc), "options") from None
    return inp


# --- Finding the post -------------------------------------------------------------------


def parse_client_key(raw) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def find_post(account, post_id, client_key) -> ContentPost | None:
    """The account's post by id, else by the composer's client key."""
    if post_id:
        try:
            return ContentPost.objects.get(pk=int(post_id), account=account)
        except (ContentPost.DoesNotExist, ValueError, TypeError):
            pass
    if client_key:
        return ContentPost.objects.filter(
            account=account, client_key=client_key
        ).first()
    return None


def version_of(post: ContentPost) -> str:
    """The token a background save sends back to show which copy it edited."""
    return post.updated_at.isoformat() if post.pk and post.updated_at else ""


# --- Saving -----------------------------------------------------------------------------


@dataclass
class SaveResult:
    post: ContentPost
    review_blocked: bool = False
    missing_wa_number: bool = False
    # Another request saved this client key first; this one changed nothing.
    duplicate: bool = False


def status_for(action: str, inp: ComposerInput):
    if action == "schedule" and inp.scheduled_at:
        return ContentPost.Status.SCHEDULED
    if action == "publish_now":
        return ContentPost.Status.PUBLISHING
    return ContentPost.Status.DRAFT


def _apply_cta(post, cta: dict, data) -> bool:
    """Save the post's call to action and add its line to each channel's caption.

    A WhatsApp CTA creates (or turns off) each ticked channel's tracked link and sets
    the customer's first message. Facebook gets a tappable link in the caption;
    Instagram gets the wording with "(link in bio)". Returns True when a WhatsApp link
    was asked for but the workspace has no WhatsApp number.
    """
    from .links import (
        CTA_WHATSAPP,
        NoWhatsAppNumber,
        cta_caption,
        disable_link,
        link_for_variant,
    )

    if post.cta != cta:
        post.cta = cta
        post.save(update_fields=["cta", "updated_at"])

    missing_number = False
    for variant in post.variants.filter(
        platform__in=post.platforms, status__in=_CHANGEABLE_VARIANTS
    ):
        link = None
        if cta.get("kind") == CTA_WHATSAPP and data.get(f"wa_link_{variant.platform}"):
            try:
                link = link_for_variant(variant)
            except NoWhatsAppNumber:
                missing_number = True
            else:
                if link.prefill_text != cta["prefill"]:
                    link.prefill_text = cta["prefill"]
                    link.save(update_fields=["prefill_text"])
        else:
            disable_link(variant)
        caption = cta_caption(variant.caption, cta, variant.platform, link)
        if caption != variant.caption:
            variant.caption = caption
            variant.save(update_fields=["caption"])
    return missing_number


def _apply_channel_options(post, channel_options: dict) -> None:
    """Save each channel's options (alt text, location, first comment, …) on the
    channels that can still change."""
    for variant in post.variants.filter(
        platform__in=post.platforms, status__in=_CHANGEABLE_VARIANTS
    ):
        options = channel_options.get(variant.platform, {})
        if variant.options != options:
            variant.options = options
            variant.save(update_fields=["options"])


class NotEditable(Exception):
    """The post moved on (published, submitted, scheduled) while this request waited."""


class StaleVersion(Exception):
    """Someone saved the post after the copy this request edited."""


def save_post(
    post: ContentPost,
    inp: ComposerInput,
    action: str,
    user,
    data,
    editable=EDITABLE_STATUSES,
    expected_version: str = "",
) -> SaveResult:
    """Write the composer's content to ``post`` and its channels in one transaction.

    The post's row is locked first, so two saves of the same post run one after the
    other and the second sees what the first did. Raises NotEditable when the post's
    status is no longer in ``editable``, StaleVersion when ``expected_version`` is given
    and the post changed since, ValueError when the channels can't be set up (no
    channel chosen), and IntegrityError when another request already created the post
    for this client key.
    """
    if post.pk:
        with transaction.atomic():
            current = (
                ContentPost.objects.select_for_update()
                .only("status", "updated_at")
                .get(pk=post.pk)
            )
            if current.status not in editable:
                raise NotEditable(current.status)
            # Compared under the lock, so two tabs saving at once can't both pass.
            if expected_version and version_of(current) != expected_version:
                raise StaleVersion(version_of(current))
            return _save_locked(post, inp, action, user, data)
    return _save_locked(post, inp, action, user, data)


def _save_locked(post, inp, action, user, data) -> SaveResult:
    from .services import apply_schedule, initialize_post_with_variants

    post.caption = inp.caption
    post.hashtags = inp.hashtags
    post.platforms = inp.platforms
    post.media_type = inp.media_type
    post.media_urls = inp.media_urls
    post.scheduled_at = inp.scheduled_at
    post.status = status_for(action, inp)
    post.platform_captions = inp.platform_captions
    # Brief fields are only overwritten when the form sends them, so a composer save
    # does not wipe a brief copied from the plan item.
    for name, value in inp.brief.items():
        setattr(post, name, value)
    if not post.pk:
        post.created_by = user

    result = SaveResult(post=post)
    with transaction.atomic():
        post.save()
        initialize_post_with_variants(post, inp.platforms)
        # Before the review check: the CTA and its caption line are part of what a
        # reviewer approves.
        result.missing_wa_number = _apply_cta(post, inp.cta, data)
        _apply_channel_options(post, inp.channel_options)
        # Checked after the variants exist, since the approval covers them. An approval
        # of earlier content no longer applies, so the post goes back to draft for a
        # fresh review.
        if not post.is_approval_valid():
            if post.approval_status == "approved":
                post.approval_status = "draft"
            result.review_blocked = post.status != ContentPost.Status.DRAFT
            post.status = ContentPost.Status.DRAFT
            post.save(update_fields=["status", "approval_status", "updated_at"])
        # After the review check, so a post held for review keeps its channels in draft
        # instead of letting the scheduler pick them up.
        apply_schedule(post, inp.channel_times)
    post.refresh_from_db(fields=["updated_at"])
    return result


def create_or_get(account, user, client_key) -> tuple[ContentPost, bool]:
    """A new unsaved post for ``client_key``, or the one another request already saved.

    Returns (post, existed). The unsaved post carries the key, so its first save
    claims it; a concurrent save of the same key fails on the unique constraint.
    """
    if client_key:
        existing = ContentPost.objects.filter(
            account=account, client_key=client_key
        ).first()
        if existing:
            return existing, True
    return ContentPost(account=account, created_by=user, client_key=client_key), False


def save_new_or_existing(
    post, inp, action, user, data, editable=EDITABLE_STATUSES, expected_version=""
) -> SaveResult:
    """``save_post`` that reports a repeated submit instead of failing: the post was
    already sent on, or another request saved this client key first. StaleVersion is
    passed on to the caller."""
    try:
        if post.pk:
            return save_post(post, inp, action, user, data, editable, expected_version)
        with transaction.atomic():
            return save_post(post, inp, action, user, data, editable)
    except NotEditable:
        post.refresh_from_db()
        return SaveResult(post=post, duplicate=True)
    except IntegrityError:
        if not post.client_key:
            raise
        existing = ContentPost.objects.get(
            account=post.account, client_key=post.client_key
        )
        return SaveResult(post=existing, duplicate=True)
