"""Comment-to-DM for Facebook Page comments, with the account's shared comment triggers.

Same invariants as apps/instagram/services/triggers.py: a thread fires at most one
trigger (``trigger_fired_at`` claimed atomically), the outbox key is deterministic,
and the Messenger thread is opened only after Meta accepted the private reply.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.facebook.models.comment import FacebookCommentThread

logger = logging.getLogger(__name__)


def evaluate_triggers(thread: FacebookCommentThread, body: str, conn) -> bool:
    """Fire the first matching trigger for this root comment. True when one fired."""
    from apps.instagram.api import first_matching_trigger
    from apps.instagram.services.triggers import already_answered_on_post

    if not body or thread.trigger_fired_at is not None:
        return False
    if already_answered_on_post(thread, "facebook_contact"):
        return False
    trigger = first_matching_trigger(
        conn.account, "facebook", body, variant=thread.content_variant
    )
    if trigger is None:
        return False
    _fire_trigger(thread, trigger, conn)
    return True


def _fire_trigger(thread: FacebookCommentThread, trigger, conn) -> None:
    from apps.facebook.models.message import FacebookOutboundMessage
    from apps.facebook.services.outbound import enqueue_reply, send_outbound
    from apps.instagram.api import CommentTrigger
    from apps.instagram.services.triggers import TOO_OLD, reply_links, too_old

    if not _claim_trigger(thread):
        return

    first_name = (thread.facebook_contact.name or "").split(" ")[0]
    links = (
        reply_links(conn.account, thread.content_variant)
        if trigger.uses_links()
        else {}
    )
    body = trigger.render_reply(first_name, links)
    action_type = (
        FacebookOutboundMessage.ActionType.COMMENT_REPLY
        if trigger.reply_type == CommentTrigger.ReplyType.COMMENT
        else FacebookOutboundMessage.ActionType.PRIVATE_REPLY
    )
    key = f"private_reply:fb:{conn.account_id}:{thread.comment_id}"
    outbound = enqueue_reply(
        conn,
        recipient_id=thread.comment_id,
        body=body,
        action_type=action_type,
        idempotency_key=key,
    )
    if outbound is not None and too_old(thread):
        outbound.mark_failed(TOO_OLD, terminal=True)
        return
    if outbound is None:
        outbound = FacebookOutboundMessage.objects.get(idempotency_key=key)
        if outbound.status == FacebookOutboundMessage.Status.SENT:
            ensure_conversation(thread, outbound)
        return
    if send_outbound(outbound):
        logger.info(
            "Facebook private reply sent for thread %s (%s)", thread.pk, trigger.name
        )
        ensure_conversation(thread, outbound)
    else:
        logger.warning(
            "Facebook private reply failed for thread %s (%s): %s",
            thread.pk,
            trigger.name,
            outbound.last_error,
        )


def _claim_trigger(thread: FacebookCommentThread) -> bool:
    updated = FacebookCommentThread.objects.filter(
        pk=thread.pk, trigger_fired_at__isnull=True
    ).update(trigger_fired_at=timezone.now())
    if updated:
        thread.trigger_fired_at = timezone.now()
        return True
    return False


def ensure_conversation(thread: FacebookCommentThread, outbound=None) -> None:
    """Open (or find) the commenter's Messenger thread and link it to the comment.

    ``outbound`` is the sent private reply: Meta's answer names the commenter's
    Messenger PSID, which the thread is opened on.
    """
    from apps.conversations.entry_points import record_comment_entry
    from apps.facebook.services.conversations import (
        get_or_create_facebook_conversation,
    )
    from apps.facebook.services.outbound import messenger_contact

    fb_contact = messenger_contact(
        thread.facebook_contact,
        outbound.resolved_recipient_id if outbound is not None else "",
    )
    _fb_convo, spine = get_or_create_facebook_conversation(fb_contact, thread.page_id)
    if thread.conversation_id != spine.pk:
        FacebookCommentThread.objects.filter(pk=thread.pk).update(conversation=spine)
        thread.conversation_id = spine.pk
    try:
        record_comment_entry(thread)
    except Exception:
        logger.exception(
            "Couldn't record the post entry point for thread %s", thread.pk
        )
