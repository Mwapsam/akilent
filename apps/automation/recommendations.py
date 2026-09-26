"""Build, step 3: "three things you should automate", from the business's own conversations.

Deterministic: every card carries a real number from the last ``WINDOW_DAYS`` of messages ("18
customers asked about prices", "31% of messages arrive when you're closed"). A brand-new business
with no conversations yet gets cards from its "Tell us about your business" answers instead. Cards
already covered by a live automation, or dismissed recently, are left out. Each card names an
intent, so "Review" builds it through ``intents.build_from_intent`` like any other draft.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from django.utils import timezone

from apps.automation import keywords as kw
from apps.automation.intents import INTENTS

WINDOW_DAYS = 14
SHOW = 3
AFTER_HOURS_SHARE = 0.2
SLOW_FIRST_REPLY_MINUTES = 5
MIN_ASKED = 3

# Keyword intents counted from real messages, with how the evidence line names the topic.
_TOPICS = (
    ("answer_pricing", "prices"), ("answer_location", "where you are"), ("answer_hours", "your opening hours"),
    ("answer_delivery", "delivery"), ("answer_payment", "how to pay"),
)
_ALSO_COVERS = {"welcome_new": ("welcome-new-enquiry", "greet-hello", "offer-a-menu")}


def _intent_keywords(intent_key: str) -> list[str]:
    from apps.automation.engagement_starters import STARTERS_BY_KEY

    intent = INTENTS[intent_key]
    return list(intent.get("keywords") or STARTERS_BY_KEY.get(intent.get("starter", ""), {}).get("keywords") or [])


def _slug(intent_key: str) -> str:
    intent = INTENTS[intent_key]
    return intent.get("starter") or intent.get("slug") or ""


def _covered(intent_key: str, live_slugs: set, live_matches: list[dict]) -> bool:
    if _slug(intent_key) in live_slugs or live_slugs & set(_ALSO_COVERS.get(intent_key, ())):
        return True
    words = _intent_keywords(intent_key)
    # Another live automation already answers most of this topic's words.
    return bool(words) and any(sum(kw.matches(m["match"], w) for w in words) * 2 >= len(words) for m in live_matches)


def _hour_label(hour: int) -> str:
    return f"{(hour % 12) or 12}{'am' if hour < 12 else 'pm'}"


def recommend(account, *, now=None, limit: int = SHOW) -> list[dict]:
    """The top ``limit`` cards: ``[{"key", "intent", "title", "evidence", "score", "entities"}]``."""
    from zoneinfo import ZoneInfo

    from apps.accounts import api as accounts_api
    from apps.accounts import business_hours
    from apps.automation import api as automation_api
    from apps.automation import patterns
    from apps.conversations import api as conversations_api

    now = now or timezone.now()
    since = now - timedelta(days=WINDOW_DAYS)
    live_slugs, live_matches = automation_api.published_slugs(account), automation_api.published_trigger_matches(account)
    hidden = patterns.dismissed_keys(account)
    messages = conversations_api.recent_customer_messages(account, since=since)
    cards = []

    def add(intent_key, title, evidence, score, entities=None, key=None):
        key = key or f"intent:{intent_key}"
        if key in hidden or (key.startswith("intent:") and _covered(intent_key, live_slugs, live_matches)):
            return
        cards.append({"key": key, "intent": intent_key, "title": title, "evidence": evidence,
                      "score": score, "entities": entities or {}})

    # What customers keep asking about.
    for intent_key, topic in _TOPICS:
        match = {"mode": "contains", "any": _intent_keywords(intent_key)}
        asked = len({m["conversation_id"] for m in messages if kw.matches(match, m["body"])})
        if asked >= MIN_ASKED:
            add(intent_key, INTENTS[intent_key]["label"],
                f"{asked} customers asked about {topic} in the last {WINDOW_DAYS} days.", asked)

    # Messages that arrive while the business is closed.
    hours = business_hours.get_hours(account)
    if hours and hours.schedule and messages:
        closed = [m for m in messages if not business_hours.open_in(hours, m["timestamp"])]
        share = len(closed) / len(messages)
        if share >= AFTER_HOURS_SHARE:
            try:
                zone = ZoneInfo(hours.timezone)
            except Exception:
                zone = ZoneInfo("UTC")
            late = [m["timestamp"].astimezone(zone).hour for m in closed]
            evening = [h for h in late if h >= 15]
            when = f"after {_hour_label(min(evening))}" if evening else "while you're closed"
            add("reply_when_closed", "Reply when you're closed",
                f"{round(share * 100)}% of customer messages arrive {when}.", len(closed))

    # New customers waiting for a first answer.
    waits = conversations_api.first_reply_waits(account, since=since, now=now)
    if waits:
        answered = [w for w in waits if w is not None]
        unanswered = len(waits) - len(answered)
        typical = median(answered) if answered else None
        if unanswered or (typical is not None and typical > SLOW_FIRST_REPLY_MINUTES):
            evidence = (f"New customers typically wait {round(typical)} minutes for a first reply."
                        if typical is not None and typical > SLOW_FIRST_REPLY_MINUTES
                        else f"{unanswered} new customers are still waiting for a first reply.")
            add("welcome_new", "Welcome new customers", evidence, len(waits))

    # Replies the team keeps sending by hand.
    for pattern in patterns.open_patterns(account)[:3]:
        add("answer_question", f"Answer questions about {pattern.topic}",
            f"Your team has sent the same answer {pattern.count} times.", pattern.count,
            entities={"keywords": pattern.question_keywords, "reply_text": pattern.reply_text,
                      "topic_label": pattern.topic}, key=f"pattern:{pattern.key}")

    # A new business: start from what the owner told us.
    if not messages:
        facts = accounts_api.business_facts(account)
        if accounts_api.business_fact(account, "opening_hours"):
            add("reply_when_closed", "Reply when you're closed",
                "You set opening hours, so customers who write later can know when you'll answer.", 0.3)
        add("welcome_new", "Welcome new customers", "Every first-time customer gets an answer straight away.", 0.2)
        if facts.get("location"):
            add("answer_location", "Answer location questions", "You told us where you are.", 0.25)
        if facts.get("payment_methods"):
            add("answer_payment", "Answer payment questions", "You told us how customers pay.", 0.15)

    cards.sort(key=lambda c: -c["score"])
    seen, top = set(), []
    for card in cards:
        if card["intent"] != "answer_question" and card["intent"] in seen:
            continue
        seen.add(card["intent"])
        top.append(card)
        if len(top) == limit:
            break
    return top
