"""
Action registry for chatbot actions.

Downstream apps register callables here from their own AppConfig.ready():

    from apps.chatbot.services.actions import register

    @register("create_support_ticket")
    def _create_support_ticket(session, **kwargs):
        ...

Every registered callable MUST scope all DB lookups to session.chatbot.account.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Callable[..., Any]] = {}


def register(slug: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _REGISTRY[slug] = fn
        return fn

    return decorator


def invoke(slug: str, session: Any, **kwargs: Any) -> dict[str, Any]:
    from apps.chatbot.models import ChatActionExecution, ChatbotAction

    try:
        action = ChatbotAction.objects.get(
            chatbot=session.chatbot, slug=slug, is_enabled=True
        )
    except ChatbotAction.DoesNotExist:
        logger.warning(
            "chatbot action denied: slug=%s not enabled for chatbot=%s",
            slug,
            session.chatbot_id,
        )
        _record_denied(session, slug)
        raise ValueError(f"Action '{slug}' is not enabled for this chatbot.") from None

    if slug not in _REGISTRY:
        logger.error(
            "chatbot action '%s' is enabled in DB for chatbot=%s but has no registered handler",
            slug,
            session.chatbot_id,
        )
        raise ValueError(f"Action '{slug}' is enabled but has no registered handler.")

    safe_inputs = _sanitise_input(slug, kwargs)
    execution = ChatActionExecution.objects.create(
        session=session,
        action=action,
        status=ChatActionExecution.Status.AUTHORIZED,
        input_metadata=safe_inputs,
    )
    try:
        result = _REGISTRY[slug](session=session, **kwargs)
        safe_output = _sanitise_output(slug, result)
        execution.status = ChatActionExecution.Status.SUCCESS
        execution.output_metadata = safe_output
        execution.save(update_fields=["status", "output_metadata"])
        return result
    except Exception as exc:
        execution.status = ChatActionExecution.Status.FAILED
        execution.error = str(exc)[:500]
        execution.save(update_fields=["status", "error"])
        raise


def _record_denied(session: Any, slug: str) -> None:
    from apps.chatbot.models import ChatActionExecution, ChatbotAction

    try:
        action = ChatbotAction.objects.filter(
            chatbot=session.chatbot, slug=slug
        ).first()
        if action:
            ChatActionExecution.objects.create(
                session=session,
                action=action,
                status=ChatActionExecution.Status.DENIED,
            )
    except Exception:
        logger.exception("failed to record denied action execution")


def _sanitise_input(slug: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    # Default: pass through. Registered actions may override via per-slug sanitisers.
    return {k: str(v) for k, v in kwargs.items()}


def _sanitise_output(slug: str, result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return {k: str(v) for k, v in result.items()}
    return {}
