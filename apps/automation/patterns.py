"""Replies the team keeps sending, found in real conversations: "You've answered this 9 times".

Deterministic, no AI: a person's replies are grouped by how similar their words are; a group of at
least ``MIN_REPLIES`` replies in different conversations is a pattern. The customer's side gives
the trigger: words that appear in most of the questions that got this reply. The count shown to
the owner is re-counted from the real messages with the same keyword matching the automation will
use, so "9 times" is a fact, not an estimate.

Recomputed from scratch by a daily task (``tasks.find_repeated_replies``); an owner who dismisses
one doesn't see it again for ``DISMISS_DAYS``.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import timedelta
from difflib import SequenceMatcher

from django.utils import timezone

from apps.automation import keywords as kw

LOOKBACK_DAYS = 30
MIN_REPLIES = 3
SIMILARITY = 0.6
MIN_REPLY_WORDS = 4
KEYWORD_SHARE = 0.4      # a question word must appear in at least 40% of the questions
MAX_KEYWORDS = 5
DISMISS_DAYS = 30
MAX_CLUSTER_SCAN = 1500

STOP_WORDS = set("""
a an the and or but if then so to of in on at by for with from as is are was were be been am do does did
i you he she it we they me my your our their this that these those there here what which who whom how
when where why can could would should will shall may might must have has had not no yes please thanks
thank hello hi hey ok okay good morning afternoon evening sir madam boss dear just also any some about
want need like get got know tell send much many one more too very really its im dont cant
""".split())


def _words(text: str) -> list[str]:
    return [w for w in kw.normalize(text).split() if len(w) > 2 and w not in STOP_WORDS and not w.isdigit()]


def _cluster(pairs: list[dict]) -> list[list[dict]]:
    """Greedy grouping of similar replies. Each group's first item is its representative."""
    groups: list[list[dict]] = []
    for pair in pairs[:MAX_CLUSTER_SCAN]:
        text = kw.normalize(pair["reply"])
        if len(text.split()) < MIN_REPLY_WORDS:
            continue  # "ok", "thanks, coming" aren't answers worth automating
        pair["_norm"] = text
        for group in groups:
            if SequenceMatcher(None, group[0]["_norm"], text).ratio() >= SIMILARITY:
                group.append(pair)
                break
        else:
            groups.append([pair])
    return groups


def _typical(group: list[dict]) -> dict:
    """The reply most like all the others (the group's medoid)."""
    if len(group) <= 2:
        return group[0]
    return max(group, key=lambda p: sum(SequenceMatcher(None, p["_norm"], q["_norm"]).ratio() for q in group))


def _question_keywords(group: list[dict]) -> list[str]:
    counts = Counter()
    for pair in group:
        counts.update(set(_words(pair["question"])))
    need = max(2, int(len(group) * KEYWORD_SHARE + 0.999))
    return [word for word, n in counts.most_common(MAX_KEYWORDS * 2) if n >= need][:MAX_KEYWORDS]


def find(account, *, now=None) -> list[dict]:
    """The patterns in this business's recent conversations (not saved). Newest data only."""
    from apps.automation import api as automation_api
    from apps.conversations import api as conversations_api

    now = now or timezone.now()
    pairs = conversations_api.person_reply_pairs(account, since=now - timedelta(days=LOOKBACK_DAYS))
    live = automation_api.published_trigger_matches(account)
    found = []
    for group in _cluster(pairs):
        if len({p["conversation_id"] for p in group}) < MIN_REPLIES:
            continue
        words = _question_keywords(group)
        if not words:
            continue
        match = {"mode": "contains", "any": words}
        asked = [p for p in group if kw.matches(match, p["question"])]
        if len({p["conversation_id"] for p in asked}) < MIN_REPLIES:
            continue
        typical = _typical(group)
        covered = next((w["name"] for w in live if any(kw.matches(w["match"], p["question"]) for p in asked)), "")
        found.append({
            "key": hashlib.sha1(" ".join(sorted(set(_words(typical["reply"])))).encode()).hexdigest()[:16],
            "reply_text": typical["reply"], "count": len(asked),
            "message_ids": [p["reply_id"] for p in asked], "question_keywords": words,
            "topic": words[0], "covered_by": covered,
        })
    return found


def refresh(account, *, now=None) -> int:
    """Recompute and store this business's patterns. Returns how many there are."""
    from apps.automation.models import ReplyPattern

    found = find(account, now=now)
    keys = []
    for item in found:
        ReplyPattern.objects.update_or_create(account=account, key=item["key"], defaults={
            k: item[k] for k in ("reply_text", "count", "message_ids", "question_keywords", "topic", "covered_by")})
        keys.append(item["key"])
    ReplyPattern.objects.filter(account=account).exclude(key__in=keys).delete()
    return len(keys)


def dismissed_keys(account) -> set:
    from apps.automation.models import DismissedSuggestion

    return set(DismissedSuggestion.objects.filter(account=account, until__gt=timezone.now()).values_list("key", flat=True))


def open_patterns(account) -> list:
    """Patterns worth offering: not already automated, not dismissed. Most frequent first."""
    from apps.automation.models import ReplyPattern

    hidden = dismissed_keys(account)
    return [p for p in ReplyPattern.objects.filter(account=account, covered_by="")
            if f"pattern:{p.key}" not in hidden]


def offers_for_messages(account, message_ids) -> dict:
    """``{message_id: {"key", "count"}}`` for the team's replies that belong to an open pattern."""
    wanted = set(message_ids)
    out = {}
    for pattern in open_patterns(account):
        for mid in pattern.message_ids:
            if mid in wanted:
                out[mid] = {"key": pattern.key, "count": pattern.count}
    return out


def dismiss(account, key: str, days: int = DISMISS_DAYS) -> None:
    from apps.automation.models import DismissedSuggestion

    DismissedSuggestion.objects.update_or_create(
        account=account, key=key[:80], defaults={"until": timezone.now() + timedelta(days=days)})
