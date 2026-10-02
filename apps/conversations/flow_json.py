"""Builds a static (no-endpoint) WhatsApp Flow JSON from a ConversationForm's
``questions``. Pure and deterministic — no AI, no I/O, same input always
produces the same JSON — so ``ConversationForm.flow_json_hash`` can detect
"needs republish" by comparing a hash, not by diffing questions structurally.

Deliberately a single, static screen for v1: every question becomes a
``TextInput`` (the Flow UI still gives the customer a proper native field per
type — email/phone/number keyboards, validation — even though Akilent's own
``questions`` schema doesn't yet have richer component types like Dropdown or
DatePicker). No backend data-exchange, no encryption: see the Phase C
extension plan for why that's out of scope until a dynamic Flow is justified.

The exact ``on-click-action`` payload shape and the Flow JSON schema
``version`` below are this module's best reconstruction of Meta's documented
building blocks, not verified against a live schema — confirm against Meta's
current Flows API / Flow JSON reference before a first real publish.
"""

from __future__ import annotations

import hashlib
import json

FLOW_JSON_VERSION = (
    "3.0"  # unverified against Meta's current default — see module docstring
)
SCREEN_ID = (
    "FORM"  # the single static screen; referenced by flow_action_payload on send
)

_INPUT_TYPE = {
    "text": "text",
    "email": "email",
    "phone": "phone",
    "number": "number",
}


class FlowJSONError(ValueError):
    """``questions`` can't be turned into a Flow — a caller mistake (e.g. no
    questions), not a customer-facing error."""


def _component_for(question: dict) -> dict:
    return {
        "type": "TextInput",
        "name": question.get("key", ""),
        "label": question.get("label") or question.get("key") or "?",
        "input-type": _INPUT_TYPE.get(question.get("field_type", "text"), "text"),
        "required": True,
    }


def build(questions: list) -> dict:
    """The full Flow JSON for a single-screen static form. Raises
    ``FlowJSONError`` if ``questions`` is empty — mirrors ``forms.FormError``
    for the text-presentation path."""
    if not questions:
        raise FlowJSONError("Cannot build a Flow for a form with no questions.")
    children = [_component_for(q) for q in questions]
    children.append(
        {
            "type": "Footer",
            "label": "Submit",
            "on-click-action": {
                "name": "complete",
                "payload": {
                    q.get("key", ""): f"${{form.{q.get('key', '')}}}" for q in questions
                },
            },
        }
    )
    return {
        "version": FLOW_JSON_VERSION,
        "screens": [
            {
                "id": SCREEN_ID,
                "title": "Form",
                "terminal": True,
                "data": {},
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {"type": "Form", "name": "form", "children": children}
                    ],
                },
            }
        ],
    }


def content_hash(flow_json: dict) -> str:
    """A stable hash of a built Flow JSON, for ``ConversationForm.flow_json_hash``'s
    "has this actually changed" comparison."""
    canonical = json.dumps(flow_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode(), usedforsecurity=False).hexdigest()  # nosec B324
