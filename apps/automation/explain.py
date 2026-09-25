"""Owner-facing wording for what automations did, and why they didn't.

The engine reports structured reasons (``workflow_engine.explain_enrollment``, step results); this
module is the only place that turns them into sentences, so copy can change without touching how
automations run. Each explanation is simple first: a headline the owner can act on, an optional
fix button, and the details only when asked for.
"""
from __future__ import annotations

from django.urls import reverse

REPLIED, NOT_REPLIED, WAITING, PROBLEM = "replied", "not_replied", "waiting", "problem"


def _words(items) -> str:
    return ", ".join(f"“{i}”" for i in items[:4])


def describe_verdict(verdict: dict) -> dict:
    """``{"name", "tone", "headline", "detail", "fix": {"label", "url"} | None}`` for one automation."""
    wf, code, d = verdict["workflow"], verdict["code"], verdict["details"]
    edit = reverse("automation:list")
    name = wf.name
    fix = None
    if code == "ran":
        error = d.get("error")
        run = d["run"]
        if error:
            headline, detail = "Started, but the reply didn't go out.", humanise_step_error(error)
            tone = PROBLEM
        elif run.status in ("waiting", "active"):
            headline, detail, tone = "Started. It's waiting for the next step.", "", WAITING
        else:
            headline, detail, tone = "Replied.", "", REPLIED
    elif code == "not_on":
        paused = d.get("status") == "archived"
        headline = "Didn't reply. This automation is " + ("turned off." if paused else "not turned on yet.")
        detail, tone = "", NOT_REPLIED
        fix = {"label": "Turn it on", "url": edit}
    elif code == "not_new_customer":
        headline = "Didn't reply. This one only welcomes brand-new customers, and this customer has messaged before."
        detail, tone = "", NOT_REPLIED
    elif code == "keyword_mismatch":
        headline = (
            f"Didn't reply. It answers messages with {_words(d.get('expected') or [])}, "
            f"but the customer said “{(d.get('received') or '').strip()[:80]}”."
        )
        detail, tone = "You can change the words it listens for when you set it up.", NOT_REPLIED
    elif code == "reply_mismatch":
        headline = "Didn't reply. It only follows one particular button the customer didn't tap."
        detail, tone = "", NOT_REPLIED
    elif code == "cooldown":
        headline = (
            f"Didn't reply. It already answered this customer in the last {d.get('minutes')} minutes, "
            "so it doesn't repeat itself."
        )
        detail, tone = "", NOT_REPLIED
    elif code == "already_running":
        headline = "Didn't start again. It is still working through an earlier message from this customer."
        detail, tone = "", WAITING
    else:  # no_run
        headline = "Didn't start, although its words and settings match this message."
        detail = (
            "Nothing about this automation stopped it. Check the notes above for a switch that is off "
            "for the whole site or your plan, or a condition of its own that this customer didn't meet."
        )
        tone = NOT_REPLIED
    return {"name": name, "tone": tone, "headline": headline, "detail": detail, "fix": fix}


# Substrings of engine and send errors, in the order they are checked.
_ERRORS = (
    ("no value for", "This customer has no value for one of the blanks in your message, and no fallback was set."),
    ("24-hour", "The 24-hour window for replying freely had already closed. Only an approved message can be sent now."),
    ("24h", "The 24-hour window for replying freely had already closed. Only an approved message can be sent now."),
    ("window", "The 24-hour window for replying freely had already closed. Only an approved message can be sent now."),
    ("opted out", "This customer opted out of messages, so nothing is sent to them."),
    ("opt-out", "This customer opted out of messages, so nothing is sent to them."),
    ("not approved", "The message hasn't been approved by WhatsApp yet, so it can't be sent."),
    ("not found", "The message this automation uses no longer exists."),
    ("isn't on your team", "The teammate it should notify is no longer on your team."),
    ("nobody on your team", "There is nobody on your team to hand this to."),
    ("no phone", "This customer has no phone number to send to."),
    ("no open lead", "This customer isn't tracked as interested yet."),
    ("no conversation", "There was no conversation to reply in."),
)


def humanise_step_error(error: str) -> str:
    lowered = (error or "").lower()
    for needle, sentence in _ERRORS:
        if needle in lowered:
            return sentence
    return "Something went wrong sending this. Try again, or contact support if it keeps happening."


def describe_step(step_type: str, status: str, result: dict, step: dict | None = None) -> dict:
    """One executed step as ``{"ok": bool, "text": str}``, for the "what happened" timeline."""
    step, result = step or {}, result or {}
    if status == "error":
        return {"ok": False, "text": humanise_step_error(result.get("error", ""))}
    snippet = lambda text: (text or "").strip().replace("\n", " ")[:80]  # noqa: E731
    texts = {
        "reply_text": lambda: f"Replied “{snippet(step.get('text'))}”",
        "send_whatsapp": lambda: "Sent your WhatsApp message",
        "send_email": lambda: "Sent an email",
        "send_buttons": lambda: "Sent a menu with buttons",
        "send_list": lambda: "Sent a menu list",
        "wait_for_reply": lambda: (
            f"Customer chose “{(result.get('reply') or {}).get('title') or result.get('went_to', '')}”"
            if result.get("reply") else "Waited for the customer's answer"),
        "add_tag": lambda: f"Tagged them “{step.get('tag', '')}”",
        "remove_tag": lambda: f"Removed the tag “{step.get('tag', '')}”",
        "create_lead": lambda: "Marked as interested",
        "update_lead_status": lambda: f"Marked as {step.get('status', 'updated')}",
        "assign_conversation": lambda: (
            "Assigned the conversation to a teammate" if result.get("changed", True)
            else "Left the conversation with its current owner"),
        "notify_team": lambda: f"Told your team ({result.get('notified', 0)} notified)",
        "wait": lambda: "Waited",
        "branch": lambda: "Checked a condition",
        "set_attribute": lambda: "Updated the customer's details",
        "webhook": lambda: "Notified another system",
        "stop": lambda: "Finished",
    }
    return {"ok": True, "text": texts.get(step_type, lambda: "Done")()}


def _duration(seconds: int) -> str:
    if seconds % 86400 == 0 and seconds >= 86400:
        n = seconds // 86400
        return f"{n} day{'s' if n != 1 else ''}"
    if seconds % 3600 == 0 and seconds >= 3600:
        n = seconds // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    n = max(seconds // 60, 1)
    return f"{n} minute{'s' if n != 1 else ''}"


def _when(trigger: dict) -> str:
    from apps.automation.labels import trigger_label

    kind = trigger.get("type", "")
    match = trigger.get("match") or {}
    words = _words(match.get("any") or [])
    if kind == "conversation.message_received":
        how = {"starts_with": "starts with", "exact": "is exactly"}.get(match.get("mode"), "mentions")
        return f"a customer's message {how} {words}" if match else "a customer messages you"
    if kind == "contact.created":
        return "someone messages you for the first time"
    return trigger_label(kind, trigger.get("name", "")).lower().removeprefix("when ").strip() or "something happens"


def _step_sentence(step: dict) -> str | None:
    t = step.get("type")
    snippet = (step.get("text") or "").strip().replace("\n", " ")[:80]
    if t == "reply_text":
        return f"sends this reply: “{snippet}”"
    if t == "send_whatsapp":
        return "sends your approved WhatsApp message"
    if t == "send_email":
        return "sends an email"
    if t == "send_buttons":
        titles = ", ".join(b.get("title", "") for b in step.get("buttons") or [])
        return f"asks “{snippet}” with buttons ({titles})"
    if t == "send_list":
        return f"asks “{snippet}” with a list to choose from"
    if t == "wait_for_reply":
        return "waits for their tap or answer, then replies to what they chose"
    if t == "wait":
        return f"waits {_duration(int(step.get('seconds') or 0))}"
    if t == "add_tag":
        return f"tags them “{step.get('tag', '')}”"
    if t == "remove_tag":
        return f"removes the tag “{step.get('tag', '')}”"
    if t == "create_lead":
        return "marks them as interested"
    if t == "update_lead_status":
        return f"marks them as {step.get('status', 'updated')}"
    if t == "assign_conversation":
        return "gives the conversation to " + (step["to"] if step.get("to") else "your least busy teammate")
    if t == "notify_team":
        to = step.get("to") or "owners"
        who = {"owners": "your owners and admins", "assignee": "the teammate looking after them"}.get(to, to)
        return f"emails {who}"
    if t == "branch":
        if step.get("field") == "within_business_hours":
            return "checks whether you're open"
        return "checks a condition and picks what happens next"
    if t == "set_attribute":
        return "saves a detail about the customer"
    if t == "webhook":
        return "notifies another system"
    return None  # stop / exit: nothing worth saying


def explain_definition(definition: dict) -> dict:
    """What an automation will do, in owner words, straight from its stored definition.

    ``{"when": str, "does": [str], "wont": [str]}``: the inverse of "why didn't it reply?".
    Nothing is written by hand per automation, so it cannot drift from what actually runs.
    """
    definition = definition or {}
    trigger = definition.get("trigger") or {}
    steps = definition.get("steps") or []
    does = [s for s in (_step_sentence(step) for step in steps) if s]
    types = {step.get("type") for step in steps}
    wont = ["It's paused or turned off."]
    if trigger.get("match"):
        wont.append("The customer's message doesn't match the words it listens for.")
    if trigger.get("cooldown_minutes") or trigger.get("match"):
        minutes = trigger.get("cooldown_minutes", 60)
        wont.append(f"It already ran for this customer in the last {_duration(int(minutes) * 60)}.")
    if types & {"reply_text", "send_buttons", "send_list"}:
        wont.append("The 24-hour window for replying to this customer has closed.")
    if types & {"reply_text", "send_buttons", "send_list", "send_whatsapp"}:
        wont.append("The customer has opted out of messages.")
    if trigger.get("type") == "contact.created":
        wont.append("The customer has messaged you before.")
    return {"when": _when(trigger), "does": does, "wont": wont}
