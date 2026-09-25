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

Optional ``extras`` (at most three) are side suggestions shown as one-click chips next to the main
proposal. A person's click applies one; AI never does:

* ``{"kind": "tag", "tag": "<one of the business's existing tags>"}``
* ``{"kind": "track_interest"}``: track this customer as interested (a lead).
* ``{"kind": "follow_up", "in_days": 1-14, "note": "<what to follow up on>"}``

An extra that fails its checks is dropped; it never sinks the main proposal.
"""
from __future__ import annotations

import json
import re

VERSION = 1
ACTIONS = ("reply", "send_template", "handoff")
MAX_REPLY = 1000
MAX_VALUE = 200
MAX_EXTRAS = 3
EXTRA_KINDS = ("tag", "track_interest", "follow_up")
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


def tool_request(data: dict):
    """``(name, args)`` when the model is asking for a look-up rather than proposing, else None."""
    name = data.get("tool")
    if isinstance(name, str) and name.strip() and "action" not in data:
        return name.strip(), data.get("args") if isinstance(data.get("args"), dict) else {}
    return None


def clean_extras(raw, *, tags=(), can_track: bool = False) -> list[dict]:
    """The usable side suggestions, in order, at most ``MAX_EXTRAS``. Anything doubtful is dropped."""
    known_tags = {t.lower(): t for t in tags}
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or item.get("kind") not in EXTRA_KINDS:
            continue
        kind = item["kind"]
        if kind == "tag":
            tag = known_tags.get(str(item.get("tag") or "").strip().lower())
            if not tag:
                continue  # only tags the business already uses: no tag sprawl
            extra = {"kind": "tag", "tag": tag}
        elif kind == "track_interest":
            if not can_track:
                continue
            extra = {"kind": "track_interest"}
        else:
            try:
                days = int(item.get("in_days"))
            except (TypeError, ValueError):
                continue
            if not 1 <= days <= 14:
                continue
            extra = {"kind": "follow_up", "in_days": days, "note": str(item.get("note") or "").strip()[:200]}
        key = (kind, extra.get("tag"))
        if key in seen:
            continue
        seen.add(key)
        out.append(extra)
        if len(out) == MAX_EXTRAS:
            break
    return out


def validate(data: dict, *, templates: dict, window_open: bool, tags=(), can_track: bool = False) -> dict:
    """A clean, checked proposal, or ``ProposalError``.

    ``templates`` maps each **approved** template name to its list of blanks. ``window_open`` is
    whether a normal message may be sent to this customer right now. ``tags`` are the business's
    existing tags and ``can_track`` whether "track as interested" makes sense for this customer;
    both only shape the optional extras.
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
            value = str(raw.get(blank) or "").strip().strip("{} ").strip()
            is_source = value.startswith(_SOURCES)
            if looks_like_name(blank) and not is_source:
                # A name only ever comes from this customer's own details, whatever the AI wrote
                # (or left empty). Rewriting beats rejecting: the source is the one safe answer.
                value, is_source = "contact.first_name", True
            if not value:
                raise ProposalError(f"The AI left the blank {blank!r} empty.")
            variables[blank] = value if is_source else value[:MAX_VALUE]
        clean = {"template": name, "variables": variables}
    else:  # handoff
        clean = {"note": str(payload.get("note") or reason or "A teammate should reply to this.").strip()[:300]}

    return {"version": VERSION, "action": action, "confidence": confidence, "reason": reason, "payload": clean,
            "extras": clean_extras(data.get("extras"), tags=tags, can_track=can_track)}


def parse(text: str, *, templates: dict, window_open: bool, tags=(), can_track: bool = False) -> dict:
    return validate(extract_json(text), templates=templates, window_open=window_open, tags=tags,
                    can_track=can_track)
