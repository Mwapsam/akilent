"""Store the Meta ad a customer tapped to reach the business (store-first, never raises).

WhatsApp puts a ``referral`` object on the first message from a Click-to-WhatsApp ad::

    {"source_url", "source_id", "source_type": "ad"|"post", "headline", "body",
     "media_type", "image_url"|"video_url", "thumbnail_url", "ctwa_clid"}

Instagram puts one on a message from a Click-to-Instagram-Direct ad (and sends a standalone
``messaging_referral`` event when the customer reopens an existing thread)::

    {"ref", "source": "ADS", "type": "OPEN_THREAD", "ad_id",
     "ads_context_data": {"ad_title", "photo_url", "video_url", "post_id", "product_id"}}

Both are normalised to one shape, stored as an :class:`AdReferral` and recorded as the
conversation's ``AD_REFERRAL`` entry point. Paid takes precedence over everything else
(docs/strategy/akilent-ads.md §4): the entry point names no post, even when the ad promotes
a Content Studio post; that match is kept on ``AdReferral.post`` as context only.

Messenger is out of scope. Facebook messages are never read here.
"""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)


def _s(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def from_whatsapp(referral: dict | None) -> dict | None:
    """Normalise a WhatsApp ``messages[].referral`` object. None when there's no ad id."""
    if not isinstance(referral, dict):
        return None
    source_id = _s(referral.get("source_id"), 128)
    if not source_id:
        return None
    return {
        "source_type": _s(referral.get("source_type"), 20).lower(),
        "source_id": source_id,
        "source_url": _s(referral.get("source_url"), 1000),
        "headline": _s(referral.get("headline"), 500),
        "body": _s(referral.get("body"), 5000),
        "media_type": _s(referral.get("media_type"), 20),
        "ctwa_clid": _s(referral.get("ctwa_clid"), 255),
        "post_ids": [],
        "raw": referral,
    }


def from_instagram(referral: dict | None) -> dict | None:
    """Normalise an Instagram ``referral`` object. Only ad referrals count (source ADS)."""
    if not isinstance(referral, dict):
        return None
    if _s(referral.get("source"), 20).upper() not in ("ADS", ""):
        return None  # e.g. an ig.me link or a shop referral: not an ad
    source_id = _s(referral.get("ad_id"), 128)
    if not source_id:
        return None
    context = referral.get("ads_context_data") or {}
    media_type = "video" if context.get("video_url") else "image" if context.get("photo_url") else ""
    post_id = _s(context.get("post_id"), 128)
    return {
        "source_type": "ad",
        "source_id": source_id,
        "source_url": "",
        "headline": _s(context.get("ad_title"), 500),
        "body": "",
        "media_type": media_type,
        "ctwa_clid": "",
        "post_ids": [post_id] if post_id else [],
        "raw": referral,
    }


def whatsapp_referral_for(message_log) -> dict | None:
    """The referral of this WhatsApp message, read from the webhook payload it arrived in."""
    payload = getattr(message_log, "raw_payload", None) or {}
    wanted = message_log.message_id
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for item in (change.get("value") or {}).get("messages") or []:
                if item.get("id") == wanted:
                    return from_whatsapp(item.get("referral"))
    return None


def instagram_referral_for(instagram_message) -> dict | None:
    """The referral of this Instagram message, from the webhook entry kept in its metadata."""
    entry = getattr(instagram_message, "metadata", None) or {}
    message = entry.get("message") or {}
    return from_instagram(message.get("referral") or entry.get("referral"))


def referral_for_message(message) -> tuple[str, dict] | tuple[None, None]:
    """``(channel, normalised referral)`` for a spine inbound message, or ``(None, None)``."""
    from apps.conversations.models import AdReferral

    if message.whatsapp_message_id:
        data = whatsapp_referral_for(message.whatsapp_message)
        return (AdReferral.Channel.WHATSAPP, data) if data else (None, None)
    if message.instagram_message_id:
        data = instagram_referral_for(message.instagram_message)
        return (AdReferral.Channel.INSTAGRAM, data) if data else (None, None)
    return None, None


def _matched_post(account_id: int, data: dict):
    """The Content Studio post the ad promotes, by Meta's own id. Context only, never credit."""
    from django.db.models import Q

    from apps.publishing.models import ContentPost

    ids = [i for i in [*data.get("post_ids", []), data["source_id"]] if i]
    if data.get("source_type") == "ad" and not data.get("post_ids"):
        return None  # an ad id alone can't name a post without ads_read
    q = Q()
    for i in ids:
        q |= Q(ig_media_id=i) | Q(fb_post_id=i) | Q(fb_post_id__endswith=f"_{i}")
    if not q:
        return None
    return ContentPost.objects.filter(q, account_id=account_id).order_by("-pk").first()


def record(conversation, message, channel: str, data: dict, *, received_at=None):
    """Save the referral and the conversation's ad entry point. Idempotent per (chat, ad)."""
    from apps.conversations.models import AdReferral, ConversationEntryPoint

    referral, created = AdReferral.objects.get_or_create(
        conversation=conversation,
        source_id=data["source_id"],
        defaults={
            "account_id": conversation.account_id,
            "message": message,
            "channel": channel,
            "source_type": data["source_type"],
            "source_url": data["source_url"],
            "headline": data["headline"],
            "body": data["body"],
            "media_type": data["media_type"],
            "ctwa_clid": data["ctwa_clid"],
            "post": _matched_post(conversation.account_id, data),
            "raw": data["raw"],
            "received_at": received_at
            or (message.timestamp if message is not None else None)
            or timezone.now(),
        },
    )
    ConversationEntryPoint.objects.get_or_create(
        conversation=conversation,
        ad_referral=referral,
        defaults={
            "account_id": conversation.account_id,
            "kind": ConversationEntryPoint.Kind.AD_REFERRAL,
            "message": message,
        },
    )
    if created:
        logger.info(
            "ad_referral: stored ad=%s channel=%s conversation=%s",
            referral.source_id,
            channel,
            conversation.pk,
        )
    return referral


def fresh_for(conversation) -> dict | None:
    """The ad the conversation's latest customer message came from, for automations.

    ``{source_id, headline, channel}`` when that message carried an ad referral, or when an
    Instagram standalone referral (no message of its own) arrived since the customer's
    previous message. Later messages in the same ad chat are not "from an ad" again; a tap
    on a different ad is. Called right after ``capture`` on the inbound path.
    """
    from apps.conversations.models import AdReferral, Message

    inbound = list(
        Message.objects.filter(
            conversation=conversation, direction=Message.Direction.INBOUND
        )
        .order_by("-timestamp", "-pk")
        .values("pk", "timestamp")[:2]
    )
    if not inbound:
        return None
    latest = inbound[0]
    refs = AdReferral.objects.filter(conversation=conversation)
    ref = refs.filter(message_id=latest["pk"]).first()
    if ref is None:
        standalone = refs.filter(message__isnull=True)
        if len(inbound) > 1:
            standalone = standalone.filter(received_at__gt=inbound[1]["timestamp"])
        ref = standalone.order_by("-received_at", "-pk").first()
    if ref is None:
        return None
    return {
        "source_id": ref.source_id,
        "headline": ref.headline,
        "channel": ref.channel,
    }


def capture(conversation, message):
    """Inbound hook: store the ad this message came from, if it carries one."""
    channel, data = referral_for_message(message)
    if not data:
        return None
    return record(conversation, message, channel, data)


def capture_instagram_event(instagram_account, igsid: str, entry: dict):
    """A standalone Instagram ``referral`` event (the customer reopened an existing thread
    from an ad). Stored against that customer's existing Instagram chat; never creates a
    chat, so a tap with no message never puts an empty conversation in the inbox."""
    from datetime import UTC, datetime

    from apps.conversations.models import AdReferral, Conversation
    from apps.instagram.models.contact import InstagramContact

    data = from_instagram(entry.get("referral"))
    if not data:
        return None
    ig_contact = InstagramContact.objects.filter(
        account=instagram_account.account, instagram_scoped_id=igsid
    ).first()
    if ig_contact is None or not ig_contact.contact_id:
        logger.info("ad_referral: no existing Instagram chat for a standalone referral")
        return None
    conversation = (
        Conversation.objects.filter(
            account=instagram_account.account,
            channel=Conversation.Channel.INSTAGRAM,
            contact_id=ig_contact.contact_id,
        )
        .order_by("-last_message_at", "-pk")
        .first()
    )
    if conversation is None:
        return None
    ts_ms = entry.get("timestamp") or 0
    received_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else None
    return record(
        conversation,
        None,
        AdReferral.Channel.INSTAGRAM,
        data,
        received_at=received_at,
    )
