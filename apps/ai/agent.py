"""``run(conversation)``: context in, a checked proposal out. It executes nothing.

The only place that talks to a model while drafting. Callers (the task, a management command) don't
know which provider or model is behind it, so switching Ollama for Anthropic or OpenAI is a
configuration change.

The model may ask for up to ``MAX_LOOKUPS`` read-only look-ups (``apps.ai.tools``) before it
proposes. Each round trip is one more model call, so the limit also caps the cost of one draft.
"""
from __future__ import annotations

import time

from apps.ai import facts as business_facts
from apps.ai import memory as ai_memory
from apps.ai import prompts, proposals, router, tools
from apps.ai.providers import backend_name, get_ai_provider
from apps.ai.types import ChatMessage

# Reasoning models spend part of this on thinking before they answer, so leave headroom.
MAX_TOKENS = 1500
TEMPERATURE = 0.2
MAX_LOOKUPS = 3


def _can_track(account, customer: dict) -> bool:
    from apps.billing import api as billing_api

    return billing_api.usable(account, "sales") and not customer.get("interested")


def run(conversation, *, business_notes: str = "", provider=None) -> dict:
    """``{"proposal", "model", "provider", "latency_ms", "tools_used", "route", "window_open", "sources"}``.

    ``facts`` is the business's structured facts (``apps.ai.facts``) and ``sources`` the other text
    a reply may take facts from (look-up results and the business's own recent replies); together
    they're what ``apps.ai.autonomy`` checks an automatic reply against.

    Raises ``AIProviderError`` if the model can't be reached, ``ProposalError`` if its answer
    isn't usable. Either way nothing has been sent or changed.
    """
    from apps.conversations.api import assistant_context

    account = conversation.account
    ctx = assistant_context(conversation, recent=prompts.RECENT_MESSAGES)
    mem = ai_memory.memory_for(conversation)
    allowed = tools.available(account)
    facts = business_facts.build(account, business_notes=business_notes)
    system, messages = prompts.build(
        business_name=ctx["business_name"], business_notes=business_notes, hours_text=ctx["hours_text"],
        templates=ctx["templates"], customer=ctx["customer"], thread=ctx["thread"],
        window_open=ctx["window_open"], business_tags=ctx["business_tags"],
        memory={"summary": mem.summary, "facts": mem.facts} if mem else None,
        tools_text=tools.describe(allowed), structured_facts=facts,
    )
    route, _why = router.choose(ctx, has_memory=mem is not None)
    provider = provider or get_ai_provider(account, tier=route)
    # Beyond the structured facts: what the business itself has said (its earlier replies) and what
    # look-ups return. Never the customer's words, or "Is it K5,000?" -> "Yes, K5,000" would pass.
    evidence = [ctx["hours_text"], ctx["business_name"]] + [
        m.get("body") or "" for m in ctx["thread"] if m.get("direction") == "outbound"]
    check = {
        "templates": {t["name"]: t["blanks"] for t in ctx["templates"]}, "window_open": ctx["window_open"],
        "tags": ctx["business_tags"], "can_track": _can_track(account, ctx["customer"]),
    }

    started, used, model = time.monotonic(), [], ""
    for round_no in range(MAX_LOOKUPS + 1):
        result = provider.chat(messages, system=system, max_tokens=MAX_TOKENS, temperature=TEMPERATURE)
        model = result.model or model
        data = proposals.extract_json(result.text)
        wanted = proposals.tool_request(data)
        if wanted is None:
            proposal = proposals.validate(data, **check)
            break
        if round_no == MAX_LOOKUPS:
            raise proposals.ProposalError("The AI kept asking for look-ups instead of proposing a reply.")
        name, args = wanted
        used.append(name)
        found = tools.result_text(name, tools.call(name, args, conversation, allowed))
        evidence.append(found)
        messages = messages + [ChatMessage("assistant", result.text), ChatMessage("user", found)]
    return {
        "proposal": proposal, "model": model, "provider": backend_name(),
        "latency_ms": int((time.monotonic() - started) * 1000), "tools_used": used,
        "route": route, "window_open": ctx["window_open"], "facts": facts, "sources": "\n".join(evidence),
    }
