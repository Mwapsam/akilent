"""Deterministic audience classifier: what a comment says commercially.

Separate from moderation on purpose (spec §2, FR3). Moderation decides an *action*
by the account's first matching rule; this decides what the comment *communicates*,
and a comment can say several things. The two never share an output, so growing this
taxonomy can never change what moderation does.

Rules run in order over a working copy of the text. A rule marked ``consume`` blanks
out what it matched, so later rules can't read the same words again: "buy followers"
is spam, not buying intent; "don't buy" is a complaint, not a purchase; "fast
delivery" is satisfaction, not a delivery question.

Term lists are keyed by language. Version 1 ships English only (spec §3); adding a
language is a list, not code. Bump ``CLASSIFIER_VERSION`` whenever a rule changes, so
history stays explainable and the backfill can re-run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .taxonomy import Category, precedence_rank

CLASSIFIER_VERSION = "rules-1"

C = Category


@dataclass(frozen=True)
class Rule:
    category: str | None  # None = an exclusion: consumes text, emits nothing
    subcategory: str
    pattern: re.Pattern
    consume: bool = False
    language: str | None = "en"  # None = language-free (links, emoji)


@dataclass(frozen=True)
class Signal:
    category: str
    subcategory: str
    terms: tuple[str, ...] = ()


@dataclass
class Classification:
    signals: list[Signal] = field(default_factory=list)
    language: str = ""

    @property
    def primary(self) -> Signal | None:
        if not self.signals:
            return None
        # Stable: within a category the first rule that matched wins.
        return min(self.signals, key=lambda s: precedence_rank(s.category))

    def categories(self) -> set[str]:
        return {s.category for s in self.signals}


def _phrases(*phrases: str) -> re.Pattern:
    body = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)


def _rule(category, subcategory, *phrases, consume=False, language="en") -> Rule:
    return Rule(category, subcategory, _phrases(*phrases), consume, language)


def _raw(category, subcategory, regex, *, consume=False, language="en") -> Rule:
    return Rule(
        category, subcategory, re.compile(regex, re.IGNORECASE), consume, language
    )


_PRAISE_EMOJI = "❤️|❤|♥️|♥|😍|🥰|😻|💕|💖|💗|💓|💞|🔥|💯|👌|👏|🙌|🤩|✨|😘"
_COLOURS = (
    "black|white|blue|red|pink|green|yellow|brown|grey|gray|beige|navy|purple|"
    "orange|gold|silver|cream|nude|maroon|khaki"
)

# Order matters: consuming rules must come before the rules whose words they contain.
RULES: dict[str, list[Rule]] = {
    "en": [
        # --- Spam and abuse first: bots' words never count as anything else -------
        _raw(
            C.SPAM_ABUSE, "spam", r"https?://\S+|www\.\S+", consume=True, language=None
        ),
        _rule(
            C.SPAM_ABUSE,
            "spam",
            "follow back",
            "follow for follow",
            "f4f",
            "l4l",
            "like for like",
            "check my profile",
            "check out my profile",
            "check my page",
            "check out my page",
            "check my bio",
            "check out my bio",
            "link in my bio",
            "buy followers",
            "free followers",
            "get followers",
            "real followers",
            "dm me for",
            "message me for",
            "earn money",
            "make money online",
            "crypto",
            "bitcoin",
            "forex",
            "investment opportunity",
            "double your money",
            "passive income",
            consume=True,
        ),
        _rule(
            C.SPAM_ABUSE,
            "promo",
            "promote it on",
            "promote your",
            "brand ambassador",
            "dm to collab",
            "dm us to collab",
            "collab",
            "collaboration",
            "send it to @",
            consume=True,
        ),
        _rule(
            C.SPAM_ABUSE,
            "toxic",
            "idiot",
            "idiots",
            "stupid",
            "loser",
            "losers",
            "moron",
            "dumb",
            "shut up",
            "fuck you",
            "f u",
            "bitch",
            "bastard",
        ),
        # --- Exclusions: phrases that contain an intent word but aren't intent ----
        _rule(
            None,
            "",
            "in order to",
            "order of the day",
            "just looking",
            "just browsing",
            "no thanks",
            "not interested",
            consume=True,
        ),
        # --- Complaints (several contain "buy", "order", "delivery", "price") ------
        _rule(
            C.COMPLAINT,
            "delivery_delay",
            "still waiting",
            "not received",
            "never received",
            "never arrived",
            "hasn't arrived",
            "has not arrived",
            "haven't received",
            "have not received",
            "late delivery",
            "delivery is late",
            "where is my order",
            "where's my order",
            "still no delivery",
            consume=True,
        ),
        _rule(
            C.COMPLAINT,
            "pricing",
            "too expensive",
            "so expensive",
            "overpriced",
            "rip off",
            "ripoff",
            "rip-off",
            "way too much",
            "price is too high",
            consume=True,
        ),
        _rule(
            C.COMPLAINT,
            "service",
            "no reply",
            "no response",
            "nobody replies",
            "no one replies",
            "nobody answers",
            "no one answers",
            "didn't reply",
            "never replied",
            "not replying",
            "ignoring me",
            "ignored my",
            "rude",
            "bad service",
            "poor service",
            "terrible service",
            consume=True,
        ),
        _rule(
            C.COMPLAINT,
            "quality",
            "poor quality",
            "bad quality",
            "cheap quality",
            "low quality",
            "fell apart",
            "broke",
            "broken",
            "damaged",
            "faulty",
            "defective",
            "doesn't work",
            "does not work",
            "stopped working",
            "garbage",
            "rubbish",
            "trash",
            "fake",
            consume=True,
        ),
        _rule(
            C.COMPLAINT,
            "dissatisfaction",
            "don't buy",
            "do not buy",
            "never buying",
            "never again",
            "waste of money",
            "disappointed",
            "disappointing",
            "refund",
            "scam",
            "scammed",
            "fraud",
            "worst",
            "terrible",
            "horrible",
            "awful",
            "unacceptable",
            "not happy",
            "unhappy",
            "regret",
            consume=True,
        ),
        # --- Content feedback (before praise: "love this video" is about the post) --
        _rule(
            C.CONTENT_FEEDBACK,
            "appreciation",
            "great video",
            "nice video",
            "love this video",
            "love the video",
            "love this reel",
            "great reel",
            "great content",
            "love your content",
            "love your posts",
            "great post",
            "nice post",
            "nice photo",
            "great photo",
            "beautiful photo",
            "nice pic",
            "nice pics",
            "great shot",
            "great photography",
            consume=True,
        ),
        _rule(
            C.CONTENT_FEEDBACK,
            "criticism",
            "can't hear",
            "cannot hear",
            "can't see",
            "too fast",
            "blurry",
            "bad lighting",
            "poor lighting",
            "too loud",
            "annoying music",
            "bad video",
            consume=True,
        ),
        _rule(
            C.CONTENT_FEEDBACK,
            "more_content",
            "more videos",
            "more content",
            "post more",
            "show more",
            "make more",
            "more of this",
            "part 2",
            "share more",
            "show us",
            "show how",
            "how does it look",
            "how it looks",
            "on a model",
            "when worn",
            "worn on",
            "tutorial",
            consume=True,
        ),
        # --- Praise: satisfaction first (it contains "delivery", "order") ----------
        _rule(
            C.PRAISE,
            "satisfaction",
            "received my",
            "got mine",
            "got my order",
            "arrived today",
            "happy with",
            "so happy",
            "highly recommend",
            "recommend",
            "great service",
            "excellent service",
            "fast delivery",
            "quick delivery",
            "thank you so much",
            "thanks so much",
            consume=True,
        ),
        _rule(
            C.PRAISE,
            "brand",
            "love your shop",
            "love your store",
            "love your brand",
            "love you guys",
            "you guys are the best",
            "best shop",
            "best store",
            "best brand",
            "favourite shop",
            "favorite shop",
            "keep it up",
            consume=True,
        ),
        _rule(
            C.PRAISE,
            "product",
            "love this",
            "love it",
            "loving this",
            "beautiful",
            "gorgeous",
            "stunning",
            "lovely",
            "cute",
            "amazing",
            "perfect",
            "pretty",
            "obsessed",
            "so good",
            "looks good",
            "nice",
        ),
        _raw(C.PRAISE, "product", _PRAISE_EMOJI, language=None),
        # --- Buying intent -------------------------------------------------------
        _rule(
            C.BUYING_INTENT,
            "price",
            "how much",
            "price",
            "prices",
            "pricing",
            "cost",
            "costs",
            "quote",
            "price please",
            "pp",
        ),
        _rule(
            C.BUYING_INTENT,
            "purchase",
            "i want to buy",
            "want to buy",
            "i'll take",
            "i will take",
            "i want this",
            "i want it",
            "i need this",
            "i need it",
            "buy",
            "purchase",
            "take my money",
        ),
        _rule(
            C.BUYING_INTENT,
            "availability",
            "available",
            "still available",
            "do you have",
            "do you still have",
            "any left",
        ),
        _rule(
            C.BUYING_INTENT,
            "ordering",
            "how do i order",
            "how to order",
            "can i order",
            "place an order",
            "order",
            "pre-order",
            "preorder",
            "reserve",
            "book one",
        ),
        _rule(
            C.BUYING_INTENT,
            "dm_request",
            "dm me",
            "dm",
            "dmd",
            "dm'd",
            "dmed",
            "inbox me",
            "pm me",
            "pm",
            "message me",
            "send me details",
            "send details",
            "check your dm",
            "check your dms",
            "sent you a dm",
        ),
        # --- Product questions ---------------------------------------------------
        _rule(
            C.PRODUCT_ENQUIRY,
            "size",
            "size",
            "sizes",
            "sizing",
            "what size",
            "true to size",
            "plus size",
            "xs",
            "xl",
            "xxl",
            "3xl",
            "4xl",
            "length",
            "measurements",
        ),
        _raw(
            C.PRODUCT_ENQUIRY,
            "colour",
            rf"(?<!\w)(?:colou?rs?|in (?:{_COLOURS}))(?!\w)",
        ),
        _rule(
            C.PRODUCT_ENQUIRY,
            "specs",
            "material",
            "fabric",
            "made of",
            "made from",
            "cotton",
            "leather",
            "dimensions",
            "weight",
            "ingredients",
            "warranty",
            "how does it work",
            "battery",
        ),
        _rule(
            C.PRODUCT_ENQUIRY,
            "stock",
            "in stock",
            "back in stock",
            "restock",
            "restocking",
            "out of stock",
            "sold out",
        ),
        _rule(
            C.PRODUCT_ENQUIRY,
            "delivery",
            "deliver",
            "delivery",
            "deliveries",
            "shipping",
            "ship to",
            "ships to",
            "do you ship",
            "postage",
            "courier",
            "pick up",
            "pickup",
            "collection",
        ),
        _rule(
            C.PRODUCT_ENQUIRY,
            "location",
            "where are you",
            "where are you located",
            "location",
            "address",
            "where is your shop",
            "where is your store",
            "physical shop",
            "physical store",
            "where can i find",
        ),
        # --- General engagement --------------------------------------------------
        _rule(
            C.GENERAL,
            "greeting",
            "hi",
            "hello",
            "hey",
            "hiya",
            "good morning",
            "good afternoon",
            "good evening",
        ),
        _rule(C.GENERAL, "chit_chat", "lol", "haha", "hahaha", "wow", "omg", "same"),
    ],
}

_MENTION = re.compile(r"@[\w.]+")
_WORD = re.compile(r"\w", re.UNICODE)
# Signals a spam or promo match wipes from the same comment: bots flatter (spec §3).
_DROPPED_BY_SPAM = {C.PRAISE, C.CONTENT_FEEDBACK, C.GENERAL}
_MAX_TERMS = 5


# Curly apostrophes (U+2018, U+2019) read as a plain one.
_APOSTROPHES = {0x2018: "'", 0x2019: "'"}


def _normalise(text: str) -> str:
    text = text.translate(_APOSTROPHES)
    return re.sub(r"\s+", " ", text).strip()


def classify(text: str) -> Classification:
    """Every audience signal in ``text``. Empty text gets no signals."""
    text = _normalise(text or "")
    if not text:
        return Classification()

    found: dict[tuple[str, str], list[str]] = {}
    languages: list[str] = []
    for language, rules in RULES.items():
        working = text
        for rule in rules:
            matches = list(rule.pattern.finditer(working))
            if not matches:
                continue
            if rule.category is not None:
                terms = found.setdefault((rule.category, rule.subcategory), [])
                for m in matches:
                    term = m.group(0).lower()
                    if term not in terms and len(terms) < _MAX_TERMS:
                        terms.append(term)
                if rule.language and rule.language not in languages:
                    languages.append(rule.language)
            if rule.consume:
                for m in reversed(matches):
                    working = (
                        working[: m.start()]
                        + " " * (m.end() - m.start())
                        + working[m.end() :]
                    )

    signals = [Signal(cat, sub, tuple(terms)) for (cat, sub), terms in found.items()]

    spammy = any(
        s.category == C.SPAM_ABUSE and s.subcategory in {"spam", "promo"}
        for s in signals
    )
    if spammy:
        signals = [s for s in signals if s.category not in _DROPPED_BY_SPAM]

    # General engagement only when nothing more specific was said.
    if any(s.category != C.GENERAL for s in signals):
        signals = [s for s in signals if s.category != C.GENERAL]
    elif not signals:
        without_mentions = _MENTION.sub("", text)
        if _MENTION.search(text) and not _WORD.search(without_mentions):
            signals = [Signal(C.GENERAL, "chit_chat", ("@mention",))]
        else:
            signals = [Signal(C.GENERAL, "other")]

    return Classification(signals=signals, language=languages[0] if languages else "")
