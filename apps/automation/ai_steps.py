"""Automation steps that use AI, each with a way to go when AI can't help.

* ``ai_classify``: sort the customer's message into one of the owner's labels. Keywords the owner
  gave each label are checked first (no AI call, fully predictable). Then AI may only answer one of
  the labels; below the step's carefulness, or with AI off, failing or over its limit, the run goes
  the ``on_unsure`` way.
* ``ai_extract``: copy details (name, email, date, …) out of the message. Every value is checked
  with the same validators as forms before it's saved; anything missing or invalid goes the
  ``on_missing`` way.
* ``ai_answer``: reply using only the business's own facts, under the same checks as automatic
  replies. If any check fails, the ``on_handoff`` way (e.g. a holding reply and a follow-up).

Every decision is stored on the step run (``WorkflowStepRun.result``), shown in run history, and
can be corrected there; corrections become examples for the next decisions.
"""

from __future__ import annotations

import hashlib

AI_STEP_TYPES = frozenset({"ai_classify", "ai_extract", "ai_answer"})
MAX_LABELS = 8
# The behaviour profiles owners choose, never shown as numbers.
CARE = {"very_careful": 0.85, "careful": 0.7, "balanced": 0.55}
CARE_LABELS = {
    "very_careful": "Very careful",
    "careful": "Careful",
    "balanced": "Balanced",
}
EXTRACT_TYPES = ("text", "email", "phone", "number", "date")


def clean_labels(raw) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        label = " ".join(
            str(item.get("label") if isinstance(item, dict) else item).split()
        )[:30]
        if label and label.casefold() not in {o.casefold() for o in out}:
            out.append(label)
    return out[:MAX_LABELS]


def _message(run) -> str:
    return str(((run.context or {}).get("message") or {}).get("body") or "").strip()


BUDGET_REACHED = "This chat's daily AI limit has been reached."


def _within_budget(run) -> bool:
    """Whether this run may use AI now. A website chatbot's run (``chatbot_id`` in its context)
    takes one of the bot's daily AI turns; any other run only has the business's own limits,
    which ``apps.ai`` checks."""
    chatbot_id = (run.context or {}).get("chatbot_id")
    if not chatbot_id:
        return True
    from apps.chatbot.api import take_ai_turn

    return take_ai_turn(chatbot_id)


def _operation(run, step) -> str:
    digest = hashlib.sha1(_message(run).encode(), usedforsecurity=False).hexdigest()[
        :12
    ]
    return f"wf-ai:{run.pk}:{step.get('id')}:{digest}"


def corrections_for(run, step) -> list[tuple[str, str]]:
    """``(message, label)`` pairs owners corrected on this step, newest first."""
    from apps.automation.models import WorkflowStepRun

    rows = (
        WorkflowStepRun.objects.filter(
            run__workflow_id=run.workflow_id,
            step_id=step.get("id"),
            result__corrected__isnull=False,
        )
        .order_by("-executed_at")
        .values_list("result", flat=True)[:20]
    )
    return [
        (r.get("message", ""), r["corrected"])
        for r in rows
        if r.get("message") and r.get("corrected")
    ]


def _keyword_label(step, text: str) -> str | None:
    from apps.automation import keywords

    hits = [
        item["label"]
        for item in step.get("labels") or []
        if item.get("keywords")
        and keywords.matches({"mode": "contains", "any": item["keywords"]}, text)
    ]
    return hits[0] if len(hits) == 1 else None


def run_classify(run, step) -> dict:
    from apps.ai import api as ai_api

    text = _message(run)
    labels = [
        i for i in step.get("labels") or [] if isinstance(i, dict) and i.get("label")
    ]
    routes = step.get("routes") or {}
    unsure = {
        "_routed_to": step.get("on_unsure") or "",
        "label": None,
        "message": text[:300],
    }
    if not text:
        return {**unsure, "method": "no_message"}
    by_keyword = _keyword_label(step, text)
    if by_keyword:
        return {
            "_routed_to": routes.get(by_keyword, ""),
            "label": by_keyword,
            "method": "keywords",
            "message": text[:300],
        }
    if not _within_budget(run):
        return {**unsure, "method": "ai_limit"}
    verdict = ai_api.classify(
        run.workflow.account,
        text,
        labels,
        operation_id=_operation(run, step),
        corrections=corrections_for(run, step),
    )
    threshold = CARE.get(step.get("care") or "careful", CARE["careful"])
    if not verdict["label"] or verdict["confidence"] < threshold:
        return {
            **unsure,
            "method": "ai" if verdict["ai"] else "ai_unavailable",
            "guess": verdict["label"],
        }
    return {
        "_routed_to": routes.get(verdict["label"], ""),
        "label": verdict["label"],
        "method": "ai",
        "message": text[:300],
    }


def run_extract(run, step) -> dict:
    from apps.ai import api as ai_api
    from apps.conversations import forms as conversation_forms

    text = _message(run)
    fields = [
        f for f in step.get("fields") or [] if isinstance(f, dict) and f.get("key")
    ]
    found_raw = (
        ai_api.extract(
            run.workflow.account, text, fields, operation_id=_operation(run, step)
        )
        if text and _within_budget(run)
        else {}
    )
    saved, missing = {}, []
    for f in fields:
        value = None
        if f["key"] in found_raw:
            value = conversation_forms.validate(
                {"field_type": f.get("type") or "text"}, found_raw[f["key"]]
            )
        if value is None:
            if f.get("required", True):
                missing.append(f["key"])
            continue
        saved[f["key"]] = value
        conversation_forms._apply_mapping(run.contact, f.get("save_to") or "", value)
    run.context = {
        **(run.context or {}),
        "extracted": {**((run.context or {}).get("extracted") or {}), **saved},
    }
    run.save(update_fields=["context"])
    result = {"found": saved, "missing": missing, "message": text[:300]}
    if missing:
        result["_routed_to"] = step.get("on_missing") or ""
    return result


def run_answer(run, step) -> dict:
    from apps.ai import api as ai_api
    from apps.automation.workflow_engine import (
        _conversation_for_run,
        _require_open_window,
        send_key,
    )
    from apps.core.actions import ActionError, run_action

    conversation = _conversation_for_run(run, step)
    _require_open_window(run, step, conversation)
    if _within_budget(run):
        out = ai_api.grounded_answer(conversation, operation_id=_operation(run, step))
    else:
        out = {"ok": False, "text": "", "failed": BUDGET_REACHED}
    if not out["ok"]:
        return {
            "_routed_to": step.get("on_handoff") or "",
            "handed_off": True,
            "why": out["failed"],
            "message": _message(run)[:300],
        }
    try:
        sent = run_action(
            "reply",
            {"account": run.workflow.account},
            conversation=conversation,
            body=out["text"],
            idempotency_key=send_key(run, step.get("id")),
        )
    except ActionError as exc:
        raise ValueError(f"ai_answer step {step.get('id')!r}: {exc}") from exc
    return {
        **(sent or {}),
        "answered": out["text"][:500],
        "message": _message(run)[:300],
    }


def validate(step: dict, sid: str, error, ids: set) -> None:
    """Shape checks, plus the rule that every AI step has somewhere to go without AI."""
    t = step.get("type")
    if t == "ai_classify":
        labels = clean_labels(step.get("labels"))
        if len(labels) < 2:
            error(
                f"step {sid!r}: give at least two labels to sort into",
                step_id=sid,
                field="labels",
            )
        if not step.get("on_unsure"):
            error(
                f"step {sid!r}: choose where messages go when it isn't sure (or AI is off)",
                step_id=sid,
                field="on_unsure",
            )
        if (step.get("care") or "careful") not in CARE:
            error(
                f"step {sid!r}: choose how careful it should be",
                step_id=sid,
                field="care",
            )
    elif t == "ai_extract":
        fields = [
            f for f in step.get("fields") or [] if isinstance(f, dict) and f.get("key")
        ]
        if not fields:
            error(
                f"step {sid!r}: say which details to look for",
                step_id=sid,
                field="fields",
            )
        if any((f.get("type") or "text") not in EXTRACT_TYPES for f in fields):
            error(f"step {sid!r}: unknown kind of detail", step_id=sid, field="fields")
        if not step.get("on_missing"):
            error(
                f"step {sid!r}: choose where to go when a detail is missing (or AI is off)",
                step_id=sid,
                field="on_missing",
            )
    elif t == "ai_answer":
        if not step.get("on_handoff"):
            error(
                f"step {sid!r}: choose what happens when AI can't answer safely (for example "
                "a holding reply and a follow-up)",
                step_id=sid,
                field="on_handoff",
            )
