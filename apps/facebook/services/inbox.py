"""Inbox replies on Messenger: what ``conversations.actions`` calls for channel "facebook".

The 24-hour window applies to every reply. A reply a person typed in the inbox (an
inbox key, ``apps.conversations.inbox_keys``) may still go for 7 days after the
customer's last message, tagged HUMAN_AGENT; automation, AI and workflows never are.
"""

from __future__ import annotations

import uuid

from apps.core.actions import ActionError


def _target(conversation):
    from apps.facebook.models.page import active_page

    fb_convo = conversation.facebook_conversation
    if fb_convo is None:
        raise ActionError("Messenger conversation record not found")
    conn = active_page(conversation.account)
    if conn is None or conn.page_id != fb_convo.page_id:
        raise ActionError(
            "Facebook isn't connected — reconnect your Page in Settings → Channels."
        )
    return fb_convo, conn


def check_window(fb_convo, *, human: bool) -> None:
    from apps.facebook.services.outbound import (
        AWAITING_REPLY_ERROR,
        HUMAN_WINDOW_CLOSED_ERROR,
        WINDOW_CLOSED_ERROR,
    )

    if fb_convo.last_inbound_at is None:
        raise ActionError(AWAITING_REPLY_ERROR)
    if fb_convo.window_is_open:
        return
    if human and fb_convo.human_agent_window_is_open:
        return
    raise ActionError(HUMAN_WINDOW_CLOSED_ERROR if human else WINDOW_CLOSED_ERROR)


def _result(outbound, spine_msg=None, **flags) -> dict:
    """What a Messenger send reports: the inbox message (``message_id``, once sent) and the
    Facebook outbound row (``fb_outbound_message_id``).

    Never ``outbound_message_id``: the workflow engine stores that as the foreign key to
    *WhatsApp's* outbound record (see apps.conversations.actions._instagram_result).
    """
    return {
        "message_id": spine_msg.id if spine_msg else None,
        "fb_outbound_message_id": outbound.pk,
        **flags,
    }


def repeat(outbound) -> dict:
    """The answer to an inbox send repeated with the same key (never a second send)."""
    from apps.facebook.models.message import FacebookOutboundMessage
    from apps.facebook.services.outbound import record_sent_message

    if outbound.status == FacebookOutboundMessage.Status.SENT:
        return _result(outbound, record_sent_message(outbound))
    if outbound.status == FacebookOutboundMessage.Status.FAILED:
        raise ActionError(f"Facebook send failed: {outbound.last_error}")
    if outbound.status == FacebookOutboundMessage.Status.UNCONFIRMED:
        return _result(outbound, unconfirmed=True)
    return _result(outbound, queued=True)


def send(conversation, body: str, key: str | None, *, media=None) -> dict:
    """Send a text (or one attachment, ``media``) on the conversation's Messenger thread."""
    from apps.conversations.inbox_keys import is_inbox_key
    from apps.facebook.models.message import FacebookOutboundMessage
    from apps.facebook.services.outbound import (
        enqueue_reply,
        record_sent_message,
        send_outbound,
    )

    fb_convo, conn = _target(conversation)
    human = is_inbox_key(key)
    check_window(fb_convo, human=human)
    key = key or f"reply:fb:{conversation.pk}:{uuid.uuid4()}"
    outbound = enqueue_reply(
        conn,
        fb_convo.facebook_contact.psid,
        body,
        action_type=FacebookOutboundMessage.ActionType.DM_REPLY,
        idempotency_key=key,
        media=media,
        human_agent=human,
    )
    if outbound is None:
        outbound = FacebookOutboundMessage.objects.get(idempotency_key=key)
        if human:
            return repeat(outbound)
    if not send_outbound(outbound):
        outbound.refresh_from_db()
        if outbound.status != FacebookOutboundMessage.Status.FAILED:
            # Transient: the outbox drain retries it with backoff (see ReplyAction).
            return _result(outbound, queued=True)
        raise ActionError(f"Facebook send failed: {outbound.last_error}")
    return _result(outbound, record_sent_message(outbound))
