"""Celery tasks for the website chatbot."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="celery")
def purge_idle_chat_sessions(days: int = 30) -> int:
    """Delete website chat sessions in which the visitor never wrote anything.

    The widget opens a session on every first page view, so most sessions are someone who never
    chatted. Those hold nothing but a key and timestamps. A session where the visitor did write
    has a contact and a conversation, which belong to the business's inbox and are kept.
    """
    import datetime

    from django.utils import timezone

    from apps.chatbot.models import ChatSession

    cutoff = timezone.now() - datetime.timedelta(days=days)
    deleted, _ = ChatSession.objects.filter(
        last_activity_at__lt=cutoff,
        conversation__isnull=True,
        contact__isnull=True,
    ).delete()
    if deleted:
        logger.info("purged %d idle website chat sessions", deleted)
    return deleted


@shared_task(queue="celery")
def notify_handoff(session_id: int) -> int:
    """Email the team that a website chat was handed to them (services/handoff.py)."""
    from apps.chatbot.models import ChatSession
    from apps.chatbot.services.handoff import tell_the_team

    session = (
        ChatSession.objects.select_related("chatbot", "conversation__contact")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return 0
    return tell_the_team(session)
