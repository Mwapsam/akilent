"""Celery tasks for the conversation spine."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="celery")
def remind_missed_conversations() -> int:
    """Give every missed WhatsApp conversation a due follow-up (see ``recovery``)."""
    from apps.conversations.recovery import create_missed_followups

    n = create_missed_followups()
    if n:
        logger.info("remind_missed_conversations: created %d follow-up(s)", n)
    return n
