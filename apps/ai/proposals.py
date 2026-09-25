"""The proposal contract: what AI may propose, and the checks it must pass before a person sees it.

A proposal is data, never an action. The envelope is versioned so a new provider or a new kind of
proposal can't silently break parsing::

    {"version": 1, "action": "reply", "confidence": 0.94,
     "reason": "Customer asked a pricing question.", "payload": {"text": "..."}}

Actions in version 1:

* ``reply``: ``payload.text``, a normal message. Only allowed inside WhatsApp's 24-hour window.
* ``send_template``: ``payload.template`` (an approved template's name) and ``payload.variables``,
  one value per blank. A value is a per-customer source (``contact.first_name``,
  ``account.company_name``...) or short text. A name-like blank must use a source, so AI can
  never write one customer's name into another's message.
* ``handoff``: ``payload.note`` for the team; AI thinks a person should take this.
"""
from __future__ import annotations

import json
import re

VERSION = 1
ACTIONS = ("reply", "send_template", "handoff")
MAX_REPLY = 1000
MAX_VALUE = 200
_SOURCES = ("contact.", "account.", "context.")


class ProposalError(ValueError):
    """The model's answer can't be used. The message is safe to show to staff."""


def extract_json(text: str) -> dict:
    """The JSON object in a model's answer, tolerating code fences and stray words around it."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ProposalError("The AI didn't answer in the expected format.")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise ProposalError("The AI didn't answer in the expected format.") from exc
    if not isinstance(data, dict):
        raise ProposalError("The AI didn't answer in the expected format.")
    return data


def validate(data: dict, *, templates: dict, window_open: bool) -> dict:
    """A clean, checked proposal, or ``ProposalError``.

    ``templates`` maps each **approved** template name to its list of blanks. ``window_open`` is
    whether a normal message may be sent to this customer right now.
    """
    from apps.automation.variables import looks_like_name

    if data.get("version", VERSION) != VERSION:
        raise ProposalError(f"Unsupported proposal version {data.get('version')!r}.")
    action = data.get("action")
    if action not in ACTIONS:
        raise ProposalError(f"Unknown proposal action {action!r}.")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    confidence = data.get("confidence")
    try:
        confidence = None if confidence is None else max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None
    reason = str(data.get("reason") or "").strip()[:300]

    if action == "reply":
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProposalError("The AI proposed an empty reply.")
        if not window_open:
            raise ProposalError(
                "The AI proposed a normal reply, but the 24-hour window has closed. Only an approved "
                "template can be sent now.")
        clean = {"text": text[:MAX_REPLY]}
    elif action == "send_template":
        name = str(payload.get("template") or "").strip()
        if name not in templates:
            raise ProposalError(f"The AI proposed a template that isn't approved ({name!r}).")
        raw = payload.get("variables") if isinstance(payload.get("variables"), dict) else {}
        variables = {}
        for blank in templates[name]:
            value = str(raw.get(blank) or "").strip()
            if not value:
                raise ProposalError(f"The AI left the blank {blank!r} empty.")
            is_source = value.startswith(_SOURCES)
            if looks_like_name(blank) and not is_source:
                raise ProposalError(
                    f"The AI typed a name into {blank!r}; names must come from the customer's own details.")
            variables[blank] = value if is_source else value[:MAX_VALUE]
        clean = {"template": name, "variables": variables}
    else:  # handoff
        clean = {"note": str(payload.get("note") or reason or "A teammate should reply to this.").strip()[:300]}

    return {"version": VERSION, "action": action, "confidence": confidence, "reason": reason, "payload": clean}


def parse(text: str, *, templates: dict, window_open: bool) -> dict:
    return validate(extract_json(text), templates=templates, window_open=window_open)
