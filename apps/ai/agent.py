"""``run(conversation)``: context in, a checked proposal out. It executes nothing.

The only place that talks to a model. Callers (the task, a management command, a later tool loop)
don't know which provider or model is behind it, so switching Ollama for Anthropic or OpenAI is a
configuration change.
"""
from __future__ import annotations

import time

from apps.ai import prompts, proposals
from apps.ai.providers import backend_name, get_ai_provider

# Reasoning models spend part of this on thinking before they answer, so leave headroom.
MAX_TOKENS = 1500
TEMPERATURE = 0.2


def run(conversation, *, business_notes: str = "", provider=None) -> dict:
    """``{"proposal": {...}, "model", "provider", "latency_ms"}``.

    Raises ``AIProviderError`` if the model can't be reached, ``ProposalError`` if its answer
    isn't usable. Either way nothing has been sent or changed.
    """
    from apps.conversations.api import assistant_context

    ctx = assistant_context(conversation, recent=prompts.RECENT_MESSAGES)
    system, messages = prompts.build(
        business_name=ctx["business_name"], business_notes=business_notes, hours_text=ctx["hours_text"],
        templates=ctx["templates"], customer=ctx["customer"], thread=ctx["thread"],
        window_open=ctx["window_open"],
    )
    provider = provider or get_ai_provider(conversation.account)
    started = time.monotonic()
    result = provider.chat(messages, system=system, max_tokens=MAX_TOKENS, temperature=TEMPERATURE)
    latency_ms = int((time.monotonic() - started) * 1000)
    proposal = proposals.parse(
        result.text, templates={t["name"]: t["blanks"] for t in ctx["templates"]},
        window_open=ctx["window_open"],
    )
    return {"proposal": proposal, "model": result.model, "provider": backend_name(), "latency_ms": latency_ms}
