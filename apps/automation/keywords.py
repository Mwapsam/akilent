"""Deterministic keyword matching for message-triggered workflows.

``match = {"mode": "contains", "any": ["price", "cost"]}`` on a workflow trigger means "only
start this workflow when the customer's message matches". No AI, no regex: a business owner
can read and predict every rule.

Matching is case-insensitive, ignores punctuation and repeated spaces, and works on whole
words, so the keyword "hi" matches "Hi!" and "hi there" but not "this". A keyword may be
several words ("opening hours"); it must appear as that whole phrase. Plurals and typos are
not guessed: list "price" and "prices" if both should match.
"""
from __future__ import annotations

import re

MODES = ("contains", "starts_with", "exact")
MAX_KEYWORDS = 30
MAX_KEYWORD_LENGTH = 60

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    return _SPACES.sub(" ", _NON_WORD.sub(" ", (text or "").casefold())).strip()


def matches(match: dict | None, body: str) -> bool:
    """Whether ``body`` satisfies the trigger's ``match`` rule (no rule means it does)."""
    if not match:
        return True
    body = normalize(body)
    if not body:
        return False
    mode = match.get("mode", "contains")
    padded = f" {body} "
    for keyword in match.get("any") or []:
        needle = normalize(keyword)
        if not needle:
            continue
        if mode == "exact" and body == needle:
            return True
        if mode == "starts_with" and (body + " ").startswith(needle + " "):
            return True
        if mode == "contains" and f" {needle} " in padded:
            return True
    return False


def validate(match) -> list[str]:
    """Human-readable problems with a ``match`` rule (empty list = valid)."""
    if not isinstance(match, dict):
        return ["match must be an object like {\"mode\": \"contains\", \"any\": [\"price\"]}"]
    problems = []
    if match.get("mode", "contains") not in MODES:
        problems.append(f"match.mode must be one of {', '.join(MODES)}")
    words = match.get("any")
    if not isinstance(words, list) or not words:
        problems.append("match.any must list at least one keyword")
    else:
        if len(words) > MAX_KEYWORDS:
            problems.append(f"match.any can hold at most {MAX_KEYWORDS} keywords")
        for word in words:
            if not isinstance(word, str) or not normalize(word):
                problems.append("every keyword must be some text")
                break
            if len(word) > MAX_KEYWORD_LENGTH:
                problems.append(f"a keyword can be at most {MAX_KEYWORD_LENGTH} characters")
                break
    return problems
