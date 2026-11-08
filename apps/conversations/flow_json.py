"""Builds a static (no-endpoint) WhatsApp Flow JSON from a ConversationForm's questions.

Pure and deterministic (no AI, no I/O, same input always gives the same JSON), so
``ConversationForm.flow_json_hash`` can detect "needs republish" by comparing a hash.

Targets Flow JSON 7.3, Meta's recommended version: 2.1–4.0 are frozen (can't be published)
since April 2025 and 5.0 since September 2025. No data-exchange endpoint and no encryption:
every screen is static and answers travel forward screen to screen in the navigate payload,
then come back in one ``complete`` payload (an ``nfm_reply``).

Question schema (``ConversationForm.questions`` items)::

    {"key", "label", "field_type", "maps_to",
     "required": bool = True, "helper": str = "", "options": [str], "page_break": bool}

``check`` lists what Meta would reject (label lengths, option counts, empty text) in words
for the owner, before anything is sent to Meta.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

FLOW_JSON_VERSION = "7.3"
FLOW_MESSAGE_VERSION = "3"
SCREEN_ID = "FORM"  # the first screen; referenced by flow_action_payload on send

# field_type -> (component, extra props)
FIELD_TYPES = {
    "text": ("TextInput", {"input-type": "text"}),
    "email": ("TextInput", {"input-type": "email"}),
    "phone": ("TextInput", {"input-type": "phone"}),
    "number": ("TextInput", {"input-type": "number"}),
    "long_text": ("TextArea", {}),
    "choice": ("RadioButtonsGroup", {}),
    "dropdown": ("Dropdown", {}),
    "multi_choice": ("CheckboxGroup", {}),
    "date": ("DatePicker", {}),
    "opt_in": ("OptIn", {}),
}
CHOICE_TYPES = {"choice", "dropdown", "multi_choice"}

# Meta's limits, conservatively: what a label may hold per component.
LABEL_LIMITS = {
    "TextInput": 20,
    "TextArea": 20,
    "Dropdown": 20,
    "RadioButtonsGroup": 30,
    "CheckboxGroup": 30,
    "DatePicker": 40,
    "OptIn": 120,
}
OPTION_TITLE_LIMIT = 30
MAX_OPTIONS = {"choice": 20, "multi_choice": 20, "dropdown": 200}
QUESTIONS_PER_SCREEN = 8
MAX_SCREENS = 10
HELPER_LIMIT = 80
TITLE_LIMIT = 30
FOOTER_LIMIT = 35
FIELD_NAME = re.compile(r"[A-Za-z0-9_]+")  # what Meta accepts as a component name


class FlowJSONError(ValueError):
    """The questions can't be turned into a Flow. The message is for the owner."""


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _option_id(index: int) -> str:
    return f"opt_{index}"


def check(questions: list, *, title: str = "", submit: str = "") -> list[str]:
    """Everything Meta would reject, in owner words (empty: good to publish)."""
    problems = []
    if not questions:
        problems.append("Add at least one question.")
    seen = set()
    for n, q in enumerate(questions, 1):
        ft = q.get("field_type", "text")
        if ft not in FIELD_TYPES:
            problems.append(f"Question {n}: unknown answer type “{ft}”.")
            continue
        component = FIELD_TYPES[ft][0]
        label = _clean(q.get("label"))
        if not label:
            problems.append(f"Question {n} needs a label.")
        elif len(label) > LABEL_LIMITS[component]:
            problems.append(
                f"Question {n}: “{label[:24]}…” is {len(label)} characters; in a WhatsApp "
                f"Flow this kind of question can show at most {LABEL_LIMITS[component]}. "
                "Shorten it, and put the rest in the helper text."
            )
        if len(_clean(q.get("helper"))) > HELPER_LIMIT:
            problems.append(
                f"Question {n}: helper text can be at most {HELPER_LIMIT} characters."
            )
        key = q.get("key") or ""
        if not key or key in seen:
            problems.append(f"Question {n} needs its own unique key.")
        elif not FIELD_NAME.fullmatch(key):
            # Meta rejects other characters in a field name; keys made by the builder
            # already fit, so this catches old or hand-edited forms.
            problems.append(
                f"Question {n}: its field name “{key[:30]}” can only use letters, "
                "numbers and underscores."
            )
        seen.add(key)
        if ft in CHOICE_TYPES:
            options = [_clean(o) for o in q.get("options") or [] if _clean(o)]
            if len(options) < 2:
                problems.append(f"Question {n} needs at least two choices.")
            if len(options) > MAX_OPTIONS[ft]:
                problems.append(
                    f"Question {n} can have at most {MAX_OPTIONS[ft]} choices."
                )
            long = [o for o in options if len(o) > OPTION_TITLE_LIMIT]
            if long:
                problems.append(
                    f"Question {n}: choice “{long[0][:20]}…” is longer than "
                    f"{OPTION_TITLE_LIMIT} characters."
                )
    if len(_screens(questions)) > MAX_SCREENS:
        problems.append(f"A form can have at most {MAX_SCREENS} pages.")
    if len(_clean(title)) > TITLE_LIMIT:
        problems.append(f"The form title can be at most {TITLE_LIMIT} characters.")
    if len(_clean(submit)) > FOOTER_LIMIT:
        problems.append(f"The button text can be at most {FOOTER_LIMIT} characters.")
    return problems


def _screens(questions: list) -> list[list[dict]]:
    """Split into pages: at an explicit page break, and every QUESTIONS_PER_SCREEN."""
    pages: list[list[dict]] = [[]]
    for q in questions:
        if pages[-1] and (
            q.get("page_break") or len(pages[-1]) >= QUESTIONS_PER_SCREEN
        ):
            pages.append([])
        pages[-1].append(q)
    return [p for p in pages if p]


def _component(question: dict) -> dict:
    ft = question.get("field_type", "text")
    kind, extra = FIELD_TYPES.get(ft, FIELD_TYPES["text"])
    comp = {
        "type": kind,
        "name": question.get("key", ""),
        "label": _clean(question.get("label"))
        or _clean(question.get("key"))
        or "Answer",
        "required": bool(question.get("required", True)),
        **extra,
    }
    helper = _clean(question.get("helper"))
    if helper and kind in ("TextInput", "TextArea", "DatePicker"):
        comp["helper-text"] = helper
    if ft in CHOICE_TYPES:
        options = [_clean(o) for o in question.get("options") or [] if _clean(o)]
        comp["data-source"] = [
            {"id": _option_id(i), "title": o} for i, o in enumerate(options)
        ]
    return comp


def _data_type(question: dict) -> dict:
    ft = question.get("field_type", "text")
    if ft == "multi_choice":
        return {"type": "array", "items": {"type": "string"}, "__example__": []}
    if ft == "opt_in":
        return {"type": "boolean", "__example__": False}
    return {"type": "string", "__example__": "example"}


def build(
    questions: list,
    *,
    title: str = "",
    intro: str = "",
    submit: str = "",
) -> dict:
    """The full Flow JSON. Raises ``FlowJSONError`` with the first problem ``check`` finds."""
    problems = check(questions, title=title, submit=submit)
    if problems:
        raise FlowJSONError(problems[0])
    pages = _screens(questions)
    title = _clean(title) or "Your details"
    screens = []
    carried: list[dict] = []  # questions answered on earlier pages
    for index, page in enumerate(pages):
        last = index == len(pages) - 1
        screen_id = SCREEN_ID if index == 0 else f"PAGE_{index + 1}"
        children: list[dict] = []
        if index == 0 and _clean(intro):
            children.append({"type": "TextBody", "text": _clean(intro)})
        children += [_component(q) for q in page]
        forward = {q["key"]: f"${{data.{q['key']}}}" for q in carried}
        forward.update({q["key"]: f"${{form.{q['key']}}}" for q in page})
        if last:
            action = {"name": "complete", "payload": forward}
            label = _clean(submit) or "Submit"
        else:
            action = {
                "name": "navigate",
                "next": {"type": "screen", "name": f"PAGE_{index + 2}"},
                "payload": forward,
            }
            label = "Next"
        children.append({"type": "Footer", "label": label, "on-click-action": action})
        screen: dict[str, Any] = {
            "id": screen_id,
            "title": title,
            "data": {q["key"]: _data_type(q) for q in carried},
            "layout": {
                "type": "SingleColumnLayout",
                "children": [{"type": "Form", "name": "form", "children": children}],
            },
        }
        if last:
            screen["terminal"] = True
            screen["success"] = True
        screens.append(screen)
        carried = carried + page
    return {"version": FLOW_JSON_VERSION, "screens": screens}


def option_title(question: dict, option_id: str) -> str:
    """The choice a Flow answer's option id stands for (``opt_2`` -> "Large")."""
    options = [_clean(o) for o in question.get("options") or [] if _clean(o)]
    if option_id.startswith("opt_"):
        try:
            return options[int(option_id[4:])]
        except (ValueError, IndexError):
            return ""
    return option_id if option_id in options else ""


def content_hash(flow_json: dict) -> str:
    """A stable hash of a built Flow JSON, for the "has this actually changed" check."""
    canonical = json.dumps(flow_json, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode(), usedforsecurity=False).hexdigest()
