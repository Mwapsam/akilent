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


def retrieve(
    chatbot: ChatbotConfig, query: str, *, max_results: int = _MAX_ENTRIES
) -> list[dict]:
    """Return a ranked list of relevant knowledge entries for this query.

    Uses BM25 (``apps.ai.ranking``) over the same candidate set as
    ``apps.ai.facts.build`` — all active entries linked to the chatbot.
    Falls back to keyword overlap when BM25 is not enabled for the account.

    Each item: {"title": str, "content": str, "source_type": str, "score": float}
    Returns an empty list if no entries match.
    """
    from apps.ai.models import KnowledgeBaseEntry
    from apps.ai import ranking
    from apps.chatbot.models import ChatbotKnowledgeSource

    entry_ids = list(
        ChatbotKnowledgeSource.objects.filter(
            chatbot=chatbot, is_active=True
        ).values_list("knowledge_entry_id", flat=True)
    )
    if not entry_ids:
        return []

    entries = list(
        KnowledgeBaseEntry.objects.filter(
            id__in=entry_ids, is_active=True, account=chatbot.account
        ).values("pk", "title", "content", "source_type")
    )
    if not entries:
        return []

    doc_ids = {f"k{e['pk']}" for e in entries}
    source_type_map = {f"k{e['pk']}": e["source_type"] for e in entries}
    docs = [
        {"id": f"k{e['pk']}", "title": e["title"], "content": e["content"]}
        for e in entries
    ]

    # Empty query → nothing is relevant; return early.
    from apps.ai.facts import terms as _terms

    if not query or not _terms(query):
        return []

    # Use the account-level cached ranker (None when BM25 is disabled).
    # Query the whole account index with a top_k large enough to always include every
    # linked entry regardless of how other chatbots' entries rank, then filter down.
    ranker = ranking.get_cached_ranker(chatbot.account)
    if ranker is not None:
        # top_k = all linked entries × some headroom so none are cut before the filter.
        # top_k = full corpus size so no linked entry is cut before the filter,
        # regardless of how other chatbots' entries rank in the account index.
        all_results = ranker.query(query, top_k=max(len(ranker._docs), len(docs)))
        results = [(doc, score) for doc, score in all_results if doc["id"] in doc_ids][:max_results]
        if results:
            return [
                {
                    "title": doc["title"],
                    "content": doc["content"],
                    "source_type": source_type_map.get(doc["id"], ""),
                    "score": round(score, 4),
                }
                for doc, score in results
            ]
        # BM25 returned nothing (all-stopword query or wording mismatch on small corpus);
        # fall through to keyword overlap so entries are never silently dropped.

    # Fallback: keyword overlap — rank first, then truncate, then filter to matches only.
    from apps.ai.facts import select_knowledge

    wanted = _terms(query)
    # select_knowledge ranks by overlap when entries don't fit the budget; here we run it
    # over the full linked set so the best-matching entry wins regardless of its DB order.
    chosen = [
        e for e in select_knowledge(docs, query)
        if wanted & (_terms(e["title"]) | _terms(e["content"]))
    ][:max_results]
    return [
        {
            "title": doc["title"],
            "content": doc["content"],
            "source_type": source_type_map.get(doc["id"], ""),
            "score": 0.0,
        }
        for doc in chosen
    ]
