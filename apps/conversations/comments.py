"""Comments on the business's Instagram and Facebook posts, handled by a person.

Backs the Comments view (``comments_views``): list recent comment threads from both
channels, and let a teammate comment on a post, reply publicly, send a private
reply (which opens a DM, like a comment trigger does) and hide, unhide or delete a
comment. Every send goes through the channel's outbox, idempotent on the browser's
key; every moderation action writes a ModerationLog with ``acted_by``.

Each channel needs its feature (``instagram`` / ``facebook``) and to be switched on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from django.db.models import Prefetch

logger = logging.getLogger(__name__)

INSTAGRAM = "instagram"
FACEBOOK = "facebook"
CHANNELS = (INSTAGRAM, FACEBOOK)
FILTERS = ("all", "unanswered", "intent", "flagged")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class CommentError(Exception):
    """The action can't be done; the message says why, for a person."""


@dataclass
class CommentItem:
    channel: str
    pk: int  # the comment row (instagram.Comment / facebook.FacebookComment)
    comment_id: str
    author: str
    body: str
    timestamp: datetime
    outbound: bool
    state: str  # pending / approved / flagged / hidden / deleted
    hidden: bool


@dataclass
class ThreadItem:
    channel: str
    pk: int  # the thread row
    post_id: str
    root: CommentItem | None
    replies: list[CommentItem]
    intent: str
    conversation_public_id: str
    received_at: datetime
    answered: bool
    flagged: bool
    variant_id: int | None = None  # the Content Studio channel that published the post


@dataclass
class PostItem:
    channel: str
    post_id: str
    label: str
    variant: object | None
    threads: list[ThreadItem] = field(default_factory=list)
    business_comments: list = field(default_factory=list)  # sent POST_COMMENT rows

    @property
    def latest(self) -> datetime | None:
        dates = [t.received_at for t in self.threads]
        return max(dates) if dates else None


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def usable_channels(account) -> list[str]:
    """The comment channels this business can use here: feature, switch and connection."""
    from apps.billing import api as billing_api
    from apps.core.channels import is_enabled
    from apps.facebook.models.page import active_page

    found = []
    if (
        is_enabled(INSTAGRAM)
        and billing_api.usable(account, "instagram")
        and account.instagram_accounts.filter(is_active=True).exists()
    ):
        found.append(INSTAGRAM)
    if (
        is_enabled(FACEBOOK)
        and billing_api.usable(account, "facebook")
        and active_page(account) is not None
    ):
        found.append(FACEBOOK)
    return found


def _require(account, channel: str) -> None:
    if channel not in CHANNELS:
        raise CommentError("Unknown channel.")
    if channel not in usable_channels(account):
        label = "Instagram" if channel == INSTAGRAM else "Facebook"
        raise CommentError(f"{label} isn't connected, so comments can't be managed.")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def posts_with_comments(
    account,
    *,
    channel: str = "",
    filter_by: str = "all",
    post_id: str = "",
    variant_id: int | None = None,
    limit: int = 30,
) -> list[PostItem]:
    """Recent posts with their comment threads, newest activity first.

    Published Content Studio posts are included even with no comments yet, so a person
    can be the first to comment. ``variant_id`` narrows it to one Content Studio post's
    channel (its photo or video id isn't always the post id comments name).
    """
    channels = [c for c in usable_channels(account) if not channel or c == channel]
    posts: dict[tuple[str, str], PostItem] = {}
    for ch in channels:
        linked = set()
        for thread in _threads(account, ch, filter_by, post_id, variant_id, limit):
            key = (ch, thread.post_id)
            if key not in posts:
                posts[key] = PostItem(ch, thread.post_id, "", None)
            posts[key].threads.append(thread)
            linked.add(thread.variant_id)
        if filter_by == "all":
            for variant in _published_variants(account, ch, post_id, variant_id):
                ref = _variant_post_ref(variant)
                if ref and variant.pk not in linked and (ch, ref) not in posts:
                    posts[(ch, ref)] = PostItem(ch, ref, "", variant)
    for item in posts.values():
        _label(account, item)
        item.business_comments = _business_comments(account, item)
    ordered = sorted(
        posts.values(),
        key=lambda p: p.latest or _variant_time(p.variant) or _EPOCH,
        reverse=True,
    )
    return ordered[:limit]


def _threads(
    account, channel, filter_by, post_id, variant_id, limit
) -> list[ThreadItem]:
    qs: Any
    comments_model: Any
    if channel == INSTAGRAM:
        from apps.instagram.models.comment import Comment, CommentThread

        qs = CommentThread.objects.filter(
            instagram_account__account=account
        ).select_related("instagram_contact", "conversation")
        comments_model = Comment
        author = lambda c: (  # noqa: E731
            f"@{c.instagram_contact.username}"
            if c.instagram_contact.username
            else c.instagram_contact.name or "Someone"
        )
    else:
        from apps.facebook.models.comment import (
            FacebookComment,
            FacebookCommentThread,
        )

        qs = FacebookCommentThread.objects.filter(account=account).select_related(
            "facebook_contact", "conversation"
        )
        comments_model = FacebookComment
        author = lambda c: c.facebook_contact.name or "Someone"  # noqa: E731
    if post_id:
        qs = qs.filter(post_id=post_id)
    if variant_id:
        qs = qs.filter(content_variant_id=variant_id)
    if filter_by == "intent":
        qs = qs.exclude(intent="")
    if filter_by == "flagged":
        qs = qs.filter(comments__moderation_state="flagged").distinct()
    contact_field = "instagram_contact" if channel == INSTAGRAM else "facebook_contact"
    qs = qs.prefetch_related(
        Prefetch(
            "comments",
            queryset=comments_model.objects.select_related(contact_field).order_by(
                "timestamp", "pk"
            ),
        )
    ).order_by("-received_at")[: limit * 5]

    items = []
    for thread in qs:
        comments = [
            CommentItem(
                channel=channel,
                pk=c.pk,
                comment_id=c.comment_id,
                author="You" if c.direction == "outbound" else author(c),
                body=c.body,
                timestamp=c.timestamp,
                outbound=c.direction == "outbound",
                state=c.moderation_state,
                hidden=c.is_hidden,
            )
            for c in thread.comments.all()
        ]
        root = next((c for c in comments if c.comment_id == thread.comment_id), None)
        replies = [c for c in comments if c is not root]
        answered = bool(thread.conversation_id) or any(c.outbound for c in comments)
        flagged = any(c.state == "flagged" for c in comments)
        if filter_by == "unanswered" and answered:
            continue
        items.append(
            ThreadItem(
                channel=channel,
                pk=thread.pk,
                post_id=thread.post_id,
                root=root,
                replies=replies,
                intent=thread.intent,
                conversation_public_id=(
                    thread.conversation.public_id if thread.conversation else ""
                ),
                received_at=thread.received_at,
                answered=answered,
                flagged=flagged,
                variant_id=thread.content_variant_id,
            )
        )
    return items


def _published_variants(account, channel: str, post_id: str, variant_id=None):
    from django.db.models import Q

    from apps.publishing.models import ContentPostVariant

    qs = (
        ContentPostVariant.objects.filter(post__account=account, platform=channel)
        .exclude(publication_id="")
        .select_related("post")
        .order_by("-published_at", "-pk")
    )
    if post_id:
        qs = qs.filter(Q(publication_id=post_id) | Q(metadata__fb_post_id=post_id))
    if variant_id:
        qs = qs.filter(pk=variant_id)
    return list(qs[:20])


def _variant_post_ref(variant) -> str:
    """The id comments are posted to: the IG media, or the Facebook post (or object)."""
    if variant.platform == FACEBOOK:
        return (variant.metadata or {}).get("fb_post_id") or variant.publication_id
    return variant.publication_id


def _variant_time(variant):
    if variant is None:
        return None
    return variant.published_at


def _label(account, item: PostItem) -> None:
    """A recognisable name for the post: its Content Studio caption, if Akilent posted it."""
    variant: Any
    from apps.publishing.models import ContentPostVariant

    variant = item.variant
    if variant is None:
        thread_variant_ids = []
        if item.threads:
            if item.channel == INSTAGRAM:
                from apps.instagram.models.comment import CommentThread

                thread_variant_ids = list(
                    CommentThread.objects.filter(
                        pk__in=[t.pk for t in item.threads],
                        content_variant__isnull=False,
                    ).values_list("content_variant_id", flat=True)[:1]
                )
            else:
                from apps.facebook.models.comment import FacebookCommentThread

                thread_variant_ids = list(
                    FacebookCommentThread.objects.filter(
                        pk__in=[t.pk for t in item.threads],
                        content_variant__isnull=False,
                    ).values_list("content_variant_id", flat=True)[:1]
                )
        if thread_variant_ids:
            variant = (
                ContentPostVariant.objects.select_related("post")
                .filter(pk=thread_variant_ids[0])
                .first()
            )
            item.variant = variant
    text = ""
    if variant is not None:
        text = variant.caption or variant.post.caption or ""
    item.label = (text.strip().splitlines() or [""])[0][:80] or (
        "Instagram post" if item.channel == INSTAGRAM else "Facebook post"
    )


def _business_comments(account, item: PostItem) -> list:
    if item.channel == INSTAGRAM:
        from apps.instagram.models.message import OutboundMessage

        return list(
            OutboundMessage.objects.filter(
                instagram_account__account=account,
                action_type=OutboundMessage.ActionType.POST_COMMENT,
                recipient_igsid=item.post_id,
            ).order_by("created_at")
        )
    from apps.facebook.models.message import FacebookOutboundMessage

    return list(
        FacebookOutboundMessage.objects.filter(
            account=account,
            action_type=FacebookOutboundMessage.ActionType.POST_COMMENT,
            recipient_id=item.post_id,
        ).order_by("created_at")
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _key(kind: str, user, client_key: str) -> str:
    import uuid

    return f"comment:{kind}:{user.pk}:{client_key or uuid.uuid4()}"


def _ig_account_for_post(account, post_id: str):
    from apps.instagram.models.comment import CommentThread

    thread = (
        CommentThread.objects.filter(
            instagram_account__account=account, post_id=post_id
        )
        .select_related("instagram_account")
        .first()
    )
    if thread is not None and thread.instagram_account.is_ready:
        return thread.instagram_account
    iba = next(
        (a for a in account.instagram_accounts.filter(is_active=True) if a.is_ready),
        None,
    )
    if iba is None:
        raise CommentError("Instagram isn't connected — reconnect it in Settings.")
    return iba


def _send(channel: str, outbound) -> None:
    """Send now; a transient failure is left to the outbox drain."""
    Row: Any
    send_outbound: Any
    if channel == INSTAGRAM:
        from apps.instagram.models.message import OutboundMessage as Row
        from apps.instagram.services.outbound import send_outbound
    else:
        from apps.facebook.models.message import FacebookOutboundMessage as Row
        from apps.facebook.services.outbound import send_outbound
    if outbound.status == Row.Status.SENT:
        return
    if not send_outbound(outbound):
        outbound.refresh_from_db()
        if outbound.status == Row.Status.FAILED:
            raise CommentError(f"Not sent: {outbound.last_error}")


def post_comment(account, channel: str, post_id: str, text: str, user, client_key=""):
    """A new comment on one of the business's posts."""
    enqueue_reply: Any
    outbound: Any
    text = (text or "").strip()
    if not text:
        raise CommentError("Write a comment first.")
    if not post_id:
        raise CommentError("Choose a post.")
    _require(account, channel)
    key = _key("post", user, client_key)
    if channel == INSTAGRAM:
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.services.outbound import enqueue_reply

        iba = _ig_account_for_post(account, post_id)
        outbound = enqueue_reply(
            iba,
            post_id,
            text,
            action_type=OutboundMessage.ActionType.POST_COMMENT,
            idempotency_key=key,
        ) or OutboundMessage.objects.get(idempotency_key=key)
    else:
        from apps.facebook.models.message import FacebookOutboundMessage
        from apps.facebook.models.page import active_page
        from apps.facebook.services.outbound import enqueue_reply

        conn = active_page(account)
        outbound = enqueue_reply(
            conn,
            post_id,
            text,
            action_type=FacebookOutboundMessage.ActionType.POST_COMMENT,
            idempotency_key=key,
        ) or FacebookOutboundMessage.objects.get(idempotency_key=key)
    _send(channel, outbound)
    return outbound


def _comment(account, channel: str, pk: int):
    comment: Any
    if channel == INSTAGRAM:
        from apps.instagram.models.comment import Comment

        comment = (
            Comment.objects.select_related(
                "thread__instagram_account", "thread__instagram_contact"
            )
            .filter(pk=pk, thread__instagram_account__account=account)
            .first()
        )
    else:
        from apps.facebook.models.comment import FacebookComment

        comment = (
            FacebookComment.objects.select_related("thread__facebook_contact")
            .filter(pk=pk, thread__account=account)
            .first()
        )
    if comment is None:
        raise CommentError("That comment wasn't found.")
    return comment


def reply(account, channel: str, comment_pk: int, text: str, user, client_key=""):
    """A public reply under a visitor's comment, as the business."""
    enqueue_reply: Any
    outbound: Any
    text = (text or "").strip()
    if not text:
        raise CommentError("Write a reply first.")
    _require(account, channel)
    comment = _comment(account, channel, comment_pk)
    # Replies go under the thread's root comment (Instagram and Facebook nest one level).
    target = comment.thread.comment_id
    key = _key("reply", user, client_key)
    if channel == INSTAGRAM:
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.services.outbound import enqueue_reply

        outbound = enqueue_reply(
            comment.thread.instagram_account,
            target,
            text,
            action_type=OutboundMessage.ActionType.COMMENT_REPLY,
            idempotency_key=key,
        ) or OutboundMessage.objects.get(idempotency_key=key)
    else:
        from apps.facebook.models.message import FacebookOutboundMessage
        from apps.facebook.models.page import active_page
        from apps.facebook.services.outbound import enqueue_reply

        outbound = enqueue_reply(
            active_page(account),
            target,
            text,
            action_type=FacebookOutboundMessage.ActionType.COMMENT_REPLY,
            idempotency_key=key,
        ) or FacebookOutboundMessage.objects.get(idempotency_key=key)
    _send(channel, outbound)
    return outbound


def private_reply(
    account, channel: str, comment_pk: int, text: str, user, client_key=""
):
    """A private DM in answer to a comment; opens the DM in the inbox, like a trigger.

    Meta allows one private reply per comment, within 7 days of it.
    """
    enqueue_reply: Any
    outbound: Any
    text = (text or "").strip()
    if not text:
        raise CommentError("Write a message first.")
    _require(account, channel)
    comment = _comment(account, channel, comment_pk)
    if comment.direction == "outbound":
        raise CommentError("That's your own comment.")
    key = _key("dm", user, client_key)
    if channel == INSTAGRAM:
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.services.outbound import enqueue_reply
        from apps.instagram.services.triggers import _ensure_conversation

        outbound = enqueue_reply(
            comment.thread.instagram_account,
            comment.comment_id,
            text,
            action_type=OutboundMessage.ActionType.PRIVATE_REPLY,
            idempotency_key=key,
        ) or OutboundMessage.objects.get(idempotency_key=key)
        _send(channel, outbound)
        outbound.refresh_from_db()
        if outbound.status == OutboundMessage.Status.SENT:
            _ensure_conversation(comment.thread, comment.thread.instagram_contact)
    else:
        from apps.facebook.models.message import FacebookOutboundMessage
        from apps.facebook.models.page import active_page
        from apps.facebook.services.outbound import enqueue_reply
        from apps.facebook.services.triggers import ensure_conversation

        outbound = enqueue_reply(
            active_page(account),
            comment.comment_id,
            text,
            action_type=FacebookOutboundMessage.ActionType.PRIVATE_REPLY,
            idempotency_key=key,
        ) or FacebookOutboundMessage.objects.get(idempotency_key=key)
        _send(channel, outbound)
        outbound.refresh_from_db()
        if outbound.status == FacebookOutboundMessage.Status.SENT:
            ensure_conversation(comment.thread, outbound)
    comment.thread.refresh_from_db(fields=["conversation"])
    return comment.thread.conversation


MODERATION_ACTIONS = ("hide", "unhide", "delete", "approve")


def moderate(account, channel: str, comment_pk: int, action: str, user):
    """Hide, unhide, delete or approve (clear a flag on) a comment by hand; logged."""
    outcome: str
    from apps.instagram.models.moderation import ModerationLog

    if action not in MODERATION_ACTIONS:
        raise CommentError("Unknown action.")
    _require(account, channel)
    comment = _comment(account, channel, comment_pk)
    if comment.direction == "outbound" and action != "delete":
        raise CommentError("That's your own comment.")

    S = type(comment).ModerationState
    if action == "approve":
        outcome, error = ModerationLog.Outcome.APPLIED, ""
        comment.moderation_state = S.APPROVED
        comment.save(update_fields=["moderation_state"])
    else:
        outcome, error = _apply(channel, comment, action)
    log = ModerationLog(
        account=account,
        rule=None,
        moderation_action=action,
        outcome=outcome,
        error=error,
        acted_by=user,
        **(
            {"comment": comment}
            if channel == INSTAGRAM
            else {"facebook_comment": comment}
        ),
    )
    log.save()
    if outcome == ModerationLog.Outcome.FAILED:
        raise CommentError(error)
    return comment


def _apply(channel: str, comment, action: str) -> tuple[str, str]:
    provider: Any
    from apps.instagram.models.moderation import ModerationLog

    S = type(comment).ModerationState
    if channel == INSTAGRAM:
        from apps.instagram.providers.meta import MetaInstagramProvider

        iba = comment.thread.instagram_account
        if not iba.access_token:
            return (
                ModerationLog.Outcome.FAILED,
                "Instagram isn't connected — reconnect it.",
            )
        provider = MetaInstagramProvider(
            access_token=iba.access_token,
            instagram_account_id=iba.instagram_business_account_id,
        )
        label = "Instagram"
    else:
        from apps.facebook.models.page import active_page
        from apps.facebook.providers.meta import provider_for

        conn = active_page(comment.thread.account)
        if conn is None:
            return (
                ModerationLog.Outcome.FAILED,
                "Facebook isn't connected — reconnect it.",
            )
        provider = provider_for(conn)
        label = "Facebook"

    if action in {"hide", "unhide"}:
        hidden = action == "hide"
        ok, err = provider.hide_comment(comment.comment_id, hidden)
        if not ok:
            return ModerationLog.Outcome.FAILED, f"{label} refused: {err}"
        comment.is_hidden = hidden
        comment.moderation_state = S.HIDDEN if hidden else S.APPROVED
        comment.save(update_fields=["is_hidden", "moderation_state"])
        return ModerationLog.Outcome.APPLIED, ""
    ok, err = provider.delete_comment(comment.comment_id)
    if not ok:
        return ModerationLog.Outcome.FAILED, f"{label} refused: {err}"
    comment.moderation_state = S.DELETED
    comment.save(update_fields=["moderation_state"])
    return ModerationLog.Outcome.APPLIED, ""
