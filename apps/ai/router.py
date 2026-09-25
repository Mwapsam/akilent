"""Which model tier answers a message: ``fast`` for short simple ones, ``standard`` for the rest.

Plain rules, no model call to decide, so routing costs nothing and is easy to explain. Only matters
when ``AI_MODEL_FAST`` is set; otherwise both tiers are the same model. When in doubt: standard.
"""
from __future__ import annotations

SHORT_MESSAGE = 160      # characters in the customer's latest message
SHORT_THREAD = 4         # messages in the recent window


def choose(ctx: dict, *, has_memory: bool) -> tuple[str, str]:
    """``(tier, reason)`` for drafting a reply to this conversation."""
    thread = ctx.get("thread") or []
    last_inbound = next((m.get("body") or "" for m in reversed(thread) if m.get("direction") == "inbound"), "")
    if not ctx.get("window_open", True):
        return "standard", "reply window closed: a template has to be chosen"
    if has_memory:
        return "standard", "long conversation"
    if len(thread) > SHORT_THREAD:
        return "standard", "several messages to take into account"
    if len(last_inbound) > SHORT_MESSAGE or last_inbound.count("?") > 1:
        return "standard", "long or multi-part question"
    return "fast", "short, simple message"
