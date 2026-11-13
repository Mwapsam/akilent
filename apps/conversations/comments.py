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
class PostRef:
    """The post a comment is on, as the Comments list shows it beside the comment."""

    channel: str
    post_id: str
    label: str
    thumb: str = ""  # a media URL (Content Studio posts); "" shows a placeholder
    url: str = ""  # the live post, when known
    variant_id: int | None = None


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
    post: PostRef | None = None  # set by comment_feed
    # Why the automatic reply to this comment didn't go (set by comment_feed): a reply
    # rule fired but Meta refused it, or the comment was past the 7-day window.
    auto_reply_error: str = ""

    @property
    def collapsed(self) -> bool:
        """Hidden or deleted at the root: folded into "3 hidden · show" on the page."""
        return self.root is not None and self.root.state in ("hidden", "deleted")


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


# ---------------------------------------------------------------------------
# The Comments list: one flat list of comments, newest first, a page at a time
# ---------------------------------------------------------------------------

FEED_FILTERS = ("unanswered", "all", "intent", "flagged")
FEED_PAGE = 20


def comment_feed(
    account,
    *,
    channel: str = "",
    filter_by: str = "unanswered",
    post_id: str = "",
    variant_id: int | None = None,
    before: datetime | None = None,
    limit: int = FEED_PAGE,
) -> tuple[list[ThreadItem], datetime | None]:
    """Comment threads (a visitor's comment with its replies) across both channels, newest
    first, each with its post. Returns the page and the cursor for "Load more" (None at
    the end). Queries don't grow with the number of comments or replies: per channel one
    for the threads and one for their comments, then one or two for the posts.

    "Not answered" (the default): nobody replied, publicly or in private, and it isn't
    deleted. Posts without comments never appear; a post is reached through its comments.
    """
    if filter_by not in FEED_FILTERS:
        filter_by = "unanswered"
    rows: list[ThreadItem] = []
    for ch in usable_channels(account):
        if channel and ch != channel:
            continue
        qs = _feed_queryset(account, ch, filter_by, post_id, variant_id)
        if before is not None:
            qs = qs.filter(received_at__lt=before)
        rows += [
            _thread_item(ch, t) for t in qs.order_by("-received_at", "-pk")[: limit + 1]
        ]
    rows.sort(key=lambda t: (t.received_at, t.pk), reverse=True)
    page, more = rows[:limit], len(rows) > limit
    _attach_posts(account, page)
    _attach_auto_reply_errors(account, page)
    return page, (page[-1].received_at if more and page else None)


def _attach_auto_reply_errors(account, threads: list[ThreadItem]) -> None:
    """Say on a comment when its automatic reply failed, instead of only in the logs.

    A reply rule's message is keyed ``private_reply:…`` and addressed to the comment
    (apps.instagram/facebook.services.triggers); one query per channel on the page."""
    from apps.facebook.models.message import FacebookOutboundMessage
    from apps.instagram.models.message import OutboundMessage

    by_channel: dict[str, dict[str, ThreadItem]] = {INSTAGRAM: {}, FACEBOOK: {}}
    for t in threads:
        if t.root is not None and not t.conversation_public_id:
            by_channel[t.channel][t.root.comment_id] = t
    if by_channel[INSTAGRAM]:
        for recipient, error in OutboundMessage.objects.filter(
            instagram_account__account=account,
            recipient_igsid__in=list(by_channel[INSTAGRAM]),
            idempotency_key__startswith="private_reply:",
            status=OutboundMessage.Status.FAILED,
        ).values_list("recipient_igsid", "last_error"):
            by_channel[INSTAGRAM][recipient].auto_reply_error = _reason(error)
    if by_channel[FACEBOOK]:
        for recipient, error in FacebookOutboundMessage.objects.filter(
            account=account,
            recipient_id__in=list(by_channel[FACEBOOK]),
            idempotency_key__startswith="private_reply:",
            status=FacebookOutboundMessage.Status.FAILED,
        ).values_list("recipient_id", "last_error"):
            by_channel[FACEBOOK][recipient].auto_reply_error = _reason(error)


def _reason(error: str) -> str:
    """The why, without a leading "Not sent:" (the page says that part)."""
    error = (error or "").strip()
    if error.lower().startswith("not sent:"):
        error = error[len("not sent:") :].strip()
    return error[:200] or "Meta didn't accept it."


def _feed_queryset(account, channel, filter_by, post_id, variant_id):
    from django.db.models import Exists, OuterRef

    qs: Any
    comments_model: Any
    if channel == INSTAGRAM:
        from apps.instagram.models.comment import Comment, CommentThread

        qs = CommentThread.objects.filter(
            instagram_account__account=account
        ).select_related("instagram_contact", "conversation")
        comments_model, contact_field = Comment, "instagram_contact"
    else:
        from apps.facebook.models.comment import (
            FacebookComment,
            FacebookCommentThread,
        )

        qs = FacebookCommentThread.objects.filter(account=account).select_related(
            "facebook_contact", "conversation"
        )
        comments_model, contact_field = FacebookComment, "facebook_contact"
    if post_id:
        qs = qs.filter(post_id=post_id)
    if variant_id:
        qs = qs.filter(content_variant_id=variant_id)
    if filter_by == "unanswered":
        replied = comments_model.objects.filter(
            thread=OuterRef("pk"), direction="outbound"
        )
        deleted = comments_model.objects.filter(
            thread=OuterRef("pk"),
            comment_id=OuterRef("comment_id"),
            moderation_state="deleted",
        )
        qs = (
            qs.filter(conversation__isnull=True)
            .exclude(Exists(replied))
            .exclude(Exists(deleted))
        )
    elif filter_by == "intent":
        qs = qs.exclude(intent="")
    elif filter_by == "flagged":
        qs = qs.filter(
            Exists(
                comments_model.objects.filter(
                    thread=OuterRef("pk"), moderation_state="flagged"
                )
            )
        )
    return qs.prefetch_related(
        Prefetch(
            "comments",
            queryset=comments_model.objects.select_related(contact_field).order_by(
                "timestamp", "pk"
            ),
        )
    )


def _ig_author(c) -> str:
    contact = c.instagram_contact
    return f"@{contact.username}" if contact.username else contact.name or "Someone"


def _fb_author(c) -> str:
    return c.facebook_contact.name or "Someone"


def _thread_item(channel: str, thread) -> ThreadItem:
    author = _ig_author if channel == INSTAGRAM else _fb_author
    items = [
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
    root = next((c for c in items if c.comment_id == thread.comment_id), None)
    return ThreadItem(
        channel=channel,
        pk=thread.pk,
        post_id=thread.post_id,
        root=root,
        replies=[c for c in items if c is not root],
        intent=thread.intent,
        conversation_public_id=(
            thread.conversation.public_id if thread.conversation else ""
        ),
        received_at=thread.received_at,
        answered=bool(thread.conversation_id) or any(c.outbound for c in items),
        flagged=any(c.state == "flagged" for c in items),
        variant_id=thread.content_variant_id,
    )


def _attach_posts(account, threads: list[ThreadItem]) -> None:
    """Each thread's post: its Content Studio caption and picture when Akilent posted it
    (one query, two when some threads aren't linked to a post yet)."""
    from django.db.models import Q

    from apps.publishing.models import ContentPostVariant

    if not threads:
        return
    by_pk: dict[int, Any] = {}
    by_ref: dict[tuple[str, str], Any] = {}
    linked = {t.variant_id for t in threads if t.variant_id}
    if linked:
        for v in ContentPostVariant.objects.select_related("post").filter(
            pk__in=linked, post__account=account
        ):
            by_pk[v.pk] = v
    ids = {t.post_id for t in threads if t.variant_id not in by_pk}
    if ids:
        for v in (
            ContentPostVariant.objects.select_related("post")
            .filter(post__account=account, platform__in=CHANNELS)
            .filter(Q(publication_id__in=ids) | Q(metadata__fb_post_id__in=ids))
        ):
            by_ref.setdefault((v.platform, _variant_post_ref(v)), v)
            by_ref.setdefault((v.platform, v.publication_id), v)
    for t in threads:
        found = by_pk.get(t.variant_id) if t.variant_id else None
        t.post = post_ref(
            t.channel, t.post_id, found or by_ref.get((t.channel, t.post_id))
        )


def post_ref(channel: str, post_id: str, variant=None) -> PostRef:
    text, thumb, url, vid = "", "", "", None
    if variant is not None:
        text = variant.caption or variant.post.caption or ""
        media = variant.media_urls or variant.post.media_urls or []
        thumb = media[0] if media else ""
        url = variant.publication_url or ""
        vid = variant.pk
    if not url and channel == FACEBOOK and post_id:
        url = f"https://www.facebook.com/{post_id}"
    label = (text.strip().splitlines() or [""])[0][:80] or (
        "Instagram post" if channel == INSTAGRAM else "Facebook post"
    )
    return PostRef(channel, post_id, label, thumb, url, vid)


def post_header(account, channel: str, post_id: str, variant_id=None):
    """One post for the Comments list narrowed to it: its name and picture, the business's
    own comments on it, and what to comment on as the business. (PostItem, PostRef), or
    None when the post can't be found on a usable channel."""
    from apps.publishing.models import ContentPostVariant

    variant = None
    if variant_id:
        variant = (
            ContentPostVariant.objects.select_related("post")
            .filter(pk=variant_id, post__account=account)
            .first()
        )
        if variant is not None:
            channel = channel or variant.platform
            post_id = post_id or _variant_post_ref(variant)
    if channel not in usable_channels(account) or not post_id:
        return None
    if variant is None:
        found = _published_variants(account, channel, post_id)
        variant = found[0] if found else None
    ref = post_ref(channel, post_id, variant)
    item = PostItem(channel, post_id, ref.label, variant)
    item.business_comments = _business_comments(account, item)
    return item, ref


def moderate_many(account, items: list[tuple[str, int]], action: str, user):
    """Hide, unhide or delete several comments. Each goes to Instagram/Facebook on its own,
    so one refusal doesn't stop the rest. Returns (done, failed, first error)."""
    if action not in ("hide", "unhide", "delete"):
        raise CommentError("Unknown action.")
    done: list[tuple[str, int]] = []
    failed: list[tuple[str, int]] = []
    first_error = ""
    for channel, pk in items:
        try:
            moderate(account, channel, pk, action, user)
            done.append((channel, pk))
        except CommentError as exc:
            failed.append((channel, pk))
            first_error = first_error or str(exc)
    return done, failed, first_error
