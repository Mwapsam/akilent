"""BM25-powered product lookup for the workflow engine and AI tools.

``lookup_product(account, query)`` returns a dict with one of three outcomes:

* ``found``     — the top result clearly wins (above threshold and ≥ 0.05 margin over
                   the runner-up, or only one candidate);
* ``ambiguous`` — up to 3 candidates that are close together;
* ``not_found`` — nothing matched the threshold.

Additional ``reason`` keys are used when the winning product has a data problem:

* ``no_price``  — the winning product exists but its price is NULL/missing.

The function NEVER invents prices or currencies.
"""

from __future__ import annotations

from typing import Any

from apps.ai.ranking import (  # noqa: F401 — tokenize re-exported for tests
    BM25Ranker,
    KnowledgeDoc,
    tokenize,
)
from apps.commerce.models import Product

# A score must clear this value to be considered a real match.
_THRESHOLD = 0.1
# The winner must beat the runner-up by at least this margin to be "found"
# (avoids false-positive disambiguation when two products are nearly equal).
_MARGIN = 0.05


def _product_to_doc(p: Product) -> KnowledgeDoc:
    """Build a KnowledgeDoc-shaped dict for the BM25Ranker corpus."""
    title = p.name or ""
    # Product has no description field — use name as both title and content.
    content = p.name or ""
    return {"id": str(p.pk), "title": title, "content": content}


def lookup_product(account, query: str) -> dict[str, Any]:
    """Search active products for *account* using BM25.

    Returns a dict with keys:
      ``outcome``    — "found" | "ambiguous" | "not_found"
      ``product``    — Product instance (when outcome == "found")
      ``candidates`` — list of Product instances (when outcome == "ambiguous")
      ``reason``     — "no_match" | "no_price" | "ambiguous" (when not "found")
    """
    products = list(
        Product.objects.filter(account=account, is_active=True).order_by("name")
    )
    if not products:
        return {"outcome": "not_found", "reason": "no_match"}

    if not (query or "").strip():
        return {"outcome": "not_found", "reason": "no_match"}

    docs = [_product_to_doc(p) for p in products]
    ranker = BM25Ranker(docs)
    scored = ranker.query(query, top_k=10)  # returns [(doc, score), ...]

    # Filter to scores that clear the threshold.
    above = [(doc, score) for doc, score in scored if score >= _THRESHOLD]

    if not above:
        return {"outcome": "not_found", "reason": "no_match"}

    # Map doc id back to product.
    product_by_id = {str(p.pk): p for p in products}

    top_doc, top_score = above[0]
    top_product = product_by_id.get(top_doc["id"])
    if top_product is None:
        return {"outcome": "not_found", "reason": "no_match"}

    # Single candidate: threshold only, no margin check needed.
    if len(above) == 1:
        if top_product.price is None:
            return {
                "outcome": "not_found",
                "reason": "no_price",
                "product": top_product,
            }
        return {"outcome": "found", "product": top_product}

    runner_up_score = above[1][1]
    margin = top_score - runner_up_score

    if margin >= _MARGIN:
        # Clear winner.
        if top_product.price is None:
            return {
                "outcome": "not_found",
                "reason": "no_price",
                "product": top_product,
            }
        return {"outcome": "found", "product": top_product}

    # Ambiguous: return up to 3 candidates.
    candidates = []
    for doc, _score in above[:3]:
        p = product_by_id.get(doc["id"])
        if p is not None:
            candidates.append(p)

    return {"outcome": "ambiguous", "reason": "ambiguous", "candidates": candidates}
