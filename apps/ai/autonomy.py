"""Replying on its own ("autopilot"): whether AI may send a proposal without a person, by fixed rules.

The owner chooses "reply automatically to the questions I choose" and ticks the kinds of question.
Even then a reply is sent only if **every** check below passes; otherwise it falls back to today's
behaviour, a suggestion a person reviews. Each decision (every check, pass or fail) is stored on the
proposal, so the owner can always see why AI did or didn't send (see ``api.autopilot_report``).

The model's own judgement is one input (its confidence), never the decision. The check that matters
most is ``facts``, against the business's *structured* facts (``apps.ai.facts``):

* a price must be a catalogue price, or an amount the owner wrote in the notes;
* a time must be an opening or closing time, or one written in the notes;
* any other number, and any link, must appear in what the business wrote or a look-up returned.

The customer's own words never count: agreeing with a price they suggested is exactly the mistake
this exists to stop.

Rhythm: at most one automatic reply per customer message, none within ``TEAM_QUIET_MINUTES`` of a
reply from the team (a person or an automation), and after ``MAX_IN_A_ROW`` automatic replies in a
row a person takes over.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.ai import facts as business_facts

# The kinds of question an owner may let AI answer alone, safest first ("other" never is).
TOPICS = {
    "hours": "Opening hours and whether you're open",
    "location": "Where you are and how to find you",
    "payment": "How to pay",
    "greeting": "Greetings and thank-yous",
    "product_info": "What you sell (without prices)",
    "price": "Prices from your product catalogue",
    "delivery": "Delivery areas and times",
}
# Unlocked only once the facts behind them are structured enough to check.
TOPIC_LOCKS = {
    "price": "Needs a product catalogue with prices, so every price AI quotes can be checked (coming with catalog sync).",
    "delivery": "Coming later: delivery rules aren't structured yet, so AI can't check them.",
    "location": "Tell Akilent where you are first (Build, step 1), so AI answers from your own words.",
    "payment": "Tell Akilent how customers pay first (Build, step 1), so AI answers from your own words.",
}
# Owners see the words; the numbers stay behind the scenes and in the audit record.
CONFIDENCE_CHOICES = ((0.9, "Very careful"), (0.85, "Careful"), (0.75, "Balanced"))
TEAM_QUIET_MINUTES = 10
MAX_IN_A_ROW = 5

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_LINK = re.compile(r"(?:https?://|www\.)\S+", re.I)


@dataclass
class Decision:
    send: bool
    checks: list = field(default_factory=list)   # [{"name", "ok", "detail", "value"?}]

    @property
    def first_failure(self) -> dict | None:
        return next((c for c in self.checks if not c["ok"]), None)

    def as_dict(self) -> dict:
        return {"send": self.send, "checks": self.checks, "decided_at": timezone.now().isoformat()}


def confidence_label(threshold: float) -> str:
    return next((label for value, label in CONFIDENCE_CHOICES if abs(value - threshold) < 1e-6), "Careful")


def locked_topics(account, facts: dict | None = None) -> dict:
    """``{topic: why}`` for topics this business can't use yet, because their facts can't be checked."""
    facts = facts if facts is not None else business_facts.build(account)
    profile = facts.get("business") or {}
    locks = {"delivery": TOPIC_LOCKS["delivery"]}
    if not facts.get("products"):
        locks["price"] = TOPIC_LOCKS["price"]
    if not profile.get("location"):
        locks["location"] = TOPIC_LOCKS["location"]
    if not profile.get("payment_methods"):
        locks["payment"] = TOPIC_LOCKS["payment"]
    return locks


def _plain_numbers(text: str) -> set:
    out = set()
    for raw in _NUMBER.findall(text or ""):
        for part in re.split(r"[.,]", raw.replace(",", "")):
            out.add(part.lstrip("0") or "0")
        out.add(raw.replace(",", "").lstrip("0") or "0")
    return out


def unsupported_facts(reply: str, facts: dict, extra_text: str = "") -> list[str]:
    """What in ``reply`` can't be traced to the business's facts. Empty means every fact checks out.

    ``facts`` is ``apps.ai.facts.build(...)``; ``extra_text`` is look-up results and the business's
    own earlier replies.
    """
    reply = reply or ""
    problems, rest = [], reply
    prices = business_facts.allowed_amounts(facts, extra_text)
    for m in business_facts.MONEY.finditer(reply):
        if business_facts.amount(m.group(1) or m.group(2)) not in prices:
            problems.append(m.group(0).strip())
        rest = rest.replace(m.group(0), " ")
    times = business_facts.allowed_times(facts, extra_text)
    for m in business_facts.TIME.finditer(rest):
        if business_facts.minutes_of(m) not in times:
            problems.append(m.group(0).strip())
    rest = business_facts.TIME.sub(" ", rest)
    written = "\n".join([business_facts.written_text(facts), extra_text or "",
                         " ".join(f"{p.get('name')} {p.get('price')}" for p in facts.get("products", []))])
    known = _plain_numbers(written)
    problems += [n for n in _NUMBER.findall(rest) if not all(p in known for p in _plain_numbers(n))]
    lowered = written.lower()
    problems += [link for link in _LINK.findall(reply) if link.lower().rstrip(".,)") not in lowered]
    return problems


def is_auto_mode(ai_settings) -> bool:
    return bool(ai_settings and ai_settings.enabled and ai_settings.reply_mode == "auto"
                and getattr(settings, "AI_AUTONOMY_ENABLED", True))


def evaluate(*, ai_settings, proposal: dict, conversation, automatic: bool, window_open: bool,
             facts: dict, extra_text: str = "", trigger_message_id=None, now=None) -> Decision:
    """Every check, in order, for sending ``proposal`` (the validated contract) without a person."""
    from apps.accounts import business_hours
    from apps.ai.models import AIProposal
    from apps.conversations.api import last_team_reply_at

    now = now or timezone.now()
    checks = []

    def check(name, ok, detail, **extra):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, **extra})

    topics = set(ai_settings.auto_topics or []) if ai_settings else set()
    intent = proposal.get("intent") or "other"
    confidence = proposal.get("confidence")
    threshold = ai_settings.auto_min_confidence if ai_settings else 1.0
    on = is_auto_mode(ai_settings)
    check("switched_on", on, "Automatic replies are on." if on else "Automatic replies are off.")
    check("automatic", automatic,
          "Answering a customer's message." if automatic else "A teammate asked for this suggestion.")
    check("plain_reply", proposal.get("action") == "reply",
          "A normal reply." if proposal.get("action") == "reply" else "Templates and hand-offs always need a person.")
    locked = locked_topics(conversation.account, facts)
    allowed = intent in topics and intent in TOPICS and intent not in locked
    if allowed:
        topic_detail = f"About {TOPICS[intent].lower()}, which you allowed."
    elif intent in topics and intent in locked:
        topic_detail = f"About {TOPICS[intent].lower()}, which can't be checked yet."
    else:
        topic_detail = f"About {TOPICS.get(intent, 'something you haven’t chosen').lower()}, which isn't on your list."
    check("allowed_topic", allowed, topic_detail, value=intent)
    sure = confidence is not None and confidence >= threshold
    check("confident", sure,
          "AI was sure enough." if sure else f"AI wasn't sure enough for “{confidence_label(threshold)}”.",
          value={"confidence": confidence, "needed": threshold})
    check("window_open", window_open,
          "The 24-hour reply window is open." if window_open else "The 24-hour reply window has closed.")
    check("not_assigned", conversation.assigned_to_id is None,
          "Nobody on the team has taken this conversation." if conversation.assigned_to_id is None
          else "A teammate has taken this conversation.")
    team_at = last_team_reply_at(conversation)
    quiet = team_at is None or now - team_at >= timedelta(minutes=TEAM_QUIET_MINUTES)
    check("team_quiet", quiet,
          "Your team hasn't replied in the last few minutes." if quiet
          else "Your team replied a few minutes ago, so AI stays out of it.")
    if ai_settings and ai_settings.auto_only_when_closed:
        open_now = business_hours.is_open(conversation.account, now)
        check("closed_now", not open_now,
              "You're closed, so AI answers." if not open_now else "You're open, so your team answers.")
    auto = AIProposal.objects.filter(conversation=conversation, auto_sent_at__isnull=False)
    already = bool(trigger_message_id) and auto.filter(trigger_message_id=trigger_message_id).exists()
    check("one_per_message", not already,
          "One reply to this message." if not already else "AI already answered this message.")
    in_a_row = auto.filter(auto_sent_at__gt=team_at).count() if team_at else auto.count()
    check("in_a_row", in_a_row < MAX_IN_A_ROW,
          f"{in_a_row} automatic replies since your team last replied." if in_a_row < MAX_IN_A_ROW
          else f"AI has answered {in_a_row} times in a row. Time for a person.", value=in_a_row)
    missing = unsupported_facts((proposal.get("payload") or {}).get("text", ""), facts, extra_text)
    check("facts", not missing,
          "Every price, time, number and link checks out against your facts." if not missing
          else "Mentions " + ", ".join(missing[:3]) + ", which couldn't be checked against your facts.",
          value=missing[:10])
    return Decision(send=all(c["ok"] for c in checks), checks=checks)


# What the autopilot report calls each reason a reply was held back.
HELD_REASONS = {
    "confident": "AI wasn't sure",
    "facts": "Facts couldn't be checked",
    "not_assigned": "A teammate had taken over",
    "team_quiet": "A teammate had taken over",
    "allowed_topic": "Not a topic you allowed",
    "window_open": "The 24-hour window had closed",
    "plain_reply": "Needed a template or a person",
    "closed_now": "You were open",
    "in_a_row": "Time for a person",
    "one_per_message": "Already answered",
    "automatic": "Asked for by a teammate",
}
