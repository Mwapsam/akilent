"""BM25 knowledge ranker shared by ``apps.ai.facts`` and ``apps.chatbot.services.knowledge``.

Both retrieval paths call ``get_cached_ranker(account)`` over the same candidate set
(all active ``KnowledgeBaseEntry`` rows for the account), so they always agree on which
entries are most relevant to a given query.

The index is cached in Redis under ``kb_bm25:{account_id}:{version}``. The version is
incremented in the same transaction as every knowledge mutation (``apps.ai.knowledge_write``),
so an old key is never read after data changes — it just expires quietly after 1 hour.
"""
from __future__ import annotations

import math
from typing import TypedDict

from django.core.cache import cache

# BM25 hyperparameters
_K1 = 1.5
_B = 0.75
_TITLE_WEIGHT = 2   # title terms count this many times in per-document TF
_MIN_SCORE = 0.1    # entries scoring below this are not returned
_CACHE_TTL = 3600   # 1 hour

# Import tokenization primitives from facts so both paths share the same vocabulary.
from apps.ai.facts import _WORD, _STOPWORDS, _stem  # noqa: E402


def tokenize(text: str) -> list[str]:
    """Tokenize for BM25: lowercase, strip stopwords, lightly stem.

    Uses the same primitives as ``apps.ai.facts.terms`` so BM25 and keyword-overlap
    ranking always agree on vocabulary.
    """
    return [
        _stem(w)
        for w in _WORD.findall((text or "").lower())
        if w not in _STOPWORDS and len(w) > 1
    ]


class KnowledgeDoc(TypedDict):
    id: str
    title: str
    content: str


class BM25Ranker:
    """Offline BM25 index over a fixed corpus of knowledge entries.

    Build once per version, cache in Redis, query many times.
    """

    def __init__(self, docs: list[KnowledgeDoc]) -> None:
        self._docs = docs
        n = len(docs)

        self._doc_terms: list[dict[str, int]] = []
        self._doc_lens: list[int] = []

        if not n:
            self._avgdl = 1.0
            self._idf: dict[str, float] = {}
            return

        df: dict[str, int] = {}

        for doc in docs:
            title_toks = tokenize(doc["title"])
            content_toks = tokenize(doc["content"])
            tf: dict[str, int] = {}
            for t in title_toks:
                tf[t] = tf.get(t, 0) + _TITLE_WEIGHT
            for t in content_toks:
                tf[t] = tf.get(t, 0) + 1
            self._doc_terms.append(tf)
            self._doc_lens.append(_TITLE_WEIGHT * len(title_toks) + len(content_toks))
            for term in tf:
                df[term] = df.get(term, 0) + 1

        self._avgdl = sum(self._doc_lens) / n
        self._idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
            for term, freq in df.items()
        }

    def query(self, text: str, *, top_k: int = 60) -> list[tuple[KnowledgeDoc, float]]:
        """Return ``(doc, score)`` pairs sorted by descending score ≥ ``_MIN_SCORE``."""
        q_terms = tokenize(text)
        if not q_terms or not self._docs:
            return []

        avgdl = self._avgdl or 1.0
        results: list[tuple[KnowledgeDoc, float]] = []

        for i, doc in enumerate(self._docs):
            tf_doc = self._doc_terms[i]
            dl = self._doc_lens[i]
            norm = 1 - _B + _B * dl / avgdl
            score = 0.0
            for term in q_terms:
                if term not in tf_doc:
                    continue
                idf = self._idf.get(term, 0.0)
                tf = tf_doc[term]
                score += idf * tf * (_K1 + 1) / (tf + _K1 * norm)
            if score >= _MIN_SCORE:
                results.append((doc, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


def get_cached_ranker(account) -> BM25Ranker | None:
    """Return a BM25Ranker for the account if BM25 is enabled, else ``None``.

    The index is built from the committed ``KnowledgeBaseEntry`` rows and stored
    in the cache under ``kb_bm25:{account_id}:{version}``.  After building, the
    version is re-read; if it changed during the build the result is returned but
    not cached — the next caller will get a fresh build from the new version.
    """
    from apps.ai.models import AISettings, KnowledgeBaseEntry

    try:
        ai_settings = AISettings.objects.get(account=account)
    except AISettings.DoesNotExist:
        return None

    if not ai_settings.bm25_enabled:
        return None

    version = ai_settings.knowledge_version
    cache_key = f"kb_bm25:{account.pk}:{version}"
    ranker: BM25Ranker | None = cache.get(cache_key)
    if ranker is not None:
        return ranker

    entries = list(
        KnowledgeBaseEntry.objects.filter(account=account, is_active=True)
        .order_by("title", "pk")
        .values("pk", "title", "content")
    )
    docs: list[KnowledgeDoc] = [
        {"id": f"k{e['pk']}", "title": e["title"], "content": e["content"]}
        for e in entries
    ]
    ranker = BM25Ranker(docs)

    # Re-read version to detect a mutation that raced with the build (Revision 6).
    current = (
        AISettings.objects.filter(account=account)
        .values_list("knowledge_version", flat=True)
        .first()
    )
    if current == version:
        cache.set(cache_key, ranker, timeout=_CACHE_TTL)

    return ranker
