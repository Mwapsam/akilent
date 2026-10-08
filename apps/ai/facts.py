"""The business's facts, structured: what an automatic reply may state, in a form code can check.

The prompt gets these as JSON next to the owner's free-text notes, and ``apps.ai.autonomy`` checks
an automatic reply against them. A price is accepted because it *is* a catalogue price (or an
amount the owner wrote in the notes), a time because it *is* an opening or closing time, not
because the same digits happen to appear somewhere.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MAX_PRODUCTS = 30
# How much of the business's Q&A goes into one prompt. When everything fits it all goes; past
# that, the entries closest to the customer's question win (see ``select_knowledge``).
MAX_KNOWLEDGE_CHARS = 12000
MAX_KNOWLEDGE_ENTRIES = 60
_TITLE_WEIGHT = 3
_WORD = re.compile(r"[^\W_]+", re.U)
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "got",
        "had",
        "has",
        "have",
        "hello",
        "hi",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "please",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "up",
        "us",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
    ]
)
_SUFFIXES = ("ing", "ers", "er", "ed", "es", "s")

# "K18,000", "ZMW 250", "$12.50", "250 kwacha", "1,200 USD"
_CURRENCY = r"(?:K|ZMW|USD|US\$|\$|KES|KSh|NGN|₦|R|ZAR|GHS|£|€|TZS|UGX|MWK)"
MONEY = re.compile(
    rf"(?<![\w.]){_CURRENCY}\s?(\d[\d,]*(?:\.\d+)?)(?!\w)"
    r"|(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(?:kwacha|dollars?|zmw|usd|kes|ngn|zar)\b",
    re.I,
)
# "08:00", "8am", "5 pm", "17:30"
TIME = re.compile(
    r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b|(?<!\d)(\d{1,2}):(\d{2})(?!\d)", re.I
)


def amount(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def amounts_in(text: str) -> set:
    return {
        a
        for m in MONEY.finditer(text or "")
        if (a := amount(m.group(1) or m.group(2))) is not None
    }


def minutes_of(match) -> int | None:
    if match.group(4) is not None:
        hours, minutes = int(match.group(4)), int(match.group(5))
    else:
        hours, minutes = int(match.group(1)), int(match.group(2) or 0)
        suffix = match.group(3).lower()
        if hours > 12:
            return None
        hours = hours % 12 + (12 if suffix == "pm" else 0)
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def times_in(text: str) -> set:
    return {t for m in TIME.finditer(text or "") if (t := minutes_of(m)) is not None}


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def terms(text: str) -> set:
    """The words in ``text`` that say what it's about: lowercased, stopwords out, lightly stemmed,
    so "joining as a freelancer" and "join as freelancers" share their terms."""
    return {
        _stem(w)
        for w in _WORD.findall((text or "").lower())
        if w not in _STOPWORDS and len(w) > 1
    }


def all_knowledge(account) -> list[dict]:
    """Every active Q&A the business has written, as ``{"id", "title", "content"}``.

    ``KnowledgeBaseEntry`` rows (ids ``k<pk>``), then the FAQs and common questions on
    ``accounts.BusinessKnowledge`` (``f<n>`` / ``q<n>``). An id is what the model cites in a
    proposal's ``sources``.
    """
    from apps.accounts.models import BusinessKnowledge
    from apps.ai.models import KnowledgeBaseEntry

    out = [
        {"id": f"k{pk}", "title": title, "content": content}
        for pk, title, content in KnowledgeBaseEntry.objects.filter(
            account=account, is_active=True
        )
        .order_by("title", "pk")
        .values_list("pk", "title", "content")
    ]
    bk = BusinessKnowledge.objects.filter(account=account).first()
    if bk is not None:
        for prefix, items in (("f", bk.faqs), ("q", bk.common_questions)):
            for n, item in enumerate(items if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                q = str(item.get("q") or item.get("question") or "").strip()
                a = str(item.get("a") or item.get("answer") or "").strip()
                if q and a:
                    out.append({"id": f"{prefix}{n}", "title": q, "content": a})
    return out


def select_knowledge(entries: list[dict], query: str = "") -> list[dict]:
    """The entries to put in front of the model for a customer asking ``query``.

    All of them when they fit ``MAX_KNOWLEDGE_CHARS``; otherwise the best word matches with the
    question (a match in the question/title counts three times one in the answer), then the rest
    by title until the budget is used. Plain, deterministic matching: the 200th entry still wins
    when it's the one being asked about.
    """
    size = sum(len(e["title"]) + len(e["content"]) for e in entries)
    if size <= MAX_KNOWLEDGE_CHARS and len(entries) <= MAX_KNOWLEDGE_ENTRIES:
        return list(entries)
    wanted = terms(query)

    def score(e):
        return _TITLE_WEIGHT * len(wanted & terms(e["title"])) + len(
            wanted & terms(e["content"])
        )

    ranked = sorted(enumerate(entries), key=lambda pair: (-score(pair[1]), pair[0]))
    chosen: list[dict] = []
    used = 0
    for _i, e in ranked:
        cost = len(e["title"]) + len(e["content"])
        if used + cost > MAX_KNOWLEDGE_CHARS and chosen:
            continue
        chosen.append(e)
        used += cost
        if len(chosen) == MAX_KNOWLEDGE_ENTRIES:
            break
    return chosen


def matches_question(entries: list[dict], query: str) -> bool:
    """Whether any entry's question shares a meaningful word with the customer's message."""
    wanted = terms(query)
    return bool(wanted) and any(wanted & terms(e["title"]) for e in entries)


def build(account, *, business_notes: str = "", query: str = "") -> dict:
    """``{"opening_hours", "timezone", "products", "knowledge", "business", "notes"}`` for this
    business.

    ``business`` is the owner's profile answers (location, payment methods, delivery, website).
    Products only when Commerce is on. ``knowledge`` is the owner's written Q&A
    (``all_knowledge``) — the same trust level as ``notes``, just organized — chosen for the
    customer's message ``query`` when there's too much to send it all.
    """
    from apps.accounts import api as accounts_api
    from apps.accounts import business_hours
    from apps.billing import api as billing_api

    hours = business_hours.get_hours(account)
    products = []
    if billing_api.usable(account, "orders"):
        from apps.core.actions import ActionError, run_action

        try:
            products = run_action(
                "lookup_products",
                {"account": account},
                account=account,
                query="*",
                limit=MAX_PRODUCTS,
            ).get("products", [])
        except ActionError:
            products = []
    knowledge = select_knowledge(all_knowledge(account), query)
    return {
        "opening_hours": dict(hours.schedule) if hours and hours.schedule else {},
        "timezone": hours.timezone if hours and hours.schedule else "",
        "products": products,
        "knowledge": knowledge,
        "business": accounts_api.business_facts(account),
        "notes": business_notes or "",
    }


def written_text(facts: dict) -> str:
    """Everything the owner wrote themselves (notes, profile answers, and Q&A), for text-level
    checks."""
    return "\n".join(
        [
            facts.get("notes", ""),
            *[str(v) for v in (facts.get("business") or {}).values()],
            *[
                f"{e.get('title', '')} {e.get('content', '')}"
                for e in facts.get("knowledge", [])
            ],
        ]
    )


def allowed_amounts(facts: dict, extra_text: str = "") -> set:
    """Prices a reply may quote: catalogue prices, amounts in the owner's notes or a look-up."""
    out = {
        a
        for p in facts.get("products", [])
        if (a := amount(str(p.get("price", "")))) is not None
    }
    return (
        out
        | amounts_in(written_text(facts))
        | amounts_in(extra_text)
        | {
            a
            for a in (
                amount(v)
                for v in re.findall(r'"price":\s*"([\d.]+)"', extra_text or "")
            )
            if a is not None
        }
    )


def allowed_times(facts: dict, extra_text: str = "") -> set:
    """Times a reply may state: opening and closing times, and times in the notes or a look-up."""
    out = set()
    for window in (facts.get("opening_hours") or {}).values():
        out |= times_in(f"{window.get('open', '')} {window.get('close', '')}")
    return out | times_in(written_text(facts)) | times_in(extra_text)
