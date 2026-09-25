"""Read-only look-ups the AI may ask for while drafting, instead of stuffing everything into the prompt.

Each tool is a thin name over an Action Registry action, run through ``run_action`` with the
business as the caller, so the registry's tenancy and module checks apply exactly as they do for
workflows. Tools only read. Nothing here sends, creates or changes anything.

Provider-neutral protocol: the model answers ``{"tool": "<name>", "args": {...}}``, Akilent runs it
and hands back the result, then the model answers with its proposal. Any model that can write JSON
can use it, so switching to Anthropic or OpenAI later changes nothing here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1500


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: str                                   # how to call it, shown to the model
    action: str                                 # the Action Registry action behind it
    kwargs: Callable                            # (conversation, args) -> run_action kwargs
    module: str | None = None                   # hidden when the business's module is off


TOOLS = (
    # Availability first: the model can't know what time it is where the business is.
    Tool("check_opening_hours", "Whether the business is open right now and, if not, when it next opens.",
         "{}", "lookup_business_hours", lambda c, a: {"account": c.account}),
    Tool("search_products", "Look up products and their prices in the business's catalogue.",
         '{"query": "<words the customer used>"}', "lookup_products",
         lambda c, a: {"account": c.account, "query": str(a.get("query") or "")[:100]}, module="commerce"),
    Tool("get_customer", "This customer's open follow-up, interest status and last few orders.",
         "{}", "lookup_customer", lambda c, a: {"conversation": c}),
)
_BY_NAME = {t.name: t for t in TOOLS}


def available(account) -> list[Tool]:
    from apps.billing import api as billing_api

    out = []
    for tool in TOOLS:
        if tool.module:
            try:
                if not billing_api.module_enabled(account, tool.module):
                    continue
            except KeyError:
                continue
        out.append(tool)
    return out


def describe(tools: list[Tool]) -> str:
    """The prompt section that tells the model which look-ups exist."""
    if not tools:
        return ""
    lines = ["## Look-ups you can ask for",
             'Before proposing, you may ask for ONE look-up by answering only {"tool": "<name>", '
             '"args": {...}}. You will get the result, then answer. Use a look-up instead of guessing.']
    lines += [f"- {t.name} {t.args}: {t.description}" for t in tools]
    return "\n".join(lines)


def call(name: str, args, conversation, allowed: list[Tool]) -> dict:
    """Run one look-up for this conversation's business. Never raises: errors come back as data."""
    from apps.core.actions import ActionError, run_action

    tool = _BY_NAME.get(name)
    if tool is None or tool not in allowed:
        return {"error": f"There is no look-up called {name!r}."}
    try:
        return run_action(tool.action, {"account": conversation.account},
                          **tool.kwargs(conversation, args if isinstance(args, dict) else {}))
    except ActionError as exc:
        return {"error": str(exc)}
    except Exception:  # noqa: BLE001 - a broken look-up must not sink the proposal
        logger.exception("AI look-up %s failed for conversation=%s", name, conversation.pk)
        return {"error": "That look-up failed."}


def result_text(name: str, result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "…"
    return f"Result of {name}: {text}\nNow either ask for another look-up or answer with the proposal JSON."
