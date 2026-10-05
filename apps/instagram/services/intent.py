from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def evaluate_intent(
    account, conversation, body: str, source_description: str = ""
) -> None:
    """
    Run buying-intent detection. If a phrase is matched, create an AIProposal
    for staff review. A Lead is never created automatically.
    """
    from apps.conversations.intent import detect_buying_intent

    intent_phrase = detect_buying_intent(body)
    if not intent_phrase:
        return

    logger.info(
        "Instagram intent detected (%s) in account=%s conversation=%s phrase=%r",
        source_description,
        account.pk,
        conversation.pk,
        intent_phrase,
    )
    _create_purchase_proposal(account, conversation)


def _create_purchase_proposal(account, conversation) -> None:
    from apps.ai.models import AIProposal

    exists = AIProposal.objects.filter(
        account=account,
        conversation=conversation,
        action="purchase_intent",
        status=AIProposal.Status.PENDING,
    ).exists()
    if exists:
        return

    AIProposal.objects.create(
        account=account,
        conversation=conversation,
        action="purchase_intent",
        confidence=1.0,
        payload={"source": "instagram", "channel": "instagram"},
    )
