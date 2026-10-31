"""Try an automation without it touching anyone: what it would say, decide and change.

A pure walk over the definition that follows the engine's own routing (``_route_for_reply``,
the keyword matcher, branch conditions, lookup outcomes), but every step that would send a
message, change a record or call out only *describes* it. Nothing is written, queued or
sent, so it is safe to run on a real customer's record.

Stateless by design: the caller passes the trigger message plus every reply the tester has
typed so far, and the whole conversation is replayed from the start. When the automation
needs another answer the result says so (``waiting``) with the choices to offer.
"""

from __future__ import annotations

from typing import Any

from apps.automation import workflow_engine as engine
from apps.automation.labels import step_label

MAX_STEPS = 60


class _Sample:
    """A pretend customer for testing without picking a real one."""

    pk = None
    first_name = "Ada"
    last_name = "Banda"
    phone = "+260971234567"
    email = ""
    source = "whatsapp"

    def __init__(self, account):
        self.account = account


def simulate(
    account,
    definition: dict,
    *,
    contact=None,
    message: str = "",
    replies: list[str] | None = None,
) -> dict[str, Any]:
    """``{"started", "why_not", "transcript", "waiting", "ended"}`` for one test conversation.

    ``transcript`` entries are ``{"kind", "text", ...}`` where kind is ``customer`` (what the
    tester typed), ``bot`` (a message the customer would get), ``action`` (something it would
    do), ``decision`` (which way it went and why) or ``note``.
    """
    definition = definition or {}
    person = contact or _Sample(account)
    replies = list(replies or [])
    transcript: list[dict] = []
    trigger = definition.get("trigger") or {}
    steps = {s["id"]: s for s in definition.get("steps", []) if s.get("id")}
    ordered = [s for s in definition.get("steps", []) if s.get("id")]

    result = {
        "started": False,
        "why_not": "",
        "transcript": transcript,
        "waiting": None,
        "ended": False,
    }
    if trigger.get("type") in ("conversation.message_received", "whatsapp.received"):
        if message:
            transcript.append({"kind": "customer", "text": message})
        why = _trigger_refusal(trigger, message)
        if why:
            result["why_not"] = why
            return result
    else:
        transcript.append(
            {"kind": "note", "text": "Starts " + _trigger_phrase(trigger) + "."}
        )
    if not ordered:
        result["why_not"] = "There are no steps yet."
        return result
    result["started"] = True

    current = ordered[0]["id"]
    seen: set[str] = set()
    for _ in range(MAX_STEPS):
        if not current:
            result["ended"] = True
            transcript.append({"kind": "note", "text": "The automation ends here."})
            return result
        step = steps.get(current)
        if step is None:
            transcript.append(
                {
                    "kind": "problem",
                    "text": f"It points to a step that doesn't exist ({current}).",
                }
            )
            return result
        if current in seen:
            transcript.append(
                {
                    "kind": "problem",
                    "text": "It loops back to an earlier step, so it would stop.",
                }
            )
            return result
        seen.add(current)
        outcome = _run(account, person, step, replies, transcript)
        if outcome.get("waiting"):
            result["waiting"] = outcome["waiting"]
            return result
        current = outcome.get("next", "")
    transcript.append({"kind": "problem", "text": "Too many steps in a row."})
    return result


def _trigger_refusal(trigger: dict, message: str) -> str:
    from apps.automation import keywords

    match = trigger.get("match")
    if match and not keywords.matches(match, message or ""):
        words = ", ".join(f"“{w}”" for w in match.get("any") or [])
        return f"This message wouldn't start it: it only starts when a message has {words}."
    return ""


def _trigger_phrase(trigger: dict) -> str:
    from apps.automation.labels import trigger_label

    label = trigger_label(trigger.get("type", ""), trigger.get("name", ""))
    return label[0].lower() + label[1:] if label else "when triggered"


def _say(account, person, text: str) -> str:
    from apps.automation.variables import merge_business_facts, merge_first_name

    return merge_business_facts(
        merge_first_name(text or "", person.first_name), account
    )


def _take_reply(replies: list[str], transcript: list[dict]) -> str | None:
    if not replies:
        return None
    text = replies.pop(0)
    transcript.append({"kind": "customer", "text": text})
    return text


def _run(
    account, person, step: dict, replies: list[str], transcript: list[dict]
) -> dict:
    t = step.get("type")
    nxt = step.get("next", "")
    say = lambda text, **extra: transcript.append(  # noqa: E731
        {
            "kind": "bot",
            "text": _say(account, person, text),
            "step_id": step["id"],
            **extra,
        }
    )
    act = lambda text: transcript.append(  # noqa: E731
        {"kind": "action", "text": text, "step_id": step["id"]}
    )
    decide = lambda text: transcript.append(  # noqa: E731
        {"kind": "decision", "text": text, "step_id": step["id"]}
    )

    if t == "reply_text":
        say(step.get("text", ""))
        return {"next": nxt}
    if t == "send_whatsapp":
        say(f"[WhatsApp template “{step.get('template', '')}”]", template=True)
        return {"next": nxt}
    if t == "send_email":
        act(
            f"Would email them: “{step.get('subject') or step.get('template') or 'an email'}”."
        )
        return {"next": nxt}
    if t in ("send_buttons", "send_list"):
        options = [b.get("title", "") for b in step.get("buttons") or []] or [
            r.get("title", "") for r in step.get("rows") or []
        ]
        say(step.get("text", ""), options=[o for o in options if o])
        return {"next": nxt}
    if t == "wait_for_reply":
        reply = _take_reply(replies, transcript)
        if reply is None:
            return {
                "waiting": {
                    "step_id": step["id"],
                    "kind": "choice",
                    "options": list((step.get("routes") or {}).keys()),
                    "timeout_label": _hours(step.get("timeout_seconds")),
                }
            }
        if reply == "__timeout__":
            decide("They didn't answer in time.")
            return {"next": step.get("on_timeout", "")}
        target = engine._route_for_reply(step, {"body": reply})
        if target is None:
            decide(
                "That answer doesn't match a choice, so it's left for you (or AI) to reply to."
            )
            return {"next": ""}
        decide(f"They chose “{reply}”.")
        return {"next": target}
    if t == "ask_question":
        say(step.get("question", ""))
        reply = _take_reply(replies, transcript)
        if reply is None:
            return {
                "waiting": {
                    "step_id": step["id"],
                    "kind": "text",
                    "attribute": step.get("attribute", ""),
                    "timeout_label": _hours(step.get("timeout_seconds")),
                }
            }
        if reply == "__timeout__":
            decide("They didn't answer in time.")
            return {"next": step.get("on_timeout", "")}
        act(
            f"Would save “{reply}” as {step.get('attribute') or 'the answer'} on the "
            + ("lead." if step.get("target") == "lead" else "customer.")
        )
        return {"next": nxt}
    if t == "wait":
        decide(f"Waits {_duration(step.get('seconds'))} (skipped while testing).")
        return {"next": nxt}
    if t == "branch":
        matched, how = _branch(person, step)
        decide(("Yes: " if matched else "No: ") + how)
        return {"next": step.get("on_true" if matched else "on_false", "")}
    if t in ("add_tag", "remove_tag"):
        act(
            ("Would tag them " if t == "add_tag" else "Would remove the tag ")
            + f"“{step.get('tag', '')}”."
        )
        return {"next": nxt}
    if t == "create_lead":
        act("Would track them as an interested customer.")
        return {"next": nxt}
    if t == "update_lead_status":
        act(f"Would mark them {step.get('status', '')}.")
        return {"next": nxt}
    if t == "assign_conversation":
        act(
            f"Would give the conversation to {step.get('to') or 'the least busy teammate'}."
        )
        return {"next": nxt}
    if t == "notify_team":
        act(f"Would email your team: “{_say(account, person, step.get('text', ''))}”")
        return {"next": nxt}
    if t == "ai_classify":
        return _sim_classify(account, step, transcript, decide)
    if t == "ai_extract":
        return _sim_extract(account, step, transcript, decide, act)
    if t == "ai_answer":
        decide(
            "AI answers from your own facts if it can do so safely; otherwise it hands over. "
            "(Shown here as answered; test real answers in Settings → AI.)"
        )
        return {"next": nxt}
    if t == "start_form":
        say(f"[Form “{step.get('form_name') or step.get('form', '')}”]")
        reply = _take_reply(replies, transcript)
        if reply is None:
            return {
                "waiting": {
                    "step_id": step["id"],
                    "kind": "form",
                    "options": ["Fills it in"],
                    "timeout_label": _duration(
                        int(float(step.get("timeout_hours") or 72) * 3600)
                    ),
                }
            }
        if reply == "__timeout__":
            decide("They didn't finish the form in time.")
            return {"next": step.get("on_timeout", "")}
        decide("They finished the form.")
        return {"next": nxt}
    if t == "create_followup":
        hours = float(step.get("due_in_hours") or 24)
        act(
            f"Would set a follow-up for your team, due in {_duration(int(hours * 3600))}: "
            f"“{_say(account, person, step.get('note') or 'Follow up')}”."
        )
        return {"next": nxt}
    if t == "set_attribute":
        act(f"Would set {step.get('key', '')} to {step.get('value', '')!s}.")
        return {"next": nxt}
    if t == "webhook":
        act(f"Would send the details to {step.get('url', '')}.")
        return {"next": nxt}
    if t == "action":
        act(f"Would run “{step.get('action', '')}”.")
        return {"next": nxt}
    if t == "publish_post":
        act("Would publish the chosen post.")
        return {"next": nxt}
    if t == "lookup_product":
        return _lookup(account, step, transcript, decide)
    if t in ("stop", "exit"):
        return {"next": ""}
    act(f"{step_label(t or '')}.")
    return {"next": nxt}


def _lookup(account, step, transcript, decide) -> dict:
    """A real (read-only) catalogue search on the latest thing the tester typed."""
    from apps.commerce.lookup import lookup_product

    query = next(
        (e["text"] for e in reversed(transcript) if e["kind"] == "customer"), ""
    )
    try:
        found = lookup_product(account, query)
    except Exception:
        decide("The product search failed.")
        return {"next": step.get("on_error", "")}
    outcome = found.get("outcome")
    if outcome == "found":
        decide(f"Found “{found['product'].name}”.")
        return {"next": step.get("on_found") or step.get("next", "")}
    if outcome == "ambiguous":
        names = ", ".join(p.name for p in (found.get("candidates") or [])[:3])
        decide(f"Several products match ({names}).")
        return {"next": step.get("on_ambiguous") or step.get("on_not_found", "")}
    if found.get("reason") == "no_price":
        decide("Found it, but it has no price.")
        return {"next": step.get("on_no_price") or step.get("on_not_found", "")}
    decide("No product matches.")
    return {"next": step.get("on_not_found", "")}


def _sim_operation(account, step, text) -> str:
    import hashlib

    digest = hashlib.sha1(
        f"{step.get('id')}:{text}".encode(), usedforsecurity=False
    ).hexdigest()[:16]
    return f"wf-sim:{account.pk}:{digest}"


def _last_customer_text(transcript) -> str:
    return next(
        (e["text"] for e in reversed(transcript) if e["kind"] == "customer"), ""
    )


def _sim_classify(account, step, transcript, decide) -> dict:
    """The real decision, keywords first then AI (it costs one AI call), nothing sent."""
    from apps.ai import api as ai_api
    from apps.automation import ai_steps

    text = _last_customer_text(transcript)
    routes = step.get("routes") or {}
    by_keyword = ai_steps._keyword_label(step, text) if text else None
    if by_keyword:
        decide(f"Sorted as “{by_keyword}” (your words for it).")
        return {"next": routes.get(by_keyword, "")}
    labels = [
        i for i in step.get("labels") or [] if isinstance(i, dict) and i.get("label")
    ]
    verdict: dict[str, Any] = (
        ai_api.classify(
            account, text, labels, operation_id=_sim_operation(account, step, text)
        )
        if text
        else {"label": None, "confidence": 0, "ai": False}
    )
    threshold = ai_steps.CARE.get(
        step.get("care") or "careful", ai_steps.CARE["careful"]
    )
    if verdict["label"] and verdict["confidence"] >= threshold:
        decide(f"AI decision: “{verdict['label']}”.")
        return {"next": routes.get(verdict["label"], "")}
    decide(
        "AI wasn't sure, so it goes your “not sure” way."
        if verdict["ai"]
        else "AI isn't available, so it goes your “not sure” way."
    )
    return {"next": step.get("on_unsure", "")}


def _sim_extract(account, step, transcript, decide, act) -> dict:
    from apps.ai import api as ai_api
    from apps.conversations import forms as conversation_forms

    text = _last_customer_text(transcript)
    fields = [
        f for f in step.get("fields") or [] if isinstance(f, dict) and f.get("key")
    ]
    raw = (
        ai_api.extract(
            account, text, fields, operation_id=_sim_operation(account, step, text)
        )
        if text
        else {}
    )
    missing = []
    for f in fields:
        value = (
            conversation_forms.validate(
                {"field_type": f.get("type") or "text"}, raw.get(f["key"], "")
            )
            if f["key"] in raw
            else None
        )
        if value is None:
            missing.append(f["key"])
        else:
            act(f"AI decision: would save {f['key']} = “{value}”.")
    if missing:
        decide("Missing " + ", ".join(missing) + ", so it goes your “missing” way.")
        return {"next": step.get("on_missing", "")}
    return {"next": step.get("next", "")}


def _branch(person, step: dict) -> tuple[bool, str]:
    from apps.automation.builder import BRANCH_FIELDS

    label = str(
        next(
            (f["label"] for f in BRANCH_FIELDS if f["field"] == step.get("field")),
            step.get("field", "the condition"),
        )
    )
    if step.get("field") == "within_business_hours":
        from apps.accounts import business_hours

        wants_open = step.get("value", True) in engine._TRUTHY
        if step.get("operator", "eq") == "ne":
            wants_open = not wants_open
        matched = business_hours.is_open(person.account) is wants_open
        return matched, f"{label.lower()} (checked against your real hours)."
    if getattr(person, "pk", None):
        try:
            return engine._condition_matches(
                person, step
            ), f"{label.lower()} (from their record)."
        except Exception:
            return (
                False,
                f"couldn't check {label.lower()}; it would go the “otherwise” way.",
            )
    # The pretend customer is brand new: no tags, no orders, nothing known beyond a name.
    return (
        False,
        f"{label.lower()}? A new test customer doesn't (pick a real customer to check).",
    )


def _duration(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds and seconds % 86400 == 0:
        n, unit = seconds // 86400, "day"
    elif seconds and seconds % 3600 == 0:
        n, unit = seconds // 3600, "hour"
    else:
        n, unit = max(1, seconds // 60), "minute"
    return f"{n} {unit}{'s' if n != 1 else ''}"


def _hours(seconds) -> str:
    return _duration(seconds or engine.DEFAULT_REPLY_TIMEOUT_SECONDS)
