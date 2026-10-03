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
    from apps.chatbot.models import ChatbotConfig

logger = logging.getLogger(__name__)

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
