"""WhatsApp interactive messages: reply buttons and lists, in both directions.

Inbound, a customer's tap arrives as an ``interactive`` (or template ``button``) message.
``extract_reply`` turns it into ``{"id", "title", "kind"}``: the *title* is what they saw and
tapped, so it is stored as the message text (readable in the inbox, matchable by keyword),
and the *id* is what an automation routes on. Outbound, ``build_buttons`` / ``build_list``
produce Meta's ``interactive`` object after enforcing WhatsApp's limits, so a definition that
Meta would reject fails when it is saved, not when a customer is waiting.

Interactive messages are free-form session messages: like plain text they can only be sent
inside the 24-hour customer-service window.
"""
from __future__ import annotations

from django.utils.text import slugify

MAX_BUTTONS = 3
MAX_LIST_ROWS = 10
MAX_BODY = 1024
MAX_BUTTON_TITLE = 20
MAX_ROW_TITLE = 24
MAX_ROW_DESCRIPTION = 72
MAX_ID = 200


class InteractiveError(ValueError):
    """A message that WhatsApp would refuse, worded for a business owner."""


# --- inbound ------------------------------------------------------------------------------


def extract_reply(message: dict) -> dict | None:
    """The tapped option of an inbound interactive message, or None for anything else."""
    kind = message.get("type")
    if kind == "interactive":
        block = message.get("interactive") or {}
        subtype = block.get("type")
        if subtype in ("button_reply", "list_reply"):
            reply = block.get(subtype) or {}
            return {"id": reply.get("id") or "", "title": reply.get("title") or "", "kind": subtype}
    elif kind == "button":  # a quick-reply button on a template the business sent
        button = message.get("button") or {}
        return {"id": button.get("payload") or "", "title": button.get("text") or "", "kind": "template_button"}
    return None


def reply_for_log(message_log) -> dict | None:
    """Recover the tap behind a logged inbound message from its stored webhook payload."""
    if not message_log.message_id:
        return None
    for entry in (message_log.raw_payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            for message in (change.get("value") or {}).get("messages") or []:
                if message.get("id") == message_log.message_id:
                    return extract_reply(message)
    return None


# --- outbound -----------------------------------------------------------------------------


def _text(value, label: str, limit: int, *, required: bool = True) -> str:
    value = (value or "").strip() if isinstance(value, str) or value is None else None
    if value is None:
        raise InteractiveError(f"{label} must be text.")
    if required and not value:
        raise InteractiveError(f"{label} can't be empty.")
    if len(value) > limit:
        raise InteractiveError(f"{label} can be at most {limit} characters (this is {len(value)}).")
    return value


def _option_id(option: dict, title: str) -> str:
    option_id = str(option.get("id") or "").strip() or slugify(title)[:MAX_ID]
    if not option_id:
        raise InteractiveError(f"Give “{title}” a name with letters or numbers.")
    return _text(option_id, "An option id", MAX_ID)


def build_buttons(text: str, buttons: list) -> dict:
    """Meta's ``interactive`` object for up to three reply buttons."""
    body = _text(text, "The message", MAX_BODY)
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= MAX_BUTTONS:
        raise InteractiveError(f"Use between 1 and {MAX_BUTTONS} buttons.")
    built, seen = [], set()
    for option in buttons:
        option = option if isinstance(option, dict) else {"title": option}
        title = _text(option.get("title"), "A button label", MAX_BUTTON_TITLE)
        option_id = _option_id(option, title)
        if option_id.casefold() in seen:
            raise InteractiveError(f"Two buttons share the name “{option_id}”. Make the labels different.")
        seen.add(option_id.casefold())
        built.append({"type": "reply", "reply": {"id": option_id, "title": title}})
    return {"type": "button", "body": {"text": body}, "action": {"buttons": built}}


def build_list(text: str, button_label: str, rows: list) -> dict:
    """Meta's ``interactive`` object for a list of up to ten choices behind one button."""
    body = _text(text, "The message", MAX_BODY)
    label = _text(button_label, "The list button label", MAX_BUTTON_TITLE)
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_LIST_ROWS:
        raise InteractiveError(f"Use between 1 and {MAX_LIST_ROWS} choices.")
    built, seen = [], set()
    for row in rows:
        row = row if isinstance(row, dict) else {"title": row}
        title = _text(row.get("title"), "A choice", MAX_ROW_TITLE)
        row_id = _option_id(row, title)
        if row_id.casefold() in seen:
            raise InteractiveError(f"Two choices share the name “{row_id}”. Make the titles different.")
        seen.add(row_id.casefold())
        entry = {"id": row_id, "title": title}
        description = _text(row.get("description"), "A choice's description", MAX_ROW_DESCRIPTION, required=False)
        if description:
            entry["description"] = description
        built.append(entry)
    return {
        "type": "list", "body": {"text": body},
        "action": {"button": label, "sections": [{"title": "Options", "rows": built}]},
    }


def option_titles(interactive: dict) -> list[str]:
    """The visible option labels of a built ``interactive`` object (for the inbox transcript)."""
    action = interactive.get("action") or {}
    if interactive.get("type") == "button":
        return [b["reply"]["title"] for b in action.get("buttons", [])]
    return [r["title"] for s in action.get("sections", []) for r in s.get("rows", [])]
