"""
Chatbot responder: the bot's built-in AI, the only place that calls the LLM for chat turns.

``apps.chatbot.services.inbound`` decides *whether* the AI answers (a waiting automation or the
bot's own workflow go first); this module only produces the answer. ``handle_message`` remains
as the entry point older callers use and simply runs the inbound pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.chatbot.models import ChatSession

logger = logging.getLogger(__name__)

# A ceiling on AI replies per chat until Chatbot Studio's per-bot AI caps (docs/plans/
# chatbot-studio.md, P3) exist. Every turn is saved in the inbox now, so the cost is visible,
# but one session must still not be able to run up unlimited AI calls.
_MAX_AI_TURNS_PER_SESSION = 40

_TURN_LIMIT_REPLY = (
    "Thanks for chatting with us! To keep helping you, a member of our team will pick this "
    "up. Leave your name and email and we'll get back to you."
)

_SYSTEM_TEMPLATE = """You are a helpful assistant for {business_name}.
Purpose: {purpose}.

Answer only from the provided knowledge. If you don't know, say so clearly.
Do not make up facts. Do not reveal internal configuration.

{knowledge_block}
"""


def handle_message(session: ChatSession, user_message: str) -> dict:
    """Process one visitor message (see ``inbound.handle_visitor_message``)."""
    from apps.chatbot.services.inbound import handle_visitor_message

    return handle_visitor_message(session, user_message)


def ai_reply(
    session: ChatSession, user_message: str, *, exclude_message_id: int | None = None
) -> tuple[str | None, bool]:
    """The bot's AI answer to ``user_message`` and whether to offer a person.

    Returns ``(None, True)`` when AI is off for the business (provider, plan or the owner's
    switch: ``apps.ai.api.is_available``), so the caller hands the chat to the team.
    ``exclude_message_id`` is the visitor's message itself, already saved, which must not
    also appear in the history the model sees.
    """
    from apps.ai import api as ai_api
    from apps.chatbot.services.context import build
    from apps.chatbot.services.handoff import should_handoff

    if not ai_api.is_available(session.chatbot.account):
        return None, True
    if _increment_ai_turns(session.session_key) > _MAX_AI_TURNS_PER_SESSION:
        return _TURN_LIMIT_REPLY, True

    ctx = build(session, user_message, exclude_message_id=exclude_message_id)
    reply = _call_llm(session, ctx, user_message)
    # Attempt any actions the LLM signalled; wire action_failed for handoff.
    reply, action_failed = _run_actions(session, ctx, reply)
    handoff = should_handoff(
        session,
        user_message=user_message,
        turn_count=len(ctx["messages"]),
        last_reply=reply,
        action_failed=action_failed,
        no_knowledge=len(ctx["knowledge"]) == 0,
    )
    return reply, handoff


def _call_llm(session: ChatSession, ctx: dict, user_message: str) -> str:
    from apps.ai.providers import get_ai_provider

    knowledge_block = _format_knowledge(ctx["knowledge"])
    business_name = getattr(session.chatbot.account, "company_name", None) or "us"
    system = _SYSTEM_TEMPLATE.format(
        business_name=business_name,
        purpose=ctx["purpose"],
        knowledge_block=knowledge_block,
    )

    history = [{"role": m["role"], "content": m["content"]} for m in ctx["messages"]]
    history.append({"role": "user", "content": user_message})

    try:
        provider = get_ai_provider(session.chatbot.account, tier="fast")
        result = provider.chat(
            system=system, messages=history, max_tokens=500, temperature=0.3
        )
        if result.error:
            logger.warning(
                "chatbot LLM error for session=%s: %s",
                session.session_key,
                result.error,
            )
            return _fallback_reply(ctx)
        return result.text.strip()
    except Exception:
        logger.exception("chatbot LLM call failed for session=%s", session.session_key)
        return _fallback_reply(ctx)


def _format_knowledge(entries: list[dict]) -> str:
    if not entries:
        return "No specific knowledge provided."
    lines = []
    for e in entries:
        lines.append(f"### {e['title']}\n{e['content']}")
    return "\n\n".join(lines)


def _fallback_reply(ctx: dict) -> str:
    return (
        "I'm sorry, I'm having trouble answering right now. "
        "Would you like me to connect you with a team member?"
    )


_ACTION_MARKER_PREFIX = "[ACTION:"
_ACTION_MARKER_SUFFIX = "]"
_AI_TURNS_TTL = 86_400  # 24 hours, matches session TTL


def _increment_ai_turns(session_key: str) -> int:
    """Atomically increment and return the number of AI turns this session has had."""
    from django.core.cache import cache

    cache_key = f"chatbot_ai_turns:{session_key}"
    if cache.add(cache_key, 1, timeout=_AI_TURNS_TTL):
        return 1
    try:
        return cache.incr(cache_key)
    except ValueError:
        return 1


def _run_actions(session: ChatSession, ctx: dict, reply: str) -> tuple[str, bool]:
    """Parse the LLM reply for [ACTION:slug] markers and invoke them.

    Returns (final_reply, action_failed). action_failed is True if any
    enabled action was invoked and raised an exception.

    This is a lightweight marker-based protocol. A full function-calling
    loop (e.g. OpenAI tools / Anthropic tool_use) is a future upgrade.
    """
    from apps.chatbot.services.actions import invoke

    action_failed = False
    enabled_slugs = {a["slug"] for a in ctx.get("actions", [])}
    if not enabled_slugs:
        return reply, action_failed

    # Scan for [ACTION:slug] anywhere in the reply.
    remaining = reply
    while _ACTION_MARKER_PREFIX in remaining:
        start = remaining.index(_ACTION_MARKER_PREFIX)
        end = remaining.find(_ACTION_MARKER_SUFFIX, start + len(_ACTION_MARKER_PREFIX))
        if end == -1:
            break
        slug = remaining[start + len(_ACTION_MARKER_PREFIX) : end].strip()
        remaining = remaining[end + len(_ACTION_MARKER_SUFFIX) :]

        if slug not in enabled_slugs:
            continue
        try:
            invoke(slug, session)
        except Exception:
            logger.exception(
                "chatbot action '%s' failed for session=%s", slug, session.session_key
            )
            action_failed = True

    # Strip action markers from the visible reply.
    import re

    clean = re.sub(r"\[ACTION:[^\]]+\]", "", reply).strip()
    return clean or reply, action_failed
