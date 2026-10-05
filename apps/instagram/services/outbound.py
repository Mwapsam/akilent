from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from apps.instagram.models.account import InstagramBusinessAccount
from apps.instagram.models.message import OutboundMessage

logger = logging.getLogger(__name__)


def enqueue_reply(
    instagram_account: InstagramBusinessAccount,
    recipient_igsid: str,
    body: str,
    *,
    action_type: str,
    idempotency_key: str,
) -> OutboundMessage | None:
    """
    Create an OutboundMessage record (outbox-first pattern).

    Returns the new OutboundMessage, or None if the idempotency key already
    exists (meaning this send was already enqueued or sent — safe to ignore).
    """
    try:
        with transaction.atomic():
            msg = OutboundMessage.objects.create(
                instagram_account=instagram_account,
                idempotency_key=idempotency_key,
                recipient_igsid=recipient_igsid,
                action_type=action_type,
                body=body,
            )
        return msg
    except IntegrityError:
        logger.debug(
            "OutboundMessage already exists for idempotency_key=%s", idempotency_key
        )
        return None


def send_outbound(outbound: OutboundMessage) -> bool:
    """
    Attempt to send one OutboundMessage via the Meta provider.

    Updates OutboundMessage status from the SendResult.
    Returns True if sent successfully.
    """
    from apps.instagram.providers.meta import MetaInstagramProvider

    if outbound.status == OutboundMessage.Status.SENT:
        return True  # already sent — idempotent

    instagram_account = outbound.instagram_account
    if not instagram_account.access_token:
        logger.error(
            "send_outbound: instagram_account %s has no access_token",
            instagram_account.pk,
        )
        return False
    provider = MetaInstagramProvider(
        access_token=instagram_account.access_token,
        instagram_account_id=instagram_account.instagram_business_account_id,
    )

    eligibility = provider.can_send(outbound.recipient_igsid, outbound.action_type)
    if not eligibility.eligible:
        outbound.mark_failed(
            f"Not eligible to send: {eligibility.reason}", terminal=True
        )
        return False

    outbound.status = OutboundMessage.Status.SENDING
    outbound.save(update_fields=["status"])

    if outbound.action_type == OutboundMessage.ActionType.PRIVATE_REPLY:
        # private_reply expects a comment_id, not an igsid; the caller must
        # pass the comment_id as recipient_igsid for private reply actions.
        result = provider.private_reply(outbound.recipient_igsid, outbound.body)
    else:
        result = provider.send_message(outbound.recipient_igsid, outbound.body)

    if result.success:
        outbound.mark_sent(result.provider_message_id)
        return True
    outbound.mark_failed(result.error, terminal=result.terminal)
    return False
