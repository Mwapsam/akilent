"""Turns a failed WhatsApp Flow publish into words an owner can act on.

Meta answers a bad publish in one of two ways:

* an HTTP error with a Graph error code (``WhatsAppProviderError.code`` such as ``"190"``
  or ``"100/33"``): access, permission, rate-limit or request problems, none of them about
  the questions themselves; or
* an accepted upload that lists ``validation_errors`` (``code == "FLOW_VALIDATION"``), each
  with a JSON ``pointers[].path`` like ``screens[0].layout.children[0].children[2].label``.
  We walk that path over the Flow JSON we built to find which question it means.

``humanize_flow_error`` never raises; anything it can't place gets a generic sentence. The
raw text is kept separately (``ConversationForm.flow_error_detail``) for operators.
"""

from __future__ import annotations

import re
from typing import Any

GENERIC = "WhatsApp couldn't publish this form. Try again in a few minutes, or contact support if it keeps happening."

_RECONNECT = "Your WhatsApp connection has expired. Reconnect WhatsApp in Settings, then publish again."
_PERMISSION = (
    "Akilent isn't allowed to manage forms on this WhatsApp account. Reconnect WhatsApp "
    "and allow every permission it asks for, then publish again."
)
_BUSY = "WhatsApp is busy right now. Wait a few minutes, then publish again."
_BLOCKED = (
    "WhatsApp has blocked this form. Check the questions follow WhatsApp's commerce "
    "policy, or contact support."
)
_OUR_SIDE = (
    "WhatsApp rejected the request. This is a problem on our side, not with your form. "
    "Try again later, or contact support if it keeps happening."
)
_GONE = "The form no longer exists on WhatsApp. Publish again to create it afresh."

_CODE_MESSAGES = {
    "190": _RECONNECT,
    "102": _RECONNECT,
    "10": _PERMISSION,
    "200": _PERMISSION,
    "294": _PERMISSION,
    "4": _BUSY,
    "17": _BUSY,
    "32": _BUSY,
    "613": _BUSY,
    "80007": _BUSY,
    "130429": _BUSY,
    "139000": _BLOCKED,
    "139004": _BLOCKED,
}

# What a pointer's last property means, per question.
_PROPERTY_MESSAGES = {
    "name": "needs a field name",
    "label": "WhatsApp didn't accept its wording. Keep it short and plain",
    "helper-text": "its helper text is too long",
    "data-source": "WhatsApp didn't accept its choices. Keep each one short, with no repeats",
    "input-type": "WhatsApp didn't accept its answer type",
    "required": "WhatsApp didn't accept its required setting",
}

_TOKEN = re.compile(r"([A-Za-z_][\w-]*)|\[(\d+)\]")


def _walk(flow_json: dict, path: str) -> tuple[Any, str]:
    """Follow ``path`` as far as it goes; return (deepest dict reached, last property)."""
    node: Any = flow_json
    last_dict: Any = flow_json if isinstance(flow_json, dict) else None
    last_prop = ""
    for name, index in _TOKEN.findall(path or ""):
        try:
            if name:
                last_prop = name
                if not isinstance(node, dict) or name not in node:
                    break
                node = node[name]
            else:
                node = node[int(index)]
        except (IndexError, KeyError, TypeError, ValueError):
            break
        if isinstance(node, dict):
            last_dict = node
    return last_dict, last_prop


_NOT_QUESTIONS = {"Form", "Footer", "TextBody"}


def _position(flow_json: dict | None, component: dict) -> int:
    """1-based question number of ``component`` by position (questions are laid out
    in order across screens), or 0. Used when its name is missing or unknown."""
    n = 0
    for screen in (flow_json or {}).get("screens") or []:
        for form in screen.get("layout", {}).get("children") or []:
            for child in form.get("children") or []:
                if isinstance(child, dict) and child.get("type") not in _NOT_QUESTIONS:
                    n += 1
                    if child is component:
                        return n
    return 0


def _where(
    component: Any, prop: str, keys: dict[str, int], flow_json: dict | None = None
) -> str:
    """Owner words for the part of the form a component is."""
    if not isinstance(component, dict):
        return ""
    kind = component.get("type", "")
    if kind == "Footer":
        return "The button text"
    if kind == "TextBody":
        return "The intro text"
    if "layout" in component or prop == "title":
        return "The form title"
    name = component.get("name", "")
    if name in keys:
        return f"Question {keys[name]}"
    number = _position(flow_json, component)
    return f"Question {number}" if number else ""


def _problem_sentence(
    problem: Any, flow_json: dict | None, keys: dict[str, int]
) -> str:
    if not isinstance(problem, dict):
        return str(problem)
    message = str(problem.get("message") or problem.get("error") or "").strip()
    for pointer in problem.get("pointers") or []:
        path = pointer.get("path", "") if isinstance(pointer, dict) else ""
        if not path or flow_json is None:
            continue
        component, prop = _walk(flow_json, path)
        where = _where(component, prop, keys, flow_json)
        if not where:
            continue
        if where.startswith("Question"):
            if isinstance(component, dict) and not component.get("name"):
                return f"{where} needs a field name."
            detail = _PROPERTY_MESSAGES.get(prop)
            if detail:
                return f"{where}: {detail}."
        return (
            f"{where} wasn't accepted by WhatsApp{': ' + message if message else ''}."
        )
    return f"WhatsApp didn't accept the form{': ' + message if message else ''}."


def humanize_flow_error(
    *,
    code: str = "",
    message: str = "",
    problems: list | None = None,
    flow_json: dict | None = None,
    questions: list | None = None,
) -> str:
    """One or a few sentences for the owner. Never raises."""
    try:
        keys = {
            q.get("key", ""): n
            for n, q in enumerate(questions or [], 1)
            if q.get("key")
        }
        if code == "FLOW_VALIDATION" and problems:
            sentences = []
            for problem in problems[:3]:
                sentence = _problem_sentence(problem, flow_json, keys)
                if sentence not in sentences:
                    sentences.append(sentence)
            return " ".join(sentences) or GENERIC
        if code == "FLOW_VALIDATION":
            # No pointers to place: the provider's own sentence quotes Meta's reasons.
            return message or GENERIC
        if code in ("100/33", "404"):
            return _GONE
        base = (code or "").split("/")[0]
        if base in _CODE_MESSAGES:
            return _CODE_MESSAGES[base]
        if base == "100":
            return _OUR_SIDE
    except Exception:  # a translation must never break the publish task
        pass
    return GENERIC
