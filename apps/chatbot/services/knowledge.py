"""
Knowledge retrieval for chatbot responses.

Retrieves active KnowledgeBaseEntry rows linked to the chatbot's configured
knowledge sources and ranks them by simple keyword overlap with the query.
No vector embedding — keyword matching is deterministic, explainable, and
sufficient for an FAQ-scale knowledge base. Upgrade to semantic search here
later without touching callers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.ai.models import KnowledgeBaseEntry
    from apps.chatbot.models import ChatbotConfig

logger = logging.getLogger(__name__)


def link_knowledge(chatbot: ChatbotConfig, entry: KnowledgeBaseEntry) -> None:
    """Link a knowledge entry to a chatbot, enforcing ownership invariants.

    Raises ValueError if the entry and chatbot belong to different accounts,
    or if the chatbot type conflicts with the knowledge account type
    (system ↔ customer isolation).
    """
    from apps.chatbot.models import ChatbotConfig, ChatbotKnowledgeSource

    if chatbot.account_id != entry.account_id:
        raise ValueError("Knowledge entry and chatbot must belong to the same account.")
    # account_id equality is already confirmed above, so chatbot.account and
    # entry.account are the same row — use chatbot.account to avoid a second
    # DB hit (chatbot is more likely to be select_related by the caller).
    is_platform = chatbot.account.is_platform_account
    if chatbot.chatbot_type == ChatbotConfig.ChatbotType.SYSTEM and not is_platform:
        raise ValueError("System chatbots may only use platform knowledge entries.")
    if chatbot.chatbot_type == ChatbotConfig.ChatbotType.CUSTOMER and is_platform:
        raise ValueError("Customer chatbots may not use platform knowledge entries.")
    ChatbotKnowledgeSource.objects.get_or_create(
        chatbot=chatbot, knowledge_entry=entry, defaults={"is_active": True}
    )


def unlink_knowledge(chatbot: ChatbotConfig, entry: KnowledgeBaseEntry) -> None:
    """Remove a knowledge entry link from a chatbot."""
    from apps.chatbot.models import ChatbotKnowledgeSource

    ChatbotKnowledgeSource.objects.filter(
        chatbot=chatbot, knowledge_entry=entry
    ).delete()


_MAX_ENTRIES = 5
_MIN_OVERLAP = 1  # at least one keyword must match


def retrieve(
    chatbot: ChatbotConfig, query: str, *, max_results: int = _MAX_ENTRIES
) -> list[dict]:
    """Return a ranked list of relevant knowledge entries for this query.

    Each item: {"title": str, "content": str, "source_type": str, "score": int}
    Returns an empty list if no entries match.
    """
    from apps.ai.models import KnowledgeBaseEntry
    from apps.chatbot.models import ChatbotKnowledgeSource

    entry_ids = ChatbotKnowledgeSource.objects.filter(
        chatbot=chatbot, is_active=True
    ).values_list("knowledge_entry_id", flat=True)

    if not entry_ids:
        return []

    entries = KnowledgeBaseEntry.objects.filter(
        id__in=entry_ids,
        is_active=True,
        account=chatbot.account,
    )

    query_tokens = _tokenise(query)
    if not query_tokens:
        return []

    ranked = []
    for entry in entries:
        score = _overlap(
            query_tokens, _tokenise(entry.title) | _tokenise(entry.content)
        )
        if score >= _MIN_OVERLAP:
            ranked.append(
                {
                    "title": entry.title,
                    "content": entry.content,
                    "source_type": entry.source_type,
                    "score": score,
                }
            )

    ranked.sort(key=lambda x: x["score"] or 0, reverse=True)  # type: ignore[arg-type, return-value]
    return ranked[:max_results]


def _tokenise(text: str) -> set[str]:
    return {w.lower() for w in text.split() if len(w) >= 3}


def _overlap(a: set[str], b: set[str]) -> int:
    return len(a & b)
