"""Durable executor for lifecycle Workflows (Phase 6).

A run walks the ``definition["steps"]`` graph one step at a time. Synchronous
steps run inline; a ``wait`` step parks the run with ``next_due_at`` and the
``run_due_workflows`` beat task resumes it. Every step writes a
``WorkflowStepRun`` so a crashed/re-run ``advance_run`` never double-sends.

Definition shape::

    {
        "trigger": {"type": "business_event", "name": "signup.completed"},
        "steps": [
            {
                "id": "welcome",
                "type": "send_email",
                "template": "welcome",
                "from": "hi@acme.com",
                "next": "wait1",
            },
            {"id": "wait1", "type": "wait", "seconds": 172800, "next": "check"},
            {
                "id": "check",
                "type": "branch",
                "field": "opened_in_last_90d",
                "operator": "eq",
                "value": true,
                "on_true": "tips",
                "on_false": "reminder",
            },
            {
                "id": "tips",
                "type": "send_email",
                "template": "tips",
                "from": "hi@acme.com",
                "next": "stop",
            },
            {
                "id": "reminder",
                "type": "send_email",
                "template": "reminder",
                "from": "hi@acme.com",
                "next": "stop",
            },
            {"id": "stop", "type": "stop"},
        ],
    }
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.automation.models import (
    Workflow,
    WorkflowInteraction,
    WorkflowRun,
    WorkflowStepRun,
)
from apps.core.channels import ChannelStateUnknownError

logger = logging.getLogger(__name__)

_MAX_STEPS_PER_CALL = 50

_STEP_TYPES = {
    "send_email",
    "send_whatsapp",
    "webhook",
    "wait",
    "branch",
    "set_attribute",
    "stop",
    "exit",
    # Free-text reply into the conversation that triggered the run (message triggers only).
    "reply_text",
    # Label the customer ("pricing-enquiry"); adding or removing is idempotent.
    "add_tag",
    "remove_tag",
    # WhatsApp reply buttons / lists, and "ask, then wait for the customer's choice".
    "send_buttons",
    "send_list",
    "wait_for_reply",
    # Lifecycle: track the customer, say how interested they are, hand them to a teammate,
    # and tell the team.
    "create_lead",
    "update_lead_status",
    "assign_conversation",
    "notify_team",
    # Phase 4: a generic step that calls through the shared Action Registry
    # (apps.core.actions) by name, rather than requiring a hand-written
    # ``_run_<type>`` function per capability. New actions (CRM, Commerce,
    # and anything Phase 5/6 adds) are usable from a Workflow the moment
    # they're registered — no workflow_engine change needed.
    "action",
    # B — asks the contact a free-text question, validates the answer against a
    # CustomAttributeDef (C), and saves it. Supports re-prompts on invalid answers.
    "ask_question",
    # D — BM25 product lookup; routes to on_found/on_not_found/on_ambiguous/on_no_price/on_error.
    "lookup_product",
    # Publishing — publishes a ContentPost (apps.publishing) immediately.
    "publish_post",
    # A reminder on the team's follow-up list (never a message to the customer).
    "create_followup",
    # Send one of the business's forms (a WhatsApp Flow, or questions one by one) and wait
    # until the customer finishes it (``next``) or it times out (``on_timeout``).
    "start_form",
    # Optional AI, each with a declared way to go without it (apps.automation.ai_steps).
    "ai_classify",
    "ai_extract",
    "ai_answer",
}
_TRIGGER_TYPES = {
    "business_event",
    "manual",
    "contact.created",
    "contact.updated",
    "email.opened",
    "email.clicked",
    "whatsapp.received",
    # Phase 1 operational spine: channel-agnostic equivalent of
    # "whatsapp.received" — fired once per channel from apps.conversations
    # once a generic Conversation/Message/Event exists, so a Workflow can be
    # written against "a customer messaged us" without naming a channel.
    "conversation.message_received",
    # Phase 2 thin CRM: fired from apps.crm.services alongside the durable
    # Event and the legacy AutomationRule.TriggerEvent dispatch.
    "lead.created",
    "deal.created",
    "deal.stage_changed",
    # Phase 3 minimal Commerce: fired from apps.commerce.services.
    "order.created",
    "order.paid",
    # Lead status: fired when a person or automation moves a lead (see apps.crm.services).
    "lead.status_changed",
    "lead.qualified",
    "lead.lost",
    # A customer finished one of the business's forms (optionally: a given form).
    "conversation.form_completed",
    # Website chat (Chatbot Studio, docs/plans/chatbot-studio.md). Only a chatbot's own
    # workflow uses these, and apps.chatbot enrols it directly: a website visitor never starts
    # the account's other message workflows, and these never fire on WhatsApp/Instagram/
    # Facebook. "opened" = the visitor opened the chat window, before typing anything.
    "website_chat.opened",
    "website_chat.message_received",
}

# Triggers that carry the customer's message, so a keyword ``match`` can be applied to them.
_MESSAGE_TRIGGERS = {
    "conversation.message_received",
    "whatsapp.received",
    "website_chat.message_received",
}
# Triggers that start a run inside a customer's conversation, so steps that reply in that
# conversation (reply_text, buttons, questions) have somewhere to say it.
CONVERSATION_TRIGGERS = frozenset(
    {
        "conversation.message_received",
        "website_chat.message_received",
        "website_chat.opened",
    }
)
# A keyword workflow answers a repeated question once per window, not once per message.
DEFAULT_KEYWORD_COOLDOWN_MINUTES = 60
# How long a "wait for reply" step waits for the customer's choice by default (a day), and
# the longest it may wait.
DEFAULT_REPLY_TIMEOUT_SECONDS = 86400
MAX_REPLY_TIMEOUT_SECONDS = 30 * 86400


def validate_definition(definition: dict, account=None) -> list[dict]:
    """Return a list of problems with ``definition`` (empty = OK).

    Each error is ``{"step_id": str | None, "field": str | None, "message": str}``.
    ``step_id`` is ``None`` for trigger-level/structural errors; ``field`` names the
    offending key on that step where applicable (e.g. ``"next"``, ``"template"``).

    ``account``, when given, enables account-aware ``send_whatsapp`` checks: that
    the named template actually exists for this account (hard error) and, if
    found, whether it's still pending Meta approval (a warning — drafts may
    legitimately reference a template before it's approved).
    """
    from django.utils.dateparse import parse_datetime

    errors: list[dict] = []

    def _error(
        message: str,
        *,
        step_id: str | None = None,
        field: str | None = None,
        severity: str = "error",
    ) -> None:
        errors.append(
            {
                "step_id": step_id,
                "field": field,
                "message": message,
                "severity": severity,
            }
        )

    definition = definition or {}
    trig = definition.get("trigger") or {}
    if trig.get("type") not in _TRIGGER_TYPES:
        _error(
            f"trigger.type must be one of {sorted(_TRIGGER_TYPES)}",
            field="trigger.type",
        )
    elif trig.get("type") == "business_event" and not trig.get("name"):
        _error("business_event trigger requires trigger.name", field="trigger.name")

    if "match" in trig:
        from apps.automation import keywords

        if trig.get("type") not in _MESSAGE_TRIGGERS:
            _error(
                "trigger.match only works on a 'customer messages you' trigger",
                field="trigger.match",
            )
        else:
            for problem in keywords.validate(trig["match"]):
                _error(problem, field="trigger.match")
    if "reply_id" in trig:
        wanted = trig["reply_id"]
        wanted = [wanted] if isinstance(wanted, str) else wanted
        if trig.get("type") not in _MESSAGE_TRIGGERS:
            _error(
                "trigger.reply_id only works on a 'customer messages you' trigger",
                field="trigger.reply_id",
            )
        elif (
            not isinstance(wanted, list)
            or not wanted
            or not all(isinstance(w, str) and w.strip() for w in wanted)
        ):
            _error(
                "trigger.reply_id must be a button name or a list of them",
                field="trigger.reply_id",
            )
    if "from_ad" in trig:
        wanted_ads = trig["from_ad"]
        if trig.get("type") != "conversation.message_received":
            # "whatsapp.received" starts before Akilent has read the ad off the message.
            _error(
                "trigger.from_ad only works on the 'a customer messages you' trigger",
                field="trigger.from_ad",
            )
        elif wanted_ads is not True and not (
            isinstance(wanted_ads, list)
            and wanted_ads
            and all(isinstance(a, str) and a.strip() for a in wanted_ads)
        ):
            _error(
                "trigger.from_ad must be true (any Meta ad) or a list of ad ids",
                field="trigger.from_ad",
            )
    cooldown = trig.get("cooldown_minutes")
    if cooldown is not None and (
        not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0
    ):
        _error(
            "trigger.cooldown_minutes must be a whole number of minutes (0 or more)",
            field="trigger.cooldown_minutes",
        )

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        _error("definition.steps must be a non-empty list")
        return errors

    ids: set[str] = set()
    for i, step in enumerate(steps):
        sid = step.get("id")
        if not sid:
            _error(f"steps[{i}] is missing an id", field="id")
            continue
        if sid in ids:
            _error(f"duplicate step id {sid!r}", step_id=sid, field="id")
        ids.add(sid)
        if step.get("type") not in _STEP_TYPES:
            _error(
                f"step {sid!r}: type must be one of {sorted(_STEP_TYPES)}",
                step_id=sid,
                field="type",
            )
        if step.get("type") == "send_email" and not (
            step.get("template") or step.get("subject")
        ):
            _error(
                f"step {sid!r}: send_email needs a template or subject",
                step_id=sid,
                field="template",
            )
        if step.get("type") == "send_email" and not step.get("from"):
            _error(
                f"step {sid!r}: send_email needs a from address",
                step_id=sid,
                field="from",
            )
        if step.get("type") == "send_email" and str(
            step.get("from", "")
        ).lower().endswith("@example.com"):
            _error(
                f"step {sid!r}: choose the address this email comes from (it still says "
                f"{step['from']})",
                step_id=sid,
                field="from",
            )
        if (
            step.get("type") == "send_email"
            and step.get("template")
            and account is not None
            and _resolve_template_id(account, step["template"]) is None
        ):
            # Without this the step would send a subject with an empty body.
            _error(
                f"step {sid!r}: there's no email template called {step['template']!r}; "
                "pick one of yours or write the message here",
                step_id=sid,
                field="template",
            )
        if step.get("fallback_template") and account is not None:
            from apps.whatsapp.models import MessageTemplate

            fb = MessageTemplate.objects.filter(
                account=account, whatsapp_template_name=step["fallback_template"]
            ).first()
            if fb is None:
                _error(
                    f"step {sid!r}: fallback template {step['fallback_template']!r} not found",
                    step_id=sid,
                    field="fallback_template",
                )
            else:
                mapping = step.get("fallback_variable_mapping") or {}
                missing = [v for v in (fb.variables or []) if v not in mapping]
                if missing:
                    _error(
                        f"step {sid!r}: fallback template needs a value for "
                        f"{', '.join(missing)}",
                        step_id=sid,
                        field="fallback_variable_mapping",
                    )
        if step.get("type") == "send_whatsapp" and not step.get("template"):
            _error(
                f"step {sid!r}: send_whatsapp needs a template",
                step_id=sid,
                field="template",
            )
        if (
            step.get("type") == "send_whatsapp"
            and step.get("template")
            and account is not None
        ):
            from apps.whatsapp.models import MessageTemplate

            template = MessageTemplate.objects.filter(
                account=account, whatsapp_template_name=step["template"]
            ).first()
            if template is None:
                _error(
                    f"step {sid!r}: template {step['template']!r} not found",
                    step_id=sid,
                    field="template",
                )
            else:
                if template.approval_status != template.ApprovalStatus.APPROVED:
                    _error(
                        f"step {sid!r}: template {step['template']!r} is not yet approved "
                        f"by Meta (status={template.approval_status}) — sends will fail until approved",
                        step_id=sid,
                        field="template",
                        severity="warning",
                    )
                mapping = step.get("variable_mapping") or {}
                missing = [v for v in (template.variables or []) if v not in mapping]
                if missing:
                    _error(
                        f"step {sid!r}: template {step['template']!r} needs a mapping for "
                        f"variable(s) {', '.join(missing)}",
                        step_id=sid,
                        field="variable_mapping",
                    )
                from apps.automation import variables as wa_variables

                for var in template.variables or []:
                    if (
                        var in mapping
                        and wa_variables.looks_like_name(var)
                        and wa_variables.is_fixed_text(mapping[var])
                    ):
                        _error(
                            f"step {sid!r}: '{var}' is fixed text, so every customer would get the same "
                            "name. Fill it from the customer's own details instead",
                            step_id=sid,
                            field="variable_mapping",
                            severity="warning",
                        )
        if step.get("type") == "action":
            if not step.get("action"):
                _error(
                    f"step {sid!r}: action needs an action name",
                    step_id=sid,
                    field="action",
                )
            else:
                from apps.core.actions import ActionError, get_action

                try:
                    registered = get_action(step["action"])
                except ActionError:
                    _error(
                        f"step {sid!r}: no action registered as {step['action']!r}",
                        step_id=sid,
                        field="action",
                    )
                else:
                    params = step.get("params") or {}
                    required = registered.input_schema().get("required", [])
                    for field_name in required:
                        # "contact" and "account" are auto-injected from the run at
                        # execution time (see _build_action_kwargs) — never required
                        # in params. Object-typed fields may instead be supplied as
                        # "<field>_id" (resolved by public_id at execution time).
                        if field_name in ("contact", "account"):
                            continue
                        if field_name in params or f"{field_name}_id" in params:
                            continue
                        _error(
                            f"step {sid!r}: action {step['action']!r} needs "
                            f"{field_name!r} in params (or {field_name}_id)",
                            step_id=sid,
                            field="params",
                        )
        if step.get("type") == "reply_text":
            if not (step.get("text") or "").strip():
                _error(
                    f"step {sid!r}: reply_text needs the text to send",
                    step_id=sid,
                    field="text",
                )
            if trig.get("type") not in CONVERSATION_TRIGGERS:
                _error(
                    f"step {sid!r}: reply_text needs the 'customer messages you' trigger, "
                    "because it replies in that conversation",
                    step_id=sid,
                    field="type",
                )
        if (
            step.get("type") == "create_lead"
            and len(str(step.get("source") or "")) > 50
        ):
            _error(
                f"step {sid!r}: keep the source under 50 characters",
                step_id=sid,
                field="source",
            )
        if step.get("type") == "update_lead_status":
            from apps.crm.services import SETTABLE_LEAD_STATUSES

            if step.get("status") not in {s.value for s in SETTABLE_LEAD_STATUSES}:
                _error(
                    f"step {sid!r}: choose new, contacted, qualified or lost",
                    step_id=sid,
                    field="status",
                )
        if step.get("type") == "assign_conversation":
            who = step.get("to")
            if who not in (None, "") and not (isinstance(who, str) and "@" in who):
                _error(
                    f"step {sid!r}: 'to' is a teammate's email, or empty for the least busy",
                    step_id=sid,
                    field="to",
                )
        if step.get("type") == "notify_team":
            who = step.get("to") or "owners"
            if who not in ("owners", "assignee") and not (
                isinstance(who, str) and "@" in who
            ):
                _error(
                    f"step {sid!r}: tell your owners, the assignee, or a teammate's email",
                    step_id=sid,
                    field="to",
                )
            if not (step.get("text") or "").strip():
                _error(
                    f"step {sid!r}: write what the team should be told",
                    step_id=sid,
                    field="text",
                )
            elif len(step["text"]) > 1000:
                _error(
                    f"step {sid!r}: keep the message under 1000 characters",
                    step_id=sid,
                    field="text",
                )
        if step.get("type") in ("send_buttons", "send_list"):
            from apps.whatsapp import interactive as wa_interactive

            try:
                if step["type"] == "send_buttons":
                    wa_interactive.build_buttons(step.get("text"), step.get("buttons"))
                else:
                    wa_interactive.build_list(
                        step.get("text"), step.get("button"), step.get("rows")
                    )
            except wa_interactive.InteractiveError as exc:
                _error(
                    f"step {sid!r}: {exc}",
                    step_id=sid,
                    field="buttons" if step["type"] == "send_buttons" else "rows",
                )
            _trig_type = trig.get("type")
            _all_channels = _trig_type in CONVERSATION_TRIGGERS
            if not _all_channels:
                _error(
                    f"step {sid!r}: {step['type']} needs the 'customer messages you' trigger, "
                    "because it replies in that conversation",
                    step_id=sid,
                    field="type",
                )
            # send_list fails at runtime on IG conversations; reject at publish time
            # only when the account actually has an active IG channel.
            if step["type"] == "send_list":
                try:
                    from apps.facebook.models.page import active_page
                    from apps.instagram.models.account import InstagramBusinessAccount

                    _has_ig = account is not None and (
                        InstagramBusinessAccount.objects.filter(
                            account=account, is_active=True
                        ).exists()
                        or active_page(account) is not None
                    )
                except Exception:
                    _has_ig = False
                if _has_ig:
                    _error(
                        f"step {sid!r}: Instagram and Messenger don't support lists — use send_buttons instead",
                        step_id=sid,
                        field="type",
                    )
        if step.get("type") == "wait_for_reply":
            routes = step.get("routes")
            if not isinstance(routes, dict) or not routes:
                _error(
                    f"step {sid!r}: wait_for_reply needs at least one choice to route",
                    step_id=sid,
                    field="routes",
                )
            timeout = step.get("timeout_seconds", DEFAULT_REPLY_TIMEOUT_SECONDS)
            if (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or not 60 <= timeout <= MAX_REPLY_TIMEOUT_SECONDS
            ):
                _error(
                    f"step {sid!r}: wait between 1 minute and 30 days",
                    step_id=sid,
                    field="timeout_seconds",
                )
        if step.get("type") == "ask_question":
            _validate_ask_question_step(step, trig, steps, ids, sid, _error, account)
        if step.get("type") in ("ai_classify", "ai_extract", "ai_answer"):
            from apps.automation import ai_steps

            ai_steps.validate(step, sid, _error, ids)
        if step.get("type") == "start_form":
            if not step.get("form"):
                _error(
                    f"step {sid!r}: choose which form to send",
                    step_id=sid,
                    field="form",
                )
            elif account is not None:
                from apps.conversations.models import ConversationForm

                form = ConversationForm.objects.filter(
                    account=account, public_id=step["form"]
                ).first()
                if form is None:
                    _error(
                        f"step {sid!r}: that form no longer exists",
                        step_id=sid,
                        field="form",
                    )
                elif form.status != ConversationForm.Status.PUBLISHED:
                    _error(
                        f"step {sid!r}: the form “{form.name}” is still a draft; publish it",
                        step_id=sid,
                        field="form",
                    )
        if step.get("type") == "create_followup":
            try:
                hours = float(step.get("due_in_hours") or 24)
            except (TypeError, ValueError):
                hours = -1
            if not 0.25 <= hours <= 24 * 60:
                _error(
                    f"step {sid!r}: make it due between 15 minutes and 60 days from now",
                    step_id=sid,
                    field="due_in_hours",
                )
            to = (step.get("assign_to") or "").strip()
            if to and to != "assignee" and "@" not in to:
                _error(
                    f"step {sid!r}: give it to whoever is assigned to the chat, a "
                    "teammate, or the whole team",
                    step_id=sid,
                    field="assign_to",
                )
        if step.get("type") == "lookup_product":
            if not step.get("on_error"):
                _error(
                    f"step {sid!r}: lookup_product must have on_error",
                    step_id=sid,
                    field="on_error",
                )
        if step.get("type") in ("reply_text", "send_whatsapp", "send_email"):
            import re as _re

            _text = step.get("text") or step.get("body") or ""
            _vars = _re.findall(r"\{([^{}]+)\}", _text)
            if _vars and not step.get("on_error"):
                _error(
                    f"step {sid!r}: uses template variables {_vars!r} but has no on_error",
                    step_id=sid,
                    field="on_error",
                    severity="warning",
                )
        if step.get("type") in ("add_tag", "remove_tag"):
            from apps.contacts import tags as contact_tags

            try:
                contact_tags.clean_name(step.get("tag") or "")
            except contact_tags.TagError as exc:
                _error(f"step {sid!r}: {exc}", step_id=sid, field="tag")
        if step.get("type") == "webhook" and not step.get("url"):
            _error(f"step {sid!r}: webhook needs a url", step_id=sid, field="url")
        if step.get("type") == "branch" and not (
            step.get("on_true") and step.get("on_false") and step.get("field")
        ):
            _error(
                f"step {sid!r}: branch needs field, on_true, on_false",
                step_id=sid,
                field="field",
            )
        if step.get("type") in ("send_email", "send_whatsapp") and step.get("send_at"):
            if parse_datetime(step["send_at"]) is None:
                _error(
                    f"step {sid!r}: send_at is not a valid datetime",
                    step_id=sid,
                    field="send_at",
                )

    def _check(ref, sid, field):
        if ref and ref not in ids:
            _error(
                f"step {sid!r}.{field} points to unknown step {ref!r}",
                step_id=sid,
                field=field,
            )

    for step in steps:
        sid = step.get("id")
        if not sid:
            continue
        if step.get("type") == "branch":
            _check(step.get("on_true"), sid, "on_true")
            _check(step.get("on_false"), sid, "on_false")
        elif step.get("type") == "wait_for_reply":
            for target in (step.get("routes") or {}).values():
                _check(target, sid, "routes")
            _check(step.get("default"), sid, "default")
            _check(step.get("on_timeout"), sid, "on_timeout")
        elif step.get("type") == "ask_question":
            _check(step.get("next"), sid, "next")
            _check(step.get("on_timeout"), sid, "on_timeout")
            _check(step.get("on_invalid"), sid, "on_invalid")
            _check(step.get("on_error"), sid, "on_error")
        elif step.get("type") in ("ai_classify", "ai_extract", "ai_answer"):
            for field in ("next", "on_unsure", "on_missing", "on_handoff"):
                _check(step.get(field), sid, field)
            for target in (step.get("routes") or {}).values():
                _check(target, sid, "routes")
        elif step.get("type") in ("start_form", "lookup_product", "publish_post"):
            for field in (
                "next",
                "on_timeout",
                "on_error",
                "on_found",
                "on_not_found",
                "on_ambiguous",
                "on_no_price",
            ):
                _check(step.get(field), sid, field)
        elif step.get("type") not in ("stop", "exit"):
            _check(step.get("next"), sid, "next")

    # A step runs at most once per run, so a path that leads back to an earlier step never
    # repeats it: it would spin through skipped steps and strand the run. Reject loops outright.
    graph = {
        step["id"]: [t for t in step_targets(step) if t in ids]
        for step in steps
        if step.get("id")
    }
    looped = _first_cycle(graph)
    if looped:
        _error(
            f"step {looped!r} leads back to an earlier step. Automations can't loop; "
            "point it at a later step or a stop.",
            step_id=looped,
            field="next",
        )
    return errors


_TARGET_FIELDS = (
    "next",
    "on_true",
    "on_false",
    "default",
    "on_timeout",
    "on_invalid",
    "on_error",
    "on_found",
    "on_not_found",
    "on_ambiguous",
    "on_no_price",
    "on_unsure",
    "on_missing",
    "on_handoff",
)


def step_targets(step: dict) -> list[str]:
    """Every step id ``step`` can hand the run to next, whatever its type."""
    targets = [
        step[f] for f in _TARGET_FIELDS if isinstance(step.get(f), str) and step[f]
    ]
    targets += [
        t for t in (step.get("routes") or {}).values() if isinstance(t, str) and t
    ]
    return targets


def _first_cycle(graph: dict[str, list[str]]) -> str | None:
    """The id of a step whose edge closes a cycle in ``graph``, or None."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = fully explored
    for root, edges in graph.items():
        if state.get(root):
            continue
        stack = [(root, iter(edges))]
        state[root] = 1
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2
                stack.pop()
            elif state.get(child) == 1:
                return node
            elif not state.get(child):
                state[child] = 1
                stack.append((child, iter(graph.get(child, []))))
    return None


def _steps_by_id(workflow: Workflow) -> dict:
    return {
        s["id"]: s for s in (workflow.definition or {}).get("steps", []) if "id" in s
    }


def run_definition(run: WorkflowRun) -> dict:
    """The definition ``run`` follows: its pinned snapshot, else (older runs) the live one."""
    if run.version is not None:
        return run.version.definition or {}
    return run.workflow.definition or {}


def _run_steps(run: WorkflowRun) -> dict:
    return {s["id"]: s for s in run_definition(run).get("steps", []) if "id" in s}


def _digest(definition: dict) -> str:
    import hashlib
    import json

    canonical = json.dumps(definition or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def snapshot(workflow: Workflow):
    """The ``WorkflowVersion`` holding ``workflow.definition`` as it is now (made if new).

    Keyed by content, so every path that writes a definition (editor, API, starters,
    Build) gets a snapshot without having to remember to make one.
    """
    from django.db import IntegrityError
    from django.db.models import Max

    from apps.automation.models import WorkflowVersion

    digest = _digest(workflow.definition)
    found = WorkflowVersion.objects.filter(workflow=workflow, digest=digest).first()
    if found is not None:
        return found
    for _ in range(3):
        number = (
            WorkflowVersion.objects.filter(workflow=workflow).aggregate(
                m=Max("number")
            )["m"]
            or 0
        ) + 1
        try:
            with transaction.atomic():
                return WorkflowVersion.objects.create(
                    workflow=workflow,
                    number=number,
                    digest=digest,
                    definition=workflow.definition or {},
                )
        except IntegrityError:
            found = WorkflowVersion.objects.filter(
                workflow=workflow, digest=digest
            ).first()
            if found is not None:
                return found
    raise RuntimeError(f"could not snapshot workflow {workflow.pk}")


def _first_step_id(workflow: Workflow) -> str | None:
    steps = (workflow.definition or {}).get("steps", [])
    return steps[0]["id"] if steps else None


def enroll(
    workflow: Workflow, contact, *, context: dict | None = None, subject_key: str = ""
) -> WorkflowRun | None:
    """Start a run for ``contact`` on ``workflow`` (no-op if one is already active).

    "Already active" is per ``subject_key``: with the default empty key that is one run
    per contact; with e.g. ``"order:<id>"`` each subject gets its own concurrent run.
    """
    if workflow.status != Workflow.Status.PUBLISHED or not workflow.account.is_active:
        return None  # a suspended business starts nothing new
    from apps.billing import api as billing_api

    if not billing_api.usable(workflow.account, "automations"):
        return None
    first = _first_step_id(workflow)
    if first is None:
        return None
    if _opted_out_of_this(workflow, contact, context or {}):
        return None  # they said STOP: don't start something that can only fail at send
    in_flight = dict(
        workflow=workflow,
        contact=contact,
        subject_key=subject_key,
        status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
    )
    # Most messages come from someone already part-way through: answer that cheaply,
    # before hashing the definition for a version snapshot only a new run needs.
    existing = WorkflowRun.objects.filter(**in_flight).first()
    if existing is not None:
        return existing
    run, created = WorkflowRun.objects.get_or_create(
        **in_flight,
        defaults={
            "context": context or {},
            "current_step": first,
            "version": snapshot(workflow),
        },
    )
    if not created:
        return run
    _notify_workflow(run, "workflow.started")
    advance_run(run)
    return run


# Steps that message the customer on the conversation's own channel.
_CONVERSATION_SEND_STEPS = frozenset(
    {"reply_text", "send_buttons", "send_list", "ask_question"}
)


def _opted_out_of_this(workflow: Workflow, contact, context: dict) -> bool:
    """True when ``contact`` opted out of WhatsApp and ``workflow`` would message them there."""
    if not getattr(contact, "whatsapp_opted_out", False):
        return False
    types = {s.get("type") for s in (workflow.definition or {}).get("steps", [])}
    if "send_whatsapp" in types:
        return True
    if not types & _CONVERSATION_SEND_STEPS:
        return False
    conversation_id = context.get("conversation_id")
    if not conversation_id:
        return True  # it would fall back to their WhatsApp conversation
    from apps.conversations.models import Conversation

    channel = (
        Conversation.objects.filter(public_id=conversation_id)
        .values_list("channel", flat=True)
        .first()
    )
    return channel in (None, Conversation.Channel.WHATSAPP)


def is_consent_keyword(text: str) -> bool:
    """A lone STOP / START: a consent instruction, never a question for an automation."""
    from django.conf import settings

    token = (text or "").strip().strip(".!?").upper()
    return bool(token) and (
        token in getattr(settings, "WHATSAPP_STOP_KEYWORDS", ())
        or token in getattr(settings, "WHATSAPP_START_KEYWORDS", ())
    )


def _notify_workflow(run: WorkflowRun, event_type: str) -> None:
    try:
        from apps.email.webhooks import notify

        notify(
            run.workflow.account,
            event_type,
            {
                "run_id": run.public_id,
                "workflow": run.workflow.slug,
                "contact": run.contact.public_id,
                "status": run.status,
            },
        )
    except Exception:
        logger.exception("_notify_workflow failed for run %s", run.pk)


_TRUTHY = {True, "true", "True", "yes", "1"}


def _condition_matches(contact, step: dict) -> bool:
    from apps.contacts.models import Contact
    from apps.contacts.segments import build_q

    if step["field"] == "within_business_hours":
        # A fact about the business, not the customer, so it is answered here rather than
        # by the contact query. value true (the default) matches while the business is
        # open; value false, or operator "ne", matches while it is closed.
        from apps.accounts import business_hours

        wants_open = step.get("value", True) in _TRUTHY
        if step.get("operator", "eq") == "ne":
            wants_open = not wants_open
        return business_hours.is_open(contact.account) is wants_open

    cond = {
        "field": step["field"],
        "operator": step.get("operator", "eq"),
        "value": step.get("value"),
    }
    q = build_q({"op": "and", "conditions": [cond]})
    return Contact.objects.filter(pk=contact.pk).filter(q).exists()


def _run_send_email(run: WorkflowRun, step: dict) -> dict:
    from django.utils.dateparse import parse_datetime

    from apps.api.services import create_and_queue_message
    from apps.core.channels import ChannelStateUnknownError, channel_state

    state = channel_state("email")
    if state == "disabled":
        # Admin switched Email off: keep the workflow and its step config, record the step
        # as skipped, and move on. Not a failure, so no retry and no run failure.
        return {"skipped": "channel_disabled", "channel": "email"}
    if state == "unknown":
        # Settings can't be read right now: fail the step so the message is not lost silently.
        raise ChannelStateUnknownError("email")

    contact = run.contact
    if not contact.email:
        raise ValueError(
            f"send_email step requires an email, but contact {contact.pk} has none"
        )
    send_at = parse_datetime(step["send_at"]) if step.get("send_at") else None
    msg = create_and_queue_message(
        account=run.workflow.account,
        from_email=step["from"],
        to_email=contact.email,
        subject=step.get("subject", ""),
        text_body=step.get("text", ""),
        html_body=step.get("html", ""),
        template_id=_resolve_template_id(run.workflow.account, step.get("template")),
        template_variables={
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "email": contact.email,
            "attributes": contact.attributes or {},
            **(run.context or {}),
        },
        scheduled_at=send_at,
        tz=step.get("send_at_tz", ""),
    )
    return {"message_id": msg.public_id}


def _resolve_template_id(account, slug):
    if not slug:
        return None
    from apps.email.models import EmailTemplate

    t = EmailTemplate.objects.filter(account=account, slug=slug, is_active=True).first()
    return t.id if t else None


def _run_send_whatsapp(run: WorkflowRun, step: dict) -> dict:
    """Send a WhatsApp template message via the shared sending path.

    Reuses ``apps.automation.workflows.send_whatsapp_message`` — the same
    function the legacy AutomationRule ``send_whatsapp_message`` action calls —
    so there is one place that talks to WhatsAppContact/MessageTemplate/
    OutboundMessage. Any resolution failure (no phone, unapproved/unknown
    template, no matching WhatsAppContact) raises, which ``advance_run`` turns
    into a FAILED run rather than a silent no-op or a false "completed".
    """
    from apps.core.channels import ChannelStateUnknownError, channel_state

    state = channel_state("whatsapp")
    if state == "disabled":
        return {"skipped": "channel_disabled", "channel": "whatsapp"}
    if state == "unknown":
        raise ChannelStateUnknownError("whatsapp")
    from django.utils.dateparse import parse_datetime

    from apps.automation.workflows import send_whatsapp_message
    from apps.core.scheduling import to_utc

    contact = run.contact
    phone_field = step.get("phone_field", "phone")
    # The canonical number lives on Contact.phone (WhatsApp-originated contacts have
    # no attributes); a custom phone_field still reads from attributes.
    phone = (contact.phone if phone_field == "phone" else None) or (
        contact.attributes or {}
    ).get(phone_field)
    if not phone:
        raise ValueError(
            f"send_whatsapp step {step.get('id')!r}: contact {contact.pk} has no "
            f"{phone_field!r} attribute"
        )

    template = _resolve_whatsapp_template(run.workflow.account, step.get("template"))
    if template is None:
        raise ValueError(
            f"send_whatsapp step {step.get('id')!r}: template {step.get('template')!r} not found"
        )
    if template.approval_status != template.ApprovalStatus.APPROVED:
        raise ValueError(
            f"send_whatsapp step {step.get('id')!r}: template {step.get('template')!r} "
            f"is not approved by Meta (status={template.approval_status})"
        )

    params = _resolve_variable_mapping(
        template.variables,
        step.get("variable_mapping") or {},
        run,
        contact,
        fallbacks=step.get("variable_fallbacks") or {},
    )

    send_at = None
    if step.get("send_at"):
        parsed = parse_datetime(step["send_at"])
        if parsed is not None:
            send_at = to_utc(parsed, step.get("send_at_tz") or "UTC")
    msg = send_whatsapp_message(
        run.workflow.account,
        phone=phone,
        template_id=template.id,
        params=params,
        scheduled_at=send_at,
        auto_create_contact=bool(step.get("auto_create_contact")),
        link_contact=contact,
        sent_by="automation",
    )
    return {"outbound_message_id": msg.id}


def _conversation_for_run(run: WorkflowRun, step: dict, *, fallback: bool = False):
    """The conversation that started this run, or a clear failure if it has none.

    With ``fallback`` (for steps that act on the *customer's* thread rather than reply in one,
    like assigning it), a run that did not start from a message uses the customer's most
    recently active open conversation.
    """
    from apps.conversations.models import Conversation

    public_id = (run.context or {}).get("conversation_id")
    conversation = (
        Conversation.objects.filter(
            account=run.workflow.account, public_id=public_id
        ).first()
        if public_id
        else None
    )
    if conversation is None and fallback:
        conversation = (
            Conversation.objects.filter(
                account=run.workflow.account,
                contact=run.contact,
                status=Conversation.Status.OPEN,
            )
            .order_by("-last_message_at", "-id")
            .first()
        )
    if conversation is None:
        raise ValueError(
            f"{step.get('type')} step {step.get('id')!r}: this run has no conversation to reply in"
        )
    return conversation


def send_key(run: WorkflowRun, step_id) -> str:
    """The idempotency key for a message ``step_id`` sends in ``run``.

    Fixed per run and step, so a worker retry returns the first message instead of sending a
    second. An owner's "Retry" from run history bumps the attempt, so the retried step can
    really send again.
    """
    attempt = ((run.context or {}).get("_retries") or {}).get(str(step_id), 0)
    base = f"wf:{run.pk}:{step_id}"
    return f"{base}:r{attempt}" if attempt else base


def _first_name_merge(run: WorkflowRun, text: str) -> str:
    # Plain replace, not str.format: the text is owner-written and must not be able to
    # reach into objects with "{contact.__class__}" style placeholders.
    from apps.automation.variables import merge_business_facts, merge_first_name

    return merge_business_facts(
        merge_first_name(text, run.contact.first_name), run.contact.account
    )


class WindowClosedError(ValueError):
    """The customer's messaging window has closed, so only an approved template can go out."""


def _window_open_for_run(run: WorkflowRun, conversation) -> bool:
    """Whether a free-form message may go out on ``conversation`` for this run.

    A run started by the customer's own message opened the window itself, for 24 hours
    from that message; otherwise the conversation's recorded window decides.
    """
    from datetime import timedelta

    if (
        (run.context or {}).get("message")
        and run.started_at
        and (run.started_at >= timezone.now() - timedelta(hours=24))
    ):
        return True
    return _messaging_window_open(conversation)


def _require_open_window(run: WorkflowRun, step: dict, conversation) -> None:
    """Fail the step now, with a fix the owner can act on, when the window is closed.

    Checking here (not only when the outbound queue sends) means the run and the owner see
    the real reason straight away, instead of a step that says "sent" and fails later.
    """
    if _window_open_for_run(run, conversation):
        return
    raise WindowClosedError(
        f"Step {step.get('id')!r} couldn't send: the customer hasn't messaged in the "
        "last 24 hours, so only an approved WhatsApp template can reach them now. "
        "Add a fallback template to this step, or start the automation from a "
        "customer's message."
    )


def _run_interactive(run: WorkflowRun, step: dict) -> dict:
    """Send reply buttons (WhatsApp) or quick replies (Instagram, Messenger) for this run.

    WhatsApp: unchanged — sends WA interactive message, run continues to the next
    step (typically ``wait_for_reply``).

    Instagram: in one ``transaction.atomic()``, creates a ``WorkflowInteraction``
    (token embedded in quick-reply payloads), enqueues the IG outbound message,
    and parks the run (WAITING + deadline_at). Returns ``{"_parked": True, ...}``
    so ``advance_run`` stops looping after recording the step.
    """
    from apps.conversations.models import Conversation

    conversation = _conversation_for_run(run, step)
    _require_open_window(run, step, conversation)

    if conversation.channel == Conversation.Channel.WHATSAPP:
        return _run_interactive_whatsapp(run, step, conversation)
    if conversation.channel == Conversation.Channel.INSTAGRAM:
        return _run_interactive_instagram(run, step, conversation)
    if conversation.channel == Conversation.Channel.FACEBOOK:
        # Messenger quick replies work exactly like Instagram's.
        return _run_interactive_instagram(run, step, conversation, channel="facebook")

    raise ValueError(
        f"{step['type']} step {step.get('id')!r}: channel {conversation.channel!r} "
        "does not support interactive steps"
    )


def _run_interactive_whatsapp(run: WorkflowRun, step: dict, conversation) -> dict:
    """WhatsApp interactive step — unchanged from the original implementation."""
    from apps.whatsapp import api as whatsapp_api
    from apps.whatsapp import interactive as wa_interactive

    if conversation.whatsapp_conversation is None:
        raise ValueError(
            f"{step['type']} step {step.get('id')!r}: no WhatsApp conversation"
        )
    text = _first_name_merge(run, step.get("text") or "")
    try:
        if step["type"] == "send_buttons":
            interactive = wa_interactive.build_buttons(text, step.get("buttons") or [])
        else:
            interactive = wa_interactive.build_list(
                text, step.get("button") or "", step.get("rows") or []
            )
    except wa_interactive.InteractiveError as exc:
        raise ValueError(f"{step['type']} step {step.get('id')!r}: {exc}") from exc
    msg = whatsapp_api.send_interactive(
        run.workflow.account,
        conversation.whatsapp_conversation.contact,
        interactive,
        idempotency_key=send_key(run, step.get("id")),
    )
    conversation.whatsapp_conversation.register_outbound(msg.created_at)
    return {"outbound_message_id": msg.id}


# IG quick-reply limits (from Meta's Messenger Platform docs, applies to IG too).
_IG_MAX_QUICK_REPLIES = 13
_IG_MAX_TITLE = 20
_IG_MAX_PAYLOAD = 1000
_IG_DEFAULT_TIMEOUT = 86400  # 24 h


def _run_interactive_instagram(
    run: WorkflowRun, step: dict, conversation, channel: str = "instagram"
) -> dict:
    """IG / Messenger send_buttons step: one transaction opens interaction + enqueues
    message + parks run.

    ``send_list`` is not supported on Instagram or Messenger; publish validation rejects
    it before here.
    """
    from datetime import timedelta

    from django.db import transaction
    from django.utils import timezone
    from django.utils.text import slugify

    from apps.automation.models import WorkflowInteraction

    label = "Messenger" if channel == "facebook" else "Instagram"
    if step.get("type") == "send_list":
        raise ValueError(
            f"step {step.get('id')!r}: {label} does not support lists — use send_buttons instead"
        )

    if channel == "facebook":
        from apps.facebook.models.page import active_page
        from apps.facebook.services.outbound import enqueue_reply as fb_enqueue

        fb_convo = getattr(conversation, "facebook_conversation", None)
        conn = active_page(conversation.account)
        if fb_convo is None or conn is None or conn.page_id != fb_convo.page_id:
            raise ValueError(
                f"step {step.get('id')!r}: no connected Messenger conversation on this spine"
            )
        recipient = fb_convo.facebook_contact.psid

        def enqueue(text, *, idempotency_key, quick_replies):
            return fb_enqueue(
                conn,
                recipient,
                text,
                action_type="dm_reply",
                idempotency_key=idempotency_key,
                quick_replies=quick_replies,
            )

    else:
        from apps.instagram.services.outbound import enqueue_reply as ig_enqueue

        ig_convo = getattr(conversation, "instagram_conversation", None)
        if ig_convo is None:
            raise ValueError(
                f"step {step.get('id')!r}: no Instagram conversation on this spine"
            )
        ig_account = ig_convo.instagram_account
        recipient = ig_convo.instagram_contact.instagram_scoped_id

        def enqueue(text, *, idempotency_key, quick_replies):
            return ig_enqueue(
                ig_account,
                recipient,
                text,
                action_type="dm_reply",
                idempotency_key=idempotency_key,
                quick_replies=quick_replies,
            )

    buttons = step.get("buttons") or []
    if not buttons:
        raise ValueError(f"step {step.get('id')!r}: at least one button is required")
    if len(buttons) > _IG_MAX_QUICK_REPLIES:
        raise ValueError(
            f"step {step.get('id')!r}: {label} allows at most {_IG_MAX_QUICK_REPLIES} quick replies"
        )

    text = _first_name_merge(run, step.get("text") or "")
    step_id = step.get("id") or ""

    # Workflows authored for WhatsApp put timeout_seconds/on_timeout on the following
    # wait_for_reply step. Inherit those settings when the send_buttons step omits them.
    _next_step = _run_steps(run).get(step.get("next") or "") or {}
    _next_is_wait = _next_step.get("type") == "wait_for_reply"
    _timeout_src = (
        step if "timeout_seconds" in step else (_next_step if _next_is_wait else step)
    )
    timeout = int(_timeout_src.get("timeout_seconds", _IG_DEFAULT_TIMEOUT))
    deadline = timezone.now() + timedelta(seconds=timeout)

    # Collect option ids for the interaction expected set.
    buttons = [b if isinstance(b, dict) else {"title": b} for b in buttons]
    option_ids = []
    for btn in buttons:
        title = str(btn.get("title") or "")
        if len(title) > _IG_MAX_TITLE:
            raise ValueError(
                f"step {step_id!r}: button title {title!r} exceeds {_IG_MAX_TITLE} chars"
            )
        # Prefer an explicit id (keeps parity with WA routes); fall back to slug.
        option_ids.append(str(btn.get("id") or slugify(title) or title.lower())[:200])

    # If the following wait_for_reply step has a `default` route (accept any typed answer),
    # set free_text so _pick_target routes typed replies that don't match a button label.
    _free_text = bool(
        _next_is_wait and (_next_step.get("free_text") or _next_step.get("default"))
    )

    with transaction.atomic():
        # 1. Open the interaction — idempotent: reuse any existing open row for this
        # run+step so a concurrent sweeper/fast-path retry doesn't mint a second token
        # that no quick-reply chip will ever carry.
        option_titles = [str(btn.get("title") or "") for btn in buttons]
        interaction, _ = WorkflowInteraction.objects.get_or_create(
            run=run,
            step_id=step_id,
            status=WorkflowInteraction.Status.OPEN,
            defaults={
                "conversation": conversation,
                "expected": {
                    "option_ids": option_ids,
                    "option_titles": option_titles,
                    "free_text": _free_text,
                },
                "deadline_at": deadline,
            },
        )
        token = interaction.token

        # 2. Build quick-reply chip list with token-encoded payloads.
        quick_replies = []
        for btn, oid in zip(buttons, option_ids):
            payload = f"{token}:{oid}"
            if len(payload) > _IG_MAX_PAYLOAD:
                raise ValueError(
                    f"step {step_id!r}: quick-reply payload for {btn.get('title')!r} "
                    f"exceeds {_IG_MAX_PAYLOAD} chars"
                )
            quick_replies.append(
                {
                    "content_type": "text",
                    "title": str(btn.get("title") or ""),
                    "payload": payload,
                }
            )

        # 3. Enqueue the outbound message.
        outbound = enqueue(
            text,
            idempotency_key=send_key(run, step_id),
            quick_replies=quick_replies,
        )

        # 4. Park the run.
        run.status = WorkflowRun.Status.WAITING
        run.next_due_at = deadline
        run.save(update_fields=["status", "next_due_at"])

    outbound_id = outbound.id if outbound else None
    fk = "fb_outbound_message_id" if channel == "facebook" else "ig_outbound_message_id"

    # Record the step run BEFORE attempting to send, so mark_outbound_message_failed
    # (the reconciliation hook) can find the WorkflowStepRun row on an immediate failure.
    # The outer _record call in advance_run will hit an IntegrityError and silently no-op.
    if channel == "facebook":
        _record(run, step, {"_parked": True}, fb_outbound_message_id=outbound_id)
    else:
        _record(run, step, {"_parked": True}, ig_outbound_message_id=outbound_id)

    # Send immediately (don't wait for the next drain tick) so the customer
    # sees the quick-reply chips without a multi-minute delay.
    if outbound is not None:
        send_outbound: Any
        if channel == "facebook":
            from apps.facebook.services.outbound import send_outbound
        else:
            from apps.instagram.services.outbound import send_outbound

        try:
            send_outbound(outbound)
        except Exception:
            logger.exception(
                "_run_interactive_instagram: send_outbound raised outbound=%s run=%s",
                outbound.pk,
                run.pk,
            )

    return {
        "_parked": True,
        fk: outbound_id,
        "interaction_token": token,
    }


# ── B — ask_question step ─────────────────────────────────────────────────────

_ASK_MAX_TIMEOUT = 604800  # 7 days
_ASK_MIN_TIMEOUT = 300  # 5 minutes
_ASK_DEFAULT_TIMEOUT = 86400


def _validate_ask_question_step(step, trig, steps, ids, sid, _error, account):
    """Publish-time validation for ask_question steps."""
    if not (step.get("question") or "").strip():
        _error(
            f"step {sid!r}: ask_question needs a question",
            step_id=sid,
            field="question",
        )
    if not (step.get("attribute") or "").strip():
        _error(
            f"step {sid!r}: ask_question needs an attribute key",
            step_id=sid,
            field="attribute",
        )

    timeout = step.get("timeout_seconds", _ASK_DEFAULT_TIMEOUT)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not _ASK_MIN_TIMEOUT <= timeout <= _ASK_MAX_TIMEOUT
    ):
        _error(
            f"step {sid!r}: timeout_seconds must be between {_ASK_MIN_TIMEOUT} and {_ASK_MAX_TIMEOUT}",
            step_id=sid,
            field="timeout_seconds",
        )
    max_attempts = step.get("max_attempts", 1)
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or not 1 <= max_attempts <= 3
    ):
        _error(
            f"step {sid!r}: max_attempts must be 1, 2, or 3",
            step_id=sid,
            field="max_attempts",
        )

    target = step.get("target", "contact")
    if target not in ("contact", "lead", "deal"):
        _error(
            f"step {sid!r}: target must be contact, lead or deal",
            step_id=sid,
            field="target",
        )

    # For lead/deal targets, verify a create_lead step is reachable before this step
    # (or the trigger is lead.* / deal.*).
    if target in ("lead", "deal"):
        trig_type = trig.get("type", "")
        if not (trig_type.startswith("lead.") or trig_type.startswith("deal.")):
            # Walk backwards: any path from any trigger must pass through create_lead.
            steps_by_id = {s.get("id"): s for s in steps if s.get("id")}
            if not _lead_reachable_before(steps_by_id, sid, steps):
                _error(
                    f"step {sid!r}: target='{target}' requires a create_lead step on every "
                    "path before this step, or a lead.* / deal.* trigger",
                    step_id=sid,
                    field="target",
                )


def _lead_reachable_before(steps_by_id: dict, target_sid: str, all_steps: list) -> bool:
    """Return True if a create_lead step precedes target_sid on every reachable path."""
    # Simple forward reachability: find all steps that can reach target_sid,
    # then check that every path to it passes through a create_lead.
    # We use a backwards DFS: find predecessors of target_sid.
    predecessors: dict[str, list[str]] = {s["id"]: [] for s in all_steps if s.get("id")}
    for step in all_steps:
        sid = step.get("id")
        if not sid:
            continue
        for field in (
            "next",
            "on_true",
            "on_false",
            "on_timeout",
            "on_invalid",
            "on_error",
            "default",
        ):
            ref = step.get(field)
            if ref and ref in predecessors:
                predecessors[ref].append(sid)
        for ref in (step.get("routes") or {}).values():
            if ref and ref in predecessors:
                predecessors[ref].append(sid)

    # BFS back from target_sid; stop at create_lead nodes.
    from collections import deque

    visited = set()
    queue = deque([target_sid])
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for pred in predecessors.get(node, []):
            s = steps_by_id.get(pred)
            if s and s.get("type") == "create_lead":
                continue  # this path is covered
            queue.append(pred)
    # If any predecessor has no predecessors itself (it's a start step) and isn't create_lead,
    # then there's a path without create_lead. A rough check: if any visited node has no
    # predecessors and isn't a create_lead, return False.
    for node in visited:
        if not predecessors.get(node) and node != target_sid:
            s = steps_by_id.get(node)
            if not s or s.get("type") != "create_lead":
                return False
    return True


def _messaging_window_open(conversation) -> bool:
    """True if the conversation's channel window allows free-text replies."""
    from apps.conversations.api import conversation_window_is_open

    return conversation_window_is_open(conversation)


def _run_lookup_product(run: WorkflowRun, step: dict) -> dict:
    """Execute a ``lookup_product`` step.

    The step resolves a query from ``query_variable`` (a ``{variable}`` or literal),
    calls ``lookup_product``, and routes to the appropriate ``on_*`` branch:

    * ``on_found``     — clear winner; product stored in run.context["product_id"]
    * ``on_not_found`` — no product above threshold
    * ``on_ambiguous`` — multiple equally-scored products; candidate ids stored
    * ``on_no_price``  — winning product exists but price is NULL
    * ``on_error``     — unexpected error (required at publish time)
    """
    from apps.commerce.lookup import lookup_product

    step_id = step.get("id") or ""
    on_found = step.get("on_found") or ""
    on_not_found = step.get("on_not_found") or ""
    on_ambiguous = step.get("on_ambiguous") or ""
    on_no_price = step.get("on_no_price") or ""
    on_error = step.get("on_error") or ""

    # Resolve the query from query_variable (support simple {var} substitution)
    raw_query = step.get("query_variable") or ""
    if raw_query.startswith("{") and raw_query.endswith("}"):
        key = raw_query[1:-1]
        query = str((run.context or {}).get(key, ""))
    else:
        query = raw_query

    try:
        result = lookup_product(run.workflow.account, query)
    except Exception as exc:
        logger.exception("lookup_product step %s failed: %s", step_id, exc)
        if on_error:
            return {"_routed_to": on_error, "reason": "error", "error": str(exc)}
        raise ValueError(f"lookup_product step {step_id!r}: {exc}") from exc

    outcome = result.get("outcome")

    if outcome == "found":
        product = result["product"]
        run.context = {
            **(run.context or {}),
            "product_id": str(product.pk),
            "product_name": product.name,
            "product_price": str(product.price),
            "product_currency": product.currency or "",
        }
        run.save(update_fields=["context"])
        if on_found:
            return {"_routed_to": on_found, "product_id": str(product.pk)}
        return {"product_id": str(product.pk)}

    if outcome == "ambiguous":
        candidates = result.get("candidates") or []
        run.context = {
            **(run.context or {}),
            "candidate_product_ids": [str(p.pk) for p in candidates],
            "candidate_product_names": [p.name for p in candidates],
        }
        run.save(update_fields=["context"])
        if on_ambiguous:
            return {
                "_routed_to": on_ambiguous,
                "reason": "ambiguous",
                "candidates": [str(p.pk) for p in candidates],
            }
        if on_not_found:
            return {"_routed_to": on_not_found, "reason": "ambiguous"}
        if on_error:
            return {"_routed_to": on_error, "reason": "ambiguous"}
        return {"reason": "ambiguous"}

    # not_found — may have sub-reason no_price
    reason = result.get("reason", "no_match")
    if reason == "no_price":
        product = result.get("product")
        if product:
            run.context = {
                **(run.context or {}),
                "product_id": str(product.pk),
                "product_name": product.name,
            }
            run.save(update_fields=["context"])
        if on_no_price:
            return {"_routed_to": on_no_price, "reason": "no_price"}
        if on_error:
            return {"_routed_to": on_error, "reason": "no_price"}
        return {"reason": "no_price"}

    # plain not_found
    if on_not_found:
        return {"_routed_to": on_not_found, "reason": reason}
    if on_error:
        return {"_routed_to": on_error, "reason": reason}
    return {"reason": reason}


def _run_publish_post(run: WorkflowRun, step: dict) -> dict:
    """Publish a ContentPost immediately via the variant orchestrator.

    Step shape:
      {"type": "publish_post", "post_id": "<ContentPost pk>", "on_error": "notify_team"}

    Routes through ``orchestrate_post_publication``, respecting approval checks, per-variant
    outcomes, and asynchronous Instagram processing. Returns per-variant states and preserves
    channel-specific errors.

    On approval failure or post not found, routes to on_error if provided, otherwise raises.
    On orchestration success, stores per-variant outcomes in run.context and returns them.
    On orchestration error (unexpected exception), routes to on_error if provided.
    """
    from apps.publishing.models import ContentPost
    from apps.publishing.services import orchestrate_post_publication

    step_id = step.get("id") or ""
    post_id = step.get("post_id") or ""
    on_error = step.get("on_error") or ""

    try:
        post = ContentPost.objects.get(pk=post_id, account=run.workflow.account)
    except ContentPost.DoesNotExist:
        msg = f"publish_post step {step_id!r}: ContentPost {post_id!r} not found"
        if on_error:
            return {"_routed_to": on_error, "reason": "not_found"}
        raise ValueError(msg) from None

    # Only content that is ready and not already live may be published from a workflow.
    if post.status in (
        ContentPost.Status.IDEA,
        ContentPost.Status.PUBLISHING,
        ContentPost.Status.PUBLISHED,
        ContentPost.Status.CANCELLED,
    ):
        msg = f"publish_post step {step_id!r}: post is {post.status} and cannot be published"
        if on_error:
            return {"_routed_to": on_error, "reason": "invalid_status", "error": msg}
        raise ValueError(msg)

    # Check approval at execution time
    if not post.is_approval_valid():
        msg = f"publish_post step {step_id!r}: approval is invalid or not present"
        if on_error:
            return {"_routed_to": on_error, "reason": "approval_invalid", "error": msg}
        raise ValueError(msg)

    try:
        # A workflow step means "publish now", including channels scheduled for later.
        # Each channel is still claimed under its own lock, so a scheduler dispatch
        # running at the same time cannot publish it twice.
        parent_status, outcomes = orchestrate_post_publication(post, only_due=False)
    except Exception as exc:
        logger.exception(
            "publish_post step %s failed orchestrating post=%s: %s",
            step_id,
            post_id,
            exc,
        )
        if on_error:
            return {"_routed_to": on_error, "reason": "error", "error": str(exc)}
        raise

    # Store per-variant outcomes in context so the workflow can inspect them if needed.
    # outcomes is a list of {"platform": str, "status": str, "publication_id": str, ...}
    post.refresh_from_db(fields=["ig_media_id", "fb_post_id"])
    run.context = {
        **(run.context or {}),
        "publish_outcomes": outcomes,
        "publish_parent_status": parent_status,
        "ig_media_id": post.ig_media_id,
        "fb_post_id": post.fb_post_id,
    }
    run.save(update_fields=["context"])

    # Return aggregated outcome for the step result.
    # If any variant is in PROCESSING (IG video), the publication is not yet complete.
    # The workflow can later check run.context["publish_outcomes"] to see per-channel state.
    return {
        "parent_status": parent_status,
        "outcomes": outcomes,
        "outcome_count": len(outcomes),
        "ig_media_id": post.ig_media_id,
        "fb_post_id": post.fb_post_id,
    }


def _run_ask_question(run: WorkflowRun, step: dict) -> dict:
    """Entry point for the ask_question step.

    Idempotent: if an open interaction already exists for (run, step_id),
    re-park only and correct next_due_at if it drifted.

    Otherwise (first entry): check messaging window, then in one transaction:
      open WorkflowInteraction, send the question, park the run (WAITING + deadline).
    Window closed → route to on_error without opening an interaction.
    """
    from datetime import timedelta

    from django.db import transaction
    from django.utils import timezone

    from apps.automation.models import WorkflowInteraction

    step_id = step.get("id") or ""
    timeout = int(step.get("timeout_seconds", _ASK_DEFAULT_TIMEOUT))
    target = step.get("target", "contact")
    attribute = step.get("attribute") or ""
    question = _first_name_merge(run, step.get("question") or "")

    # ── Idempotent re-entry: open interaction already exists ──────────────────
    existing = WorkflowInteraction.objects.filter(
        run=run, step_id=step_id, status=WorkflowInteraction.Status.OPEN
    ).first()

    if existing is not None:
        # The run was re-entered (worker retry). Re-park: ensure WAITING + correct deadline.
        update_fields = []
        if run.status != WorkflowRun.Status.WAITING:
            run.status = WorkflowRun.Status.WAITING
            update_fields.append("status")
        if existing.deadline_at and run.next_due_at != existing.deadline_at:
            run.next_due_at = existing.deadline_at
            update_fields.append("next_due_at")
        if update_fields:
            run.save(update_fields=update_fields)
        return {"_parked": True, "interaction_token": existing.token}

    # ── Resolve conversation ──────────────────────────────────────────────────
    on_error = step.get("on_error") or ""
    try:
        conversation = _conversation_for_run(run, step, fallback=True)
    except ValueError:
        if on_error:
            return {"_routed_to": on_error, "reason": "no_conversation"}
        raise

    # ── Messaging window check ────────────────────────────────────────────────
    if not _messaging_window_open(conversation):
        if on_error:
            return {"_routed_to": on_error, "reason": "window_closed"}
        raise ValueError(
            f"ask_question step {step_id!r}: messaging window is closed "
            "and no on_error step is configured"
        )

    deadline = timezone.now() + timedelta(seconds=timeout)

    # ── Open interaction + park run (atomic) ──────────────────────────────────
    with transaction.atomic():
        interaction = WorkflowInteraction.objects.create(
            run=run,
            step_id=step_id,
            conversation=conversation,
            expected={"free_text": True, "attribute": attribute, "target": target},
            deadline_at=deadline,
        )
        run.status = WorkflowRun.Status.WAITING
        run.next_due_at = deadline
        run.save(update_fields=["status", "next_due_at"])

    # ── Send the question (after commit, so a crash is recoverable) ───────────
    try:
        _send_ask_question_text(run, conversation, question, step_id, attempt=0)
    except Exception as exc:
        logger.exception(
            "_run_ask_question: send raised for run=%s step=%s", run.pk, step_id
        )
        # Don't wait for an answer to a question the customer never got.
        interaction.status = WorkflowInteraction.Status.CANCELLED
        interaction.save(update_fields=["status"])
        run.status = WorkflowRun.Status.ACTIVE
        run.next_due_at = None
        run.save(update_fields=["status", "next_due_at"])
        if on_error:
            return {"_routed_to": on_error, "reason": "send_failed", "error": str(exc)}
        raise ValueError(
            f"ask_question step {step_id!r}: the question couldn't be sent ({exc})"
        ) from exc

    return {"_parked": True, "interaction_token": interaction.token}


def _send_ask_question_text(
    run: WorkflowRun, conversation, text: str, step_id: str, *, attempt: int
) -> None:
    """Send the question (or re-prompt) as a plain-text reply in the conversation.

    Uses the ``reply`` action so window/opt-out/consent rules apply.
    The idempotency key is unique per step + attempt so a re-prompt after
    an invalid answer gets a fresh send, but a worker retry of the same
    attempt is deduplicated.
    """
    from apps.core.actions import ActionError, run_action

    idem_key = f"{send_key(run, step_id)}:aq:{attempt}"
    try:
        run_action(
            "reply",
            {"account": run.workflow.account},
            conversation=conversation,
            body=text,
            idempotency_key=idem_key,
        )
    except ActionError as exc:
        raise ValueError(
            f"ask_question step {step_id!r}: could not send question: {exc}"
        ) from exc


def _route_for_reply(step: dict, message: dict) -> str | None:
    """Which step a customer's answer leads to, or None if this step has no place for it.

    A tapped button matches its id (case-insensitively). A typed answer matches a choice
    written the same way (ignoring case and punctuation), so "prices" works for a choice
    named ``prices``. Anything else goes to ``default``, if the step has one.
    """
    from apps.automation import keywords

    routes = {str(k).casefold(): v for k, v in (step.get("routes") or {}).items()}
    reply_id = (message.get("reply_id") or "").casefold()
    if reply_id and reply_id in routes:
        return routes[reply_id]
    typed = keywords.normalize(message.get("body") or "")
    for key, target in (step.get("routes") or {}).items():
        if typed and typed == keywords.normalize(str(key)):
            return target
    return step.get("default") or None


def resume_on_reply(account_id: int, contact, message: dict) -> bool:
    """Continue a run that is waiting for this customer's answer. True if one took it.

    For runs that have a ``WorkflowInteraction`` row (post-A0), routing is handled
    by ``claim_reply`` from ``apps.automation.interaction``.  For legacy WAITING
    runs that predate the migration (no interaction rows at all), the original
    scan-and-lock loop is used as a fallback, but ordered deterministically by
    ``(started_at, pk)`` so the result is repeatable under concurrency.
    """

    # Try interaction-based routing first (A0 path).
    # We need the conversation to call claim_reply. The message dict carries it
    # if this came from an inbound handler that already resolved it.
    conversation = message.get("_conversation")  # set by inbound handlers post-A0
    if conversation is not None:
        from apps.automation.interaction import claim_reply

        message_key = message.get("_message_key", "")
        reply_id = message.get("reply_id", "")
        body = message.get("body", "")

        # Check whether any open interaction exists for this conversation — if so,
        # use the interaction path exclusively.
        if WorkflowInteraction.objects.filter(
            conversation=conversation,
            status=WorkflowInteraction.Status.OPEN,
        ).exists():
            result = claim_reply(conversation, message_key, reply_id, body)
            if result in ("claimed", "already_claimed"):
                return True
            # "unmatched" / "stale_run" / "wrong_conversation": fall through to the
            # wait_for_reply legacy loop — wait_for_reply steps never create interactions,
            # so a contact with one open IG interaction and one parked wait_for_reply run
            # must still be able to resume the wait_for_reply run via typed text.

    # ── Legacy fallback (pre-migration WAITING runs, no interaction rows) ────────
    # Only used when the contact's WAITING runs have NO WorkflowInteraction rows at
    # all (created before the A0 migration).  Once all such runs have timed out or
    # completed, this branch becomes unreachable.
    waiting = (
        WorkflowRun.objects.filter(
            contact=contact,
            workflow__account_id=account_id,
            status=WorkflowRun.Status.WAITING,
            next_due_at__isnull=False,
        )
        .exclude(  # skip runs whose current step already has an open A0 interaction
            interactions__status="open",
        )
        .order_by("started_at", "pk")  # deterministic under concurrency
        .select_related("workflow")
    )
    for run in waiting:
        step = _run_steps(run).get(run.current_step)
        if step is None or step.get("type") != "wait_for_reply":
            continue
        target = _route_for_reply(step, message)
        if not target:
            continue
        with transaction.atomic():
            run = (
                WorkflowRun.objects.select_for_update()
                .select_related("workflow")
                .get(pk=run.pk)
            )
            if (
                run.status != WorkflowRun.Status.WAITING
                or run.current_step != step["id"]
            ):
                continue  # another worker already handled it
            reply = {
                "id": message.get("reply_id", ""),
                "title": message.get("reply_title", ""),
                "text": message.get("body", ""),
            }
            _record(run, step, {"reply": reply, "went_to": target})
            run.context = {**(run.context or {}), "reply": reply}
            run.status = WorkflowRun.Status.ACTIVE
            run.next_due_at = None
            run.current_step = target
            run.save(update_fields=["context", "status", "next_due_at", "current_step"])
        advance_run(run)
        return True
    return False


def _run_reply_text(run: WorkflowRun, step: dict) -> dict:
    """Send free text into the conversation that started this run.

    Goes through the ``reply`` action, so the WhatsApp 24h window, opt-out and consent
    checks apply exactly as they do for a human reply in the inbox. A message trigger is
    always inside the window, which is why this needs no template.
    """
    from apps.automation.template_render import TemplateRenderError, render_template
    from apps.core.actions import ActionError, run_action

    conversation = _conversation_for_run(run, step)
    if not _window_open_for_run(run, conversation):
        fallback = step.get("fallback_template") or ""
        if fallback and conversation.channel == conversation.Channel.WHATSAPP:
            # Outside the window an approved template is the only way through.
            return {
                **_run_send_whatsapp(
                    run,
                    {
                        **step,
                        "type": "send_whatsapp",
                        "template": fallback,
                        "variable_mapping": step.get("fallback_variable_mapping") or {},
                        "variable_fallbacks": step.get("fallback_variable_fallbacks")
                        or {},
                    },
                ),
                "fallback_template": fallback,
            }
        _require_open_window(run, step, conversation)
    text = _first_name_merge(run, step.get("text") or "")

    # Apply allowlist template rendering if the text has {placeholders}.
    if "{" in text:
        on_error = step.get("on_error") or ""
        ctx = run.context or {}
        contact = run.contact
        account = run.workflow.account
        render_ctx = {
            "contact.first_name": getattr(contact, "first_name", "") or "",
            "contact.last_name": getattr(contact, "last_name", "") or "",
            "contact.phone": getattr(contact, "phone", "") or "",
            "lead.status": ctx.get("lead_status", ""),
            "product.name": ctx.get("product_name", ""),
            "product.price": ctx.get("product_price", ""),
            "product.currency": ctx.get("product_currency", ""),
            "business.name": getattr(account, "company_name", "") or "",
            # The Meta ad the chat came from (``trigger.from_ad`` workflows).
            "ad.headline": (ctx.get("ad") or {}).get("headline", "") or "",
            # Answers from a form this run sent (start_form): {form_answers.budget}.
            **{
                f"form_answers.{key}": str(value)
                for key, value in (ctx.get("form_answers") or {}).items()
            },
        }
        try:
            text = render_template(text, render_ctx, step_id=step.get("id") or "")
        except TemplateRenderError as exc:
            if on_error:
                return {
                    "_routed_to": on_error,
                    "reason": "template_error",
                    "error": str(exc),
                }
            raise ValueError(str(exc)) from exc

    try:
        # A fixed key per run and step: a retried step returns the first message instead of sending
        # a second, and the inbox can tell this reply came from an automation, not a person.
        return run_action(
            "reply",
            {"account": run.workflow.account},
            conversation=conversation,
            body=text,
            idempotency_key=send_key(run, step.get("id")),
        )
    except ActionError as exc:
        raise ValueError(f"reply_text step {step.get('id')!r}: {exc}") from exc


def _lead_for_run(run: WorkflowRun, step: dict):
    """The lead this run is about: the one in its context, else the customer's open lead."""
    from apps.crm.models import Lead

    account = run.workflow.account
    lead = None
    if (run.context or {}).get("lead_id"):
        lead = Lead.objects.filter(
            account=account, public_id=run.context["lead_id"]
        ).first()
    if lead is None:
        lead = (
            Lead.objects.filter(
                account=account,
                contact=run.contact,
                status__in=[
                    Lead.Status.NEW,
                    Lead.Status.CONTACTED,
                    Lead.Status.QUALIFIED,
                ],
            )
            .order_by("-created_at")
            .first()
        )
    if lead is None:
        raise ValueError(
            f"{step['type']} step {step.get('id')!r}: this customer has no open lead"
        )
    return lead


def _run_create_lead(run: WorkflowRun, step: dict) -> dict:
    from apps.core.actions import ActionError, run_action

    account = run.workflow.account
    try:
        result = run_action(
            "create_lead",
            {"account": account},
            account=account,
            contact=run.contact,
            source=(step.get("source") or "automation")[:50],
            workflow_run=run,
            conversation_id=(run.context or {}).get("conversation_id") or "",
        )
    except ActionError as exc:
        raise ValueError(f"create_lead step {step.get('id')!r}: {exc}") from exc
    # Later steps in this run ("mark qualified") act on this lead.
    run.context = {**(run.context or {}), "lead_id": result["lead_id"]}
    run.save(update_fields=["context"])
    return {"lead_id": result["lead_id"]}


def _run_update_lead_status(run: WorkflowRun, step: dict) -> dict:
    from apps.core.actions import ActionError, run_action

    lead = _lead_for_run(run, step)
    try:
        return run_action(
            "update_lead_status",
            {"account": run.workflow.account},
            lead=lead,
            status=step.get("status"),
        )
    except ActionError as exc:
        raise ValueError(f"update_lead_status step {step.get('id')!r}: {exc}") from exc


def _run_assign_conversation(run: WorkflowRun, step: dict) -> dict:
    from apps.core.actions import ActionError, run_action

    conversation = _conversation_for_run(run, step, fallback=True)
    try:
        return run_action(
            "auto_assign_conversation",
            {"account": run.workflow.account},
            conversation=conversation,
            email=step.get("to") or "",
        )
    except ActionError as exc:
        raise ValueError(f"assign_conversation step {step.get('id')!r}: {exc}") from exc


def _run_start_form(run: WorkflowRun, step: dict) -> dict:
    """Send a form and park the run until the customer finishes it or it times out.

    The response id is kept on the run, so ``apps.automation.api.form_finished`` can carry
    on from exactly this run when the answers arrive.
    """
    from apps.conversations import forms as conversation_forms
    from apps.conversations.models import ConversationForm

    on_error = step.get("on_error") or ""
    form = ConversationForm.objects.filter(
        account=run.workflow.account, public_id=step.get("form") or ""
    ).first()
    try:
        if form is None:
            raise ValueError("the form it sends no longer exists")
        conversation = _conversation_for_run(run, step, fallback=True)
        if not conversation_forms.can_start(form, conversation):
            raise ValueError(
                f"the form “{form.name}” isn't ready to send (publish it first)"
            )
        if conversation_forms.active_response_for(conversation) is not None:
            raise ValueError("the customer is already filling in a form")
        _require_open_window(run, step, conversation)
        response = conversation_forms.start_form(conversation, form)
    except (ValueError, conversation_forms.FormError) as exc:
        if on_error:
            return {
                "_routed_to": on_error,
                "reason": "form_not_sent",
                "error": str(exc),
            }
        raise ValueError(f"start_form step {step.get('id')!r}: {exc}") from exc
    hours = float(step.get("timeout_hours") or form.timeout_hours or 72)
    run.context = {
        **(run.context or {}),
        "_form_responses": {
            **((run.context or {}).get("_form_responses") or {}),
            step["id"]: response.pk,
        },
    }
    run.status = WorkflowRun.Status.WAITING
    run.next_due_at = timezone.now() + timedelta(hours=hours)
    run.save(update_fields=["context", "status", "next_due_at"])
    return {"_parked": True, "response_id": response.pk, "form": form.public_id}


def _abandon_run_form(run: WorkflowRun, step: dict) -> None:
    """The run stopped waiting for its form: let the conversation go too."""
    from apps.conversations.models import FormResponse

    response_id = ((run.context or {}).get("_form_responses") or {}).get(step["id"])
    if response_id:
        FormResponse.objects.filter(
            pk=response_id, status=FormResponse.Status.IN_PROGRESS
        ).update(status=FormResponse.Status.ABANDONED, completed_at=timezone.now())


def _run_create_followup(run: WorkflowRun, step: dict) -> dict:
    """Put a reminder on the team's follow-up list about this customer."""
    from datetime import timedelta

    from apps.accounts import notifications
    from apps.conversations import followups

    try:
        conversation = _conversation_for_run(run, step, fallback=True)
    except ValueError:
        conversation = None
    contact = run.contact
    who = contact.full_name or contact.phone or contact.email or "the customer"
    note = _first_name_merge(run, step.get("note") or "").replace("{contact}", who)
    assigned_to = None
    to = (step.get("assign_to") or "").strip()
    if to == "assignee":
        assigned_to = getattr(conversation, "assigned_to", None)
    elif "@" in to:
        try:
            assigned_to = notifications.recipients(run.workflow.account, to)[0]
        except notifications.NotifyError as exc:
            raise ValueError(
                f"create_followup step {step.get('id')!r}: {exc} No follow-up was set."
            ) from exc
    hours = float(step.get("due_in_hours") or 24)
    followup, created = followups.create(
        run.workflow.account,
        contact,
        conversation=conversation,
        due_at=timezone.now() + timedelta(hours=hours),
        note=note or "Follow up",
        source="workflow",
        assigned_to=assigned_to,
        for_team=not to,  # "Whole team" in the editor: nobody in particular
    )
    return {"followup_id": followup.pk, "created": created}


def _run_notify_team(run: WorkflowRun, step: dict) -> dict:
    from django.urls import reverse

    from apps.accounts import notifications

    try:
        conversation = _conversation_for_run(run, step, fallback=True)
    except ValueError:
        conversation = None  # a notification about a customer needs no conversation
    contact = run.contact
    who = contact.full_name or contact.phone or contact.email or "A customer"
    text = _first_name_merge(run, step.get("text") or "").replace("{contact}", who)
    path = (
        reverse("conversations:detail", args=[conversation.public_id])
        if conversation is not None
        else reverse("contacts:detail", args=[contact.public_id])
    )
    try:
        sent = notifications.notify_team(
            run.workflow.account,
            to=step.get("to") or "owners",
            subject=(step.get("subject") or f"Akilent: {who}")[:200],
            text=text,
            path=path,
            conversation=conversation,
        )
    except notifications.NotifyError as exc:
        raise ValueError(f"notify_team step {step.get('id')!r}: {exc}") from exc
    return {"notified": sent}


def _run_tag_step(run: WorkflowRun, step: dict) -> dict:
    from apps.contacts import tags as contact_tags

    change = (
        contact_tags.add_tag if step["type"] == "add_tag" else contact_tags.remove_tag
    )
    try:
        changed = change(run.contact, step.get("tag") or "")
    except contact_tags.TagError as exc:
        raise ValueError(f"{step['type']} step {step.get('id')!r}: {exc}") from exc
    return {"tag": step.get("tag"), "changed": changed}


def _resolve_whatsapp_template(account, name):
    if not name:
        return None
    from apps.whatsapp.models import MessageTemplate

    return MessageTemplate.objects.filter(
        account=account, whatsapp_template_name=name
    ).first()


def _resolve_variable_mapping(
    variables: list,
    mapping: dict,
    run: WorkflowRun,
    contact,
    fallbacks: dict | None = None,
) -> dict:
    """Build the WhatsApp template ``params`` dict from an explicit, allow-listed mapping.

    See ``apps.automation.variables`` for what a mapping entry can be. Only variable names the
    template declares are resolved, and only the keys a mapping names are read, so nothing
    outside it can reach the outbound message. A blank with no value and no fallback fails the
    step with the blank's name rather than sending an empty value.
    """
    from apps.automation import variables as wa_variables

    try:
        return wa_variables.resolve(
            variables,
            mapping,
            contact=contact,
            account=run.workflow.account,
            context=run.context or {},
            fallbacks=fallbacks,
        )
    except wa_variables.MissingValue as exc:
        raise ValueError(f"send_whatsapp: {exc}") from exc


def _run_webhook(run: WorkflowRun, step: dict) -> dict:
    """Queue a Workflow ``webhook`` step's HTTP POST for async delivery.

    Like ``_run_send_email``/``_run_send_whatsapp``, this step is marked
    ``ok`` once the delivery is queued, not once it's actually delivered —
    the network call (with SSRF guard, retries) happens in
    ``apps.automation.tasks.deliver_workflow_webhook``.
    """
    from apps.automation.models import WorkflowWebhookDelivery
    from apps.automation.tasks import deliver_workflow_webhook

    delivery = WorkflowWebhookDelivery.objects.create(
        run=run,
        step_id=step["id"],
        url=step["url"],
        method=(step.get("method") or "POST").upper(),
        headers=step.get("headers") or {},
        body={**(step.get("body") or {}), **(run.context or {})},
    )
    deliver_workflow_webhook.delay(delivery.id)
    return {"delivery_id": delivery.id}


# Kwarg name -> dotted path of the model to resolve it against, by
# ``public_id``, when a step's params supply ``"<name>_id"`` instead of the
# object directly. Extend this as new object-typed action kwargs appear —
# it's the only place workflow_engine needs to know about them.
_ACTION_OBJECT_RESOLVERS = {
    "conversation": "apps.conversations.models.Conversation",
    "lead": "apps.crm.models.Lead",
    "deal": "apps.crm.models.Deal",
    "order": "apps.commerce.models.Order",
}


def _resolve_action_param(source, run: WorkflowRun):
    """Same "contact."/"context."/literal convention as
    ``_resolve_variable_mapping``, generalized for action-step params."""
    contact_attrs = run.contact.attributes or {}
    context = run.context or {}
    if isinstance(source, str) and source.startswith("contact."):
        return _dig(contact_attrs, source[len("contact.") :])
    if isinstance(source, str) and source.startswith("context."):
        return _dig(context, source[len("context.") :])
    return source


def _dig(data, path: str):
    """``a.b.c`` into nested dicts ("reply.title" in a run's context); None if any step is missing."""
    for part in path.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _resolve_action_object(field_name: str, public_id: str, run: WorkflowRun):
    import importlib

    dotted = _ACTION_OBJECT_RESOLVERS[field_name]
    module_path, class_name = dotted.rsplit(".", 1)
    model = getattr(importlib.import_module(module_path), class_name)
    return model.objects.get(account=run.workflow.account, public_id=public_id)


def _build_action_kwargs(run: WorkflowRun, step: dict) -> dict:
    from apps.core.actions import get_action

    action = get_action(step["action"])
    schema = action.input_schema()

    params = step.get("params") or {}
    kwargs: dict[str, Any] = {}

    for field_name in [*schema.get("required", []), *schema.get("optional", [])]:
        if field_name == "contact":
            kwargs["contact"] = run.contact
            continue
        if field_name == "account":
            kwargs["account"] = run.workflow.account
            continue
        if field_name in _ACTION_OBJECT_RESOLVERS and field_name not in params:
            id_source = params.get(f"{field_name}_id")
            if id_source is not None:
                public_id = _resolve_action_param(id_source, run)
                if public_id:
                    kwargs[field_name] = _resolve_action_object(
                        field_name, public_id, run
                    )
            continue
        if field_name in params:
            kwargs[field_name] = _resolve_action_param(params[field_name], run)

    return kwargs


def _run_action_step(run: WorkflowRun, step: dict) -> dict:
    """Execute a Phase 4 ``action`` step through the shared Action Registry.

    Auto-injects ``contact`` (``run.contact``) and ``account``
    (``run.workflow.account``) — a step never needs to name those in
    ``params``. Other object-typed kwargs (``conversation``, ``lead``,
    ``deal``, ``order``) are resolved by ``public_id`` from ``"<name>_id"``.
    Scalar kwargs use the same ``"contact.<attr>"`` / ``"context.<key>"`` /
    literal convention as ``send_whatsapp``'s ``variable_mapping``.
    """
    from apps.core.actions import ActionError, run_action

    kwargs = _build_action_kwargs(run, step)
    try:
        return run_action(step["action"], {"account": run.workflow.account}, **kwargs)
    except ActionError as exc:
        # advance_run's caller treats any exception from a step as a failed
        # run — this just gives it a clean message instead of leaking
        # ActionError's type across the module boundary.
        raise ValueError(
            f"action step {step.get('id')!r} ({step['action']}): {exc}"
        ) from exc


def _apply_set_attribute(run: WorkflowRun, step: dict) -> dict:
    contact = run.contact
    # A value like "context.reply.title" is read from the run (what the customer tapped);
    # anything else is stored as written.
    value = _resolve_action_param(step.get("value"), run)
    key = step["key"]

    from apps.contacts.attributes import set_attributes
    from apps.contacts.models import CustomAttributeDef

    # Compatibility path: if no CustomAttributeDef exists for this key on the
    # contact entity, fall back to the legacy direct-merge behavior and emit a
    # deprecation warning. Lead/deal steps always require a definition.
    step_entity = step.get("target", "contact")
    has_def = CustomAttributeDef.objects.filter(
        account=contact.account, entity=step_entity, key=key
    ).exists()

    if has_def:
        set_attributes(contact, {key: value}, source="workflow")
    else:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "_apply_set_attribute: no CustomAttributeDef for key=%r entity=%s — "
            "falling back to legacy direct write; create a definition to enforce typing",
            key,
            step_entity,
        )
        contact.attributes = {**(contact.attributes or {}), key: value}
        contact.save(update_fields=["attributes", "updated_at"])

    return {"key": key}


# Marks a run parked by a conversation's "teammate took over" pause, so turning the pause
# off early can wake it (``Conversation.resume_automation``).
HELD_BY_PAUSE = "held_by_pause"


def _held(run: WorkflowRun) -> bool:
    """A paused workflow, or a conversation a teammate took over: the customer keeps their
    place and nothing is sent. Returns True when the run was held.

    Every path that moves a run (a button tap, a finished form, a timer) ends up here, so
    this is the one place the holds are kept. The run sweepers in ``run_due`` pick a held
    run back up afterwards: they only look at published workflows, and a conversation
    hold is parked until the pause ends.
    """
    from apps.conversations.models import Conversation

    now = timezone.now()
    until = None
    conversation_hold = False
    status = (
        Workflow.objects.filter(pk=run.workflow_id)
        .values_list("status", flat=True)
        .first()
    )
    if status == Workflow.Status.PAUSED:
        until = now
    elif conversation_id := (run.context or {}).get("conversation_id"):
        paused_until = (
            Conversation.objects.filter(public_id=conversation_id)
            .values_list("automation_paused_until", flat=True)
            .first()
        )
        if paused_until and paused_until > now:
            until, conversation_hold = paused_until, True
    if until is None:
        if (run.context or {}).pop(HELD_BY_PAUSE, None):
            run.save(update_fields=["context"])
        return False
    fields = []
    if run.next_due_at is None or run.next_due_at < until:
        run.next_due_at = until
        fields.append("next_due_at")
    # Marked only when the hold moved its timer: a longer wait of its own stays as it is.
    if conversation_hold and fields and not (run.context or {}).get(HELD_BY_PAUSE):
        run.context = {**(run.context or {}), HELD_BY_PAUSE: True}
        fields.append("context")
    if fields:
        run.save(update_fields=fields)
    return True


def advance_run(run: WorkflowRun) -> WorkflowRun:
    """Execute steps until the run waits, stops, or completes."""
    if run.status not in (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING):
        return run
    if _held(run):
        return run

    steps = _run_steps(run)

    for _ in range(_MAX_STEPS_PER_CALL):
        step = steps.get(run.current_step)
        if step is None:
            return _complete(run)

        stype = step.get("type")

        if stype == "wait":
            if (
                run.status == WorkflowRun.Status.WAITING
                and run.next_due_at
                and run.next_due_at <= timezone.now()
            ):
                # resuming from the wait
                _record(run, step, {"resumed": True})
                run.status = WorkflowRun.Status.ACTIVE
                run.next_due_at = None
                run.current_step = step.get("next", "")
                run.save(update_fields=["status", "next_due_at", "current_step"])
                continue
            run.status = WorkflowRun.Status.WAITING
            run.next_due_at = timezone.now() + timedelta(
                seconds=int(step.get("seconds", 0))
            )
            run.save(update_fields=["status", "next_due_at"])
            return run

        if stype == "wait_for_reply":
            if (
                run.status == WorkflowRun.Status.WAITING
                and run.next_due_at
                and run.next_due_at <= timezone.now()
            ):
                # Nobody answered in time: take the timeout path (or simply end).
                _record(run, step, {"timed_out": True})
                run.status = WorkflowRun.Status.ACTIVE
                run.next_due_at = None
                run.current_step = step.get("on_timeout") or ""
                run.save(update_fields=["status", "next_due_at", "current_step"])
                if not run.current_step:
                    return _complete(run)
                continue
            timeout = int(step.get("timeout_seconds", DEFAULT_REPLY_TIMEOUT_SECONDS))
            run.status = WorkflowRun.Status.WAITING
            run.next_due_at = timezone.now() + timedelta(seconds=timeout)
            run.save(update_fields=["status", "next_due_at"])
            return run

        # send_buttons / ask_question park the run (WAITING) with a deadline. On re-entry,
        # if the deadline has passed, take the timeout path and cancel interactions.
        if (
            stype in ("send_buttons", "send_list", "ask_question", "start_form")
            and run.status == WorkflowRun.Status.WAITING
            and run.next_due_at
            and run.next_due_at <= timezone.now()
        ):
            from apps.automation.interaction import cancel_open_interactions_for_run

            cancel_open_interactions_for_run(run)
            if stype == "start_form":
                _abandon_run_form(run, step)
            _record(run, step, {"timed_out": True})
            run.status = WorkflowRun.Status.ACTIVE
            run.next_due_at = None
            # Inherit on_timeout from the following wait_for_reply when not set here
            # (WA workflows that pair send_buttons with a separate wait_for_reply step).
            _on_timeout = step.get("on_timeout") or ""
            if not _on_timeout:
                _nxt = _run_steps(run).get(step.get("next") or "")
                if _nxt and _nxt.get("type") == "wait_for_reply":
                    _on_timeout = _nxt.get("on_timeout") or ""
            run.current_step = _on_timeout
            run.save(update_fields=["status", "next_due_at", "current_step"])
            if not run.current_step:
                return _complete(run)
            continue

        # Non-wait step: skip if already executed (idempotent re-entry).
        if WorkflowStepRun.objects.filter(run=run, step_id=step["id"]).exists():
            nxt = _next_after(step, run)
            if nxt == run.current_step:
                return _complete(run)
            run.current_step = nxt
            run.save(update_fields=["current_step"])
            continue

        try:
            if stype == "send_email":
                result = _run_send_email(run, step)
            elif stype == "send_whatsapp":
                result = _run_send_whatsapp(run, step)
            elif stype == "webhook":
                result = _run_webhook(run, step)
            elif stype == "action":
                result = _run_action_step(run, step)
            elif stype == "reply_text":
                result = _run_reply_text(run, step)
            elif stype in ("add_tag", "remove_tag"):
                result = _run_tag_step(run, step)
            elif stype in ("send_buttons", "send_list"):
                result = _run_interactive(run, step)
            elif stype == "create_lead":
                result = _run_create_lead(run, step)
            elif stype == "update_lead_status":
                result = _run_update_lead_status(run, step)
            elif stype == "assign_conversation":
                result = _run_assign_conversation(run, step)
            elif stype == "notify_team":
                result = _run_notify_team(run, step)
            elif stype == "create_followup":
                result = _run_create_followup(run, step)
            elif stype == "start_form":
                result = _run_start_form(run, step)
            elif stype in ("ai_classify", "ai_extract", "ai_answer"):
                from apps.automation import ai_steps

                result = {
                    "ai_classify": ai_steps.run_classify,
                    "ai_extract": ai_steps.run_extract,
                    "ai_answer": ai_steps.run_answer,
                }[stype](run, step)
            elif stype == "branch":
                matched = _condition_matches(run.contact, step)
                result = {"matched": matched}
            elif stype == "set_attribute":
                result = _apply_set_attribute(run, step)
            elif stype == "ask_question":
                result = _run_ask_question(run, step)
            elif stype == "lookup_product":
                result = _run_lookup_product(run, step)
            elif stype == "publish_post":
                result = _run_publish_post(run, step)
            elif stype in ("stop", "exit"):
                _record(run, step, {})
                return _complete(run)
            else:
                raise ValueError(f"unsupported workflow step type {stype!r}")
        except ChannelStateUnknownError:
            # Settings can't be read right now. The step wasn't recorded, so the run waits and
            # runs this step again instead of failing (and alerting) over a transient read error.
            run.status = WorkflowRun.Status.WAITING
            run.next_due_at = timezone.now() + timedelta(minutes=5)
            run.save(update_fields=["status", "next_due_at"])
            logger.warning("workflow run %s: channel state unknown, retrying", run.pk)
            return run
        except Exception as exc:
            logger.exception("workflow run %s step %s failed", run.pk, step.get("id"))
            _record(run, step, {"error": str(exc)}, status="error")
            run.status = WorkflowRun.Status.FAILED
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "completed_at"])
            from apps.automation.interaction import cancel_open_interactions_for_run

            cancel_open_interactions_for_run(run)
            _alert_failure(run, str(exc))
            return run

        _record(
            run,
            step,
            result,
            status="skipped" if result.get("skipped") else "ok",
            ig_outbound_message_id=result.get("ig_outbound_message_id"),
            fb_outbound_message_id=result.get("fb_outbound_message_id"),
        )
        # Interactive and ask_question steps park the run (WAITING + deadline).
        if result.get("_parked"):
            return run
        # A step chose its own way on (ask_question's on_error, an AI step's label or its
        # without-AI path...). An empty choice means the automation ends here.
        if "_routed_to" in result:
            run.current_step = result["_routed_to"] or ""
            run.save(update_fields=["current_step"])
            if not run.current_step:
                return _complete(run)
            continue
        run.current_step = _next_after(step, run, branch_result=result)
        run.save(update_fields=["current_step"])
        if not run.current_step:
            return _complete(run)

    if WorkflowStepRun.objects.filter(run=run, step_id=run.current_step).exists():
        # Back at a step it already ran: a loop (an older definition, or one edited
        # through the Code tab) that would otherwise sit "active" forever and block this
        # customer from the automation. Fail it so the owner hears about it.
        logger.warning(
            "workflow run %s looped at %s; failing", run.pk, run.current_step
        )
        error = f"The automation looped back to step {run.current_step!r}, so it was stopped."
        run.status = WorkflowRun.Status.FAILED
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])
        from apps.automation.interaction import cancel_open_interactions_for_run

        cancel_open_interactions_for_run(run)
        _alert_failure(run, error)
        return run
    # A long but legitimate chain: let the sweeper (run_due) carry on from here shortly,
    # instead of parking with no due time where nothing would ever pick it up.
    logger.info("workflow run %s hit step limit; continuing on next sweep", run.pk)
    run.next_due_at = timezone.now()
    run.save(update_fields=["next_due_at"])
    return run


def _next_after(
    step: dict, run: WorkflowRun, *, branch_result: dict | None = None
) -> str:
    if step.get("type") == "branch":
        if branch_result is None:
            branch_result = {"matched": _condition_matches(run.contact, step)}
        return step["on_true"] if branch_result.get("matched") else step["on_false"]
    return step.get("next", "")


def _record(
    run: WorkflowRun,
    step: dict,
    result: dict,
    *,
    status: str = "ok",
    ig_outbound_message_id: int | None = None,
    fb_outbound_message_id: int | None = None,
) -> None:
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():  # savepoint so IntegrityError doesn't break outer tx
            WorkflowStepRun.objects.create(
                run=run,
                step_id=step["id"],
                step_type=step.get("type", ""),
                status=status,
                result=result,
                # WA outbound FK (legacy key kept for reconciliation hook + existing readers).
                outbound_message_id=result.get("outbound_message_id"),
                # IG outbound FK (A workstream).
                ig_outbound_message_id=ig_outbound_message_id,
                fb_outbound_message_id=fb_outbound_message_id,
            )
    except IntegrityError:
        # uniq_workflowsteprun_run_step — a concurrent advance already recorded
        # this step. The .exists() guard in advance_run is best-effort; this
        # constraint is the real backstop against a double-send.
        logger.info("workflow run %s step %s already recorded", run.pk, step["id"])


def _alert_failure(run: WorkflowRun, error: str) -> None:
    """Email the owners when an automation fails, at most once a day per automation.

    A failure nobody hears about is the worst kind: the owner thinks it is working. The daily
    limit stops one broken automation from flooding an inbox. Best-effort: alerting must never
    turn a recorded failure into a crash.
    """
    try:
        from django.core.cache import cache
        from django.urls import reverse

        from apps.accounts import notifications
        from apps.automation import explain

        if not cache.add(f"automation-failure-alert:{run.workflow_id}", 1, 24 * 3600):
            return
        contact = run.contact
        who = contact.full_name or contact.phone or contact.email or "a customer"
        notifications.notify_team(
            run.workflow.account,
            to="owners",
            subject=f"Akilent: {run.workflow.name} couldn't send",
            text=(
                f"Your automation {run.workflow.name} couldn't finish for {who}.\n\n"
                f"{explain.humanise_step_error(error)}\n\n"
                "You'll get at most one of these emails a day for each automation."
            ),
            path=reverse("automation:run", kwargs={"run_id": run.public_id}),
        )
    except Exception:
        logger.exception("could not send the failure alert for run %s", run.pk)


def _complete(run: WorkflowRun) -> WorkflowRun:
    from apps.automation.interaction import cancel_open_interactions_for_run

    with transaction.atomic():
        run.status = WorkflowRun.Status.COMPLETED
        run.completed_at = timezone.now()
        run.next_due_at = None
        run.save(update_fields=["status", "completed_at", "next_due_at"])
        cancel_open_interactions_for_run(run)
    _notify_workflow(run, "workflow.completed")
    return run


# ── Trigger evaluation ───────────────────────────────────────────────────────


def on_business_event(event, **kwargs) -> None:
    """Enroll the event's contact into workflows triggered by this event name."""
    if event.contact_id is None:
        return
    from apps.contacts.models import Contact

    contact = Contact.objects.filter(
        pk=event.contact_id, account_id=event.account_id
    ).first()
    if contact is None:
        return  # never enroll another business's customer
    workflows = Workflow.objects.filter(
        account_id=event.account_id, status=Workflow.Status.PUBLISHED
    )
    for wf in workflows:
        trig = (wf.definition or {}).get("trigger", {})
        if trig.get("type") == "business_event" and trig.get("name") == event.name:
            try:
                with transaction.atomic():
                    enroll(wf, contact, context={"event": event.data})
            except Exception:
                logger.exception("on_business_event: enroll failed wf=%s", wf.pk)


def enroll_for_trigger(
    account_id: int,
    trigger_type: str,
    contact,
    *,
    context: dict | None = None,
    subject_key: str = "",
) -> int:
    """Enroll ``contact`` into every published workflow whose trigger matches.

    Used for the lightweight contact-lifecycle triggers (``contact.created``,
    ``email.opened``, …). ``business_event`` has its own richer path in
    ``on_business_event``.
    """
    return len(
        _enroll_matching(
            account_id, trigger_type, contact, context=context, subject_key=subject_key
        )
    )


# Steps that put a message in front of the customer.
_CUSTOMER_REPLY_STEPS = frozenset(
    {"reply_text", "send_whatsapp", "send_buttons", "send_list"}
)


def answer_message(account_id: int, contact, *, context: dict) -> bool:
    """Enroll ``contact`` into the message-received workflows; True only if one answered.

    "Answered" means a run sent the customer something, or is now waiting for their
    reply. A run that started but ended without a word (e.g. a welcome workflow for
    someone already welcomed) leaves the message for AI.
    """
    runs = _enroll_matching(
        account_id,
        "conversation.message_received",
        contact,
        context=context,
        single_answer=True,
    )
    return any(_answered(run, since=run._enrolled_at) for run in runs)  # type: ignore[attr-defined]


def _answered(run: WorkflowRun, *, since) -> bool:
    """Whether ``run`` replied to the customer (or is now waiting on them) since ``since``.

    Only what this message caused counts: a run parked on a plain ``wait`` (a check-in due
    in three days) is returned again for every later message, and its earlier sends must
    not keep AI quiet for those days.
    """
    if waiting_on_customer(run):
        return True
    from django.db.models import Q

    return WorkflowStepRun.objects.filter(
        Q(step_type__in=_CUSTOMER_REPLY_STEPS)
        | Q(step_type="ai_answer", result__has_key="answered"),
        run=run,
        status="ok",
        executed_at__gte=since,
    ).exists()


# Steps that put words in front of the customer: a workflow with any of them "talks".
_TALKING_STEPS = _CUSTOMER_REPLY_STEPS | {"ask_question", "start_form", "ai_answer"}


def _talks(workflow: Workflow) -> bool:
    return any(
        s.get("type") in _TALKING_STEPS
        for s in (workflow.definition or {}).get("steps", [])
    )


# Steps that park a run until the customer answers; any other WAITING run is on a timer.
_REPLY_WAIT_STEPS = frozenset(
    {"wait_for_reply", "send_buttons", "send_list", "ask_question", "start_form"}
)


def waiting_on_customer(run: WorkflowRun) -> bool:
    """True when ``run`` is parked until the customer replies (not merely on a timer)."""
    if run.status != WorkflowRun.Status.WAITING:
        return False
    step = _run_steps(run).get(run.current_step) or {}
    return step.get("type") in _REPLY_WAIT_STEPS


def _enroll_matching(
    account_id: int,
    trigger_type: str,
    contact,
    *,
    context: dict | None = None,
    subject_key: str = "",
    single_answer: bool = False,
) -> list[WorkflowRun]:
    """Enroll ``contact`` in every published workflow this trigger matches.

    With ``single_answer`` (an inbound message), workflows are tried in priority order and,
    once one has answered the customer, the remaining ones that would also message them are
    skipped, so one "price?" never gets two replies. Silent ones (tagging, telling the
    team) still run.
    """
    message = (context or {}).get("message") or {}
    if is_consent_keyword(message.get("body") or ""):
        return []
    workflows = Workflow.objects.filter(
        account_id=account_id, status=Workflow.Status.PUBLISHED
    ).order_by("priority", "id")
    runs: list[WorkflowRun] = []
    answered = False
    for wf in workflows:
        trigger = (wf.definition or {}).get("trigger", {})
        if trigger.get("type") != trigger_type:
            continue
        if single_answer and answered and _talks(wf):
            continue
        if not _trigger_allows(wf, trigger, contact, context or {}):
            continue
        enrolled_at = timezone.now()
        try:
            with transaction.atomic():
                run = enroll(
                    wf, contact, context=context or {}, subject_key=subject_key
                )
                if run is not None:
                    run._enrolled_at = enrolled_at  # type: ignore[attr-defined]
                    runs.append(run)
        except Exception:
            logger.exception(
                "enroll_for_trigger: wf=%s trigger=%s", wf.pk, trigger_type
            )
            continue
        if single_answer and run is not None and _answered(run, since=enrolled_at):
            answered = True
    return runs


def _trigger_allows(workflow: Workflow, trigger: dict, contact, context: dict) -> bool:
    """Trigger-level filters: a keyword ``match`` on the message, then a per-contact cooldown."""
    return trigger_verdict(workflow, trigger, contact, context) is None


def trigger_verdict(
    workflow: Workflow, trigger: dict, contact, context: dict
) -> dict | None:
    """Why a trigger's own filters refuse this event, or None if they let it through.

    Returns a structured reason (``{"code": ..., "details": {...}}``), never wording; the owner-facing
    sentences live in ``apps.automation.explain``. A trigger with neither a keyword match nor a reply
    id nor a cooldown is unaffected. The cooldown defaults to an hour for keyword workflows, so a
    customer who sends "price?" three times gets one answer.
    """
    message = (context or {}).get("message") or {}
    if trigger.get("form") and (context or {}).get("form") != trigger["form"]:
        return {"code": "other_form", "details": {"expected": trigger["form"]}}
    wanted_ads = trigger.get("from_ad")
    if wanted_ads:
        # The conversations app puts the ad in the context (``ads_automation``); without
        # it, or on a later message in the same ad chat, the message is not from an ad.
        ad = (context or {}).get("ad") or {}
        if not ad.get("source_id"):
            return {"code": "not_from_ad", "details": {}}
        if isinstance(wanted_ads, list) and ad["source_id"] not in wanted_ads:
            return {
                "code": "other_ad",
                "details": {"expected": list(wanted_ads), "received": ad["source_id"]},
            }
    match = trigger.get("match")
    if match:
        from apps.automation import keywords

        body = message.get("body") or ""
        if not keywords.matches(match, body):
            return {
                "code": "keyword_mismatch",
                "details": {
                    "expected": list(match.get("any") or []),
                    "mode": match.get("mode"),
                    "received": body,
                },
            }
    wanted = trigger.get("reply_id")
    if wanted:
        wanted = {
            w.casefold() for w in ([wanted] if isinstance(wanted, str) else wanted)
        }
        got = message.get("reply_id") or ""
        if got.casefold() not in wanted:
            return {
                "code": "reply_mismatch",
                "details": {"expected": sorted(wanted), "received": got},
            }
    cooldown = trigger.get(
        "cooldown_minutes", DEFAULT_KEYWORD_COOLDOWN_MINUTES if match else 0
    )
    if cooldown:
        since = timezone.now() - timedelta(minutes=cooldown)
        last = (
            WorkflowRun.objects.filter(
                workflow=workflow, contact=contact, started_at__gte=since
            )
            .order_by("-started_at")
            .first()
        )
        if last is not None:
            return {
                "code": "cooldown",
                "details": {"minutes": cooldown, "last_started_at": last.started_at},
            }
    return None


# Triggers that a customer's message can fire. "contact.created" only fires for a brand-new customer.
MESSAGE_TRIGGERS = ("conversation.message_received", "contact.created")


def explain_enrollment(
    account_id: int,
    contact,
    *,
    message: dict,
    message_at,
    is_first_message: bool,
    ad: dict | None = None,
) -> list[dict]:
    """For each automation a customer's message could have started: did it, and if not, why not.

    Returns structured verdicts (``{"workflow", "code", "details"}``), never wording. Codes:
    ``ran`` / ``already_running`` / ``not_on`` / ``not_new_customer`` / ``keyword_mismatch`` /
    ``reply_mismatch`` / ``cooldown`` / ``no_run``. It reuses ``trigger_verdict``, the same check
    ``enroll_for_trigger`` applies, so the explanation cannot drift from what actually happens.
    Callers supply the message facts; this module reads no other app's data.
    """
    out: list[dict] = []
    context = {"message": message, **({"ad": ad} if ad else {})}
    for wf in Workflow.objects.filter(account_id=account_id).order_by("name"):
        trigger = (wf.definition or {}).get("trigger", {})
        kind = trigger.get("type")
        if kind not in MESSAGE_TRIGGERS:
            continue

        def verdict(code: str, _wf: object = wf, **details) -> None:
            out.append({"workflow": _wf, "code": code, "details": details})

        if wf.status != Workflow.Status.PUBLISHED:
            verdict("not_on", status=wf.status)
            continue
        if kind == "contact.created" and not is_first_message:
            verdict("not_new_customer")
            continue
        run = (
            WorkflowRun.objects.filter(
                workflow=wf,
                contact=contact,
                started_at__gte=message_at - timedelta(minutes=2),
            )
            .order_by("-started_at")
            .first()
        )
        if run is not None:
            failed = run.step_runs.filter(status="error").order_by("-id").first()
            verdict(
                "ran",
                run=run,
                error=(failed.result or {}).get("error", "") if failed else "",
            )
            continue
        blocked = (
            trigger_verdict(wf, trigger, contact, context)
            if kind == "conversation.message_received"
            else None
        )
        if blocked is not None:
            verdict(blocked["code"], **blocked["details"])
            continue
        if WorkflowRun.objects.filter(
            workflow=wf,
            contact=contact,
            status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        ).exists():
            verdict("already_running")
            continue
        verdict("no_run")
    return out


_RUN_DUE_BATCH = 200


def run_due() -> int:
    """Resume every WAITING run whose timer has elapsed, and sweep stalled
    ACTIVE runs (durable continuation recovery from A0 claim_reply crashes).

    Rows are claimed under ``select_for_update(skip_locked=True)`` (where the
    backend supports it) so parallel beat ticks / workers process disjoint
    batches rather than racing the same run into a double-send.

    Stalled ACTIVE runs: ``claim_reply`` sets ``next_due_at = now`` after
    committing the run state so the sweeper can find it.  If ``advance_run``
    never ran (process crash), the run sits ACTIVE with ``next_due_at`` in the
    past.  We pick those up after a 60-second grace period.
    """
    from datetime import timedelta

    from django.db import connection

    lock_kwargs = (
        {"skip_locked": True}
        if connection.features.has_select_for_update_skip_locked
        else {}
    )
    n = 0
    stall_cutoff = timezone.now() - timedelta(seconds=60)

    with transaction.atomic():
        waiting_due = (
            WorkflowRun.objects.select_for_update(**lock_kwargs)  # type: ignore[arg-type]
            .filter(
                status=WorkflowRun.Status.WAITING,
                next_due_at__lte=timezone.now(),
                workflow__status=Workflow.Status.PUBLISHED,
                workflow__account__is_active=True,
            )  # suspended: waits resume on reactivation
            .order_by("next_due_at", "id")[:_RUN_DUE_BATCH]
        )
        runs = list(waiting_due)

    # Stalled ACTIVE runs: a claim_reply committed but advance_run never ran.
    with transaction.atomic():
        stalled_active = (
            WorkflowRun.objects.select_for_update(**lock_kwargs)  # type: ignore[arg-type]
            .filter(
                status=WorkflowRun.Status.ACTIVE,
                next_due_at__lte=stall_cutoff,
                workflow__status=Workflow.Status.PUBLISHED,
                workflow__account__is_active=True,
            )
            .order_by("next_due_at", "id")[:_RUN_DUE_BATCH]
        )
        runs += list(stalled_active)

    for run in runs:
        try:
            with transaction.atomic():
                # Re-lock the run inside its own transaction so the fast-path
                # and the sweeper can't both advance it.
                locked = (
                    WorkflowRun.objects.select_for_update()
                    .select_related("workflow")
                    .get(pk=run.pk)
                )
                if locked.status not in (
                    WorkflowRun.Status.ACTIVE,
                    WorkflowRun.Status.WAITING,
                ):
                    continue  # already advanced by the fast path
                # A WAITING run that was re-parked by a concurrent claim_reply (e.g. a new
                # IG send_buttons step with a future deadline) is not yet due; skip it.
                if locked.status == WorkflowRun.Status.WAITING:
                    if not locked.next_due_at or locked.next_due_at > timezone.now():
                        continue
                # Clear the stall marker so the run looks normal to advance_run.
                if locked.status == WorkflowRun.Status.ACTIVE and locked.next_due_at:
                    locked.next_due_at = None
                    locked.save(update_fields=["next_due_at"])
                advance_run(locked)
            n += 1
        except Exception:
            logger.exception("run_due: advance failed for run %s", run.pk)
    return n
