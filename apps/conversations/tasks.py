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


@shared_task(queue="celery")
def capture_benchmarks() -> int:
    """Measure each business's starting week and day-30 week once they've closed (``benchmarks``)."""
    from apps.conversations import benchmarks
    from apps.whatsapp import api as whatsapp_api

    made = 0
    for account, connected_at in whatsapp_api.connected_accounts():
        try:
            made += len(benchmarks.capture(account, connected_at))
        except Exception:
            logger.exception("capture_benchmarks: failed for account %s", account.pk)
    return made
