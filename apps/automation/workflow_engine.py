"""Durable executor for lifecycle Workflows (Phase 6).

A run walks the ``definition["steps"]`` graph one step at a time. Synchronous
steps run inline; a ``wait`` step parks the run with ``next_due_at`` and the
``run_due_workflows`` beat task resumes it. Every step writes a
``WorkflowStepRun`` so a crashed/re-run ``advance_run`` never double-sends.

Definition shape::

    {
      "trigger": {"type": "business_event", "name": "signup.completed"},
      "steps": [
        {"id": "welcome", "type": "send_email", "template": "welcome",
         "from": "hi@acme.com", "next": "wait1"},
        {"id": "wait1", "type": "wait", "seconds": 172800, "next": "check"},
        {"id": "check", "type": "branch",
         "field": "opened_in_last_90d", "operator": "eq", "value": true,
         "on_true": "tips", "on_false": "reminder"},
        {"id": "tips", "type": "send_email", "template": "tips",
         "from": "hi@acme.com", "next": "stop"},
        {"id": "reminder", "type": "send_email", "template": "reminder",
         "from": "hi@acme.com", "next": "stop"},
        {"id": "stop", "type": "stop"}
      ]
    }
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun

logger = logging.getLogger(__name__)

_MAX_STEPS_PER_CALL = 50

_STEP_TYPES = {
    "send_email", "send_whatsapp", "webhook", "wait", "branch", "set_attribute", "stop", "exit",
    # Phase 4: a generic step that calls through the shared Action Registry
    # (apps.core.actions) by name, rather than requiring a hand-written
    # ``_run_<type>`` function per capability. New actions (CRM, Commerce,
    # and anything Phase 5/6 adds) are usable from a Workflow the moment
    # they're registered — no workflow_engine change needed.
    "action",
}
_TRIGGER_TYPES = {
    "business_event", "manual",
    "contact.created", "contact.updated",
    "email.opened", "email.clicked",
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
}


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
        message: str, *, step_id: str | None = None, field: str | None = None,
        severity: str = "error",
    ) -> None:
        errors.append({"step_id": step_id, "field": field, "message": message, "severity": severity})

    definition = definition or {}
    trig = definition.get("trigger") or {}
    if trig.get("type") not in _TRIGGER_TYPES:
        _error(f"trigger.type must be one of {sorted(_TRIGGER_TYPES)}", field="trigger.type")
    elif trig.get("type") == "business_event" and not trig.get("name"):
        _error("business_event trigger requires trigger.name", field="trigger.name")

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
            _error(f"step {sid!r}: type must be one of {sorted(_STEP_TYPES)}", step_id=sid, field="type")
        if step.get("type") == "send_email" and not (
            step.get("template") or step.get("subject")
        ):
            _error(f"step {sid!r}: send_email needs a template or subject", step_id=sid, field="template")
        if step.get("type") == "send_email" and not step.get("from"):
            _error(f"step {sid!r}: send_email needs a from address", step_id=sid, field="from")
        if step.get("type") == "send_whatsapp" and not step.get("template"):
            _error(f"step {sid!r}: send_whatsapp needs a template", step_id=sid, field="template")
        if step.get("type") == "send_whatsapp" and step.get("template") and account is not None:
            from apps.whatsapp.models import MessageTemplate

            template = MessageTemplate.objects.filter(
                account=account, whatsapp_template_name=step["template"]
            ).first()
            if template is None:
                _error(
                    f"step {sid!r}: template {step['template']!r} not found",
                    step_id=sid, field="template",
                )
            else:
                if template.approval_status != template.ApprovalStatus.APPROVED:
                    _error(
                        f"step {sid!r}: template {step['template']!r} is not yet approved "
                        f"by Meta (status={template.approval_status}) — sends will fail until approved",
                        step_id=sid, field="template", severity="warning",
                    )
                mapping = step.get("variable_mapping") or {}
                missing = [v for v in (template.variables or []) if v not in mapping]
                if missing:
                    _error(
                        f"step {sid!r}: template {step['template']!r} needs a mapping for "
                        f"variable(s) {', '.join(missing)}",
                        step_id=sid, field="variable_mapping",
                    )
        if step.get("type") == "action":
            if not step.get("action"):
                _error(f"step {sid!r}: action needs an action name", step_id=sid, field="action")
            else:
                from apps.core.actions import ActionError, get_action

                try:
                    registered = get_action(step["action"])
                except ActionError:
                    _error(
                        f"step {sid!r}: no action registered as {step['action']!r}",
                        step_id=sid, field="action",
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
                            step_id=sid, field="params",
                        )
        if step.get("type") == "webhook" and not step.get("url"):
            _error(f"step {sid!r}: webhook needs a url", step_id=sid, field="url")
        if step.get("type") == "branch" and not (
            step.get("on_true") and step.get("on_false") and step.get("field")
        ):
            _error(f"step {sid!r}: branch needs field, on_true, on_false", step_id=sid, field="field")
        if step.get("type") in ("send_email", "send_whatsapp") and step.get("send_at"):
            if parse_datetime(step["send_at"]) is None:
                _error(f"step {sid!r}: send_at is not a valid datetime", step_id=sid, field="send_at")

    def _check(ref, sid, field):
        if ref and ref not in ids:
            _error(f"step {sid!r}.{field} points to unknown step {ref!r}", step_id=sid, field=field)

    for step in steps:
        sid = step.get("id")
        if not sid:
            continue
        if step.get("type") == "branch":
            _check(step.get("on_true"), sid, "on_true")
            _check(step.get("on_false"), sid, "on_false")
        elif step.get("type") not in ("stop", "exit"):
            _check(step.get("next"), sid, "next")
    return errors


def _steps_by_id(workflow: Workflow) -> dict:
    return {s["id"]: s for s in (workflow.definition or {}).get("steps", []) if "id" in s}


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
    if workflow.status != Workflow.Status.PUBLISHED:
        return None
    from apps.billing import api as billing_api

    if not billing_api.module_enabled(workflow.account, "automation"):
        return None
    first = _first_step_id(workflow)
    if first is None:
        return None
    run, created = WorkflowRun.objects.get_or_create(
        workflow=workflow,
        contact=contact,
        subject_key=subject_key,
        status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        defaults={"context": context or {}, "current_step": first},
    )
    if not created:
        return run
    _notify_workflow(run, "workflow.started")
    advance_run(run)
    return run


def _notify_workflow(run: WorkflowRun, event_type: str) -> None:
    try:
        from apps.email.webhooks import notify

        notify(run.workflow.account, event_type, {
            "run_id": run.public_id,
            "workflow": run.workflow.slug,
            "contact": run.contact.public_id,
            "status": run.status,
        })
    except Exception:
        logger.exception("_notify_workflow failed for run %s", run.pk)


def _condition_matches(contact, step: dict) -> bool:
    from apps.contacts.models import Contact
    from apps.contacts.segments import build_q

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

    contact = run.contact
    if not contact.email:
        raise ValueError(f"send_email step requires an email, but contact {contact.pk} has none")
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
    from django.utils.dateparse import parse_datetime

    from apps.automation.workflows import send_whatsapp_message
    from apps.core.scheduling import to_utc

    contact = run.contact
    phone_field = step.get("phone_field", "phone")
    # The canonical number lives on Contact.phone (WhatsApp-originated contacts have
    # no attributes); a custom phone_field still reads from attributes.
    phone = (contact.phone if phone_field == "phone" else None) or (contact.attributes or {}).get(phone_field)
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
        template.variables, step.get("variable_mapping") or {}, run, contact
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
    )
    return {"outbound_message_id": msg.id}


def _resolve_whatsapp_template(account, name):
    if not name:
        return None
    from apps.whatsapp.models import MessageTemplate

    return MessageTemplate.objects.filter(account=account, whatsapp_template_name=name).first()


def _resolve_variable_mapping(variables: list, mapping: dict, run: WorkflowRun, contact) -> dict:
    """Build the WhatsApp template ``params`` dict from an explicit, allow-listed mapping.

    ``variables`` is the template's declared placeholder names (``MessageTemplate.variables``,
    e.g. ``["name", "company"]`` — named, not Meta's positional ``{{1}}``/``{{2}}`` keys).
    ``mapping`` is ``step["variable_mapping"]``, keyed by those same names, valued by one of:

    - ``"contact.<attr>"``  -> ``contact.attributes.get(attr)``
    - ``"context.<key>"``   -> ``run.context.get(key)``
    - anything else         -> used verbatim as a literal constant

    Only variable names present in ``variables`` are ever resolved, and only the
    ``contact``/``context`` keys explicitly named in ``mapping`` are ever read — the
    full ``run.context`` is never forwarded wholesale, so nothing outside the
    declared mapping can reach the outbound WhatsApp message.
    """
    params: dict = {}
    contact_attrs = contact.attributes or {}
    context = run.context or {}
    for var in variables or []:
        if var not in mapping:
            continue
        source = mapping[var]
        if isinstance(source, str) and source.startswith("contact."):
            params[var] = contact_attrs.get(source[len("contact."):])
        elif isinstance(source, str) and source.startswith("context."):
            params[var] = context.get(source[len("context."):])
        else:
            params[var] = source
    return params


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
        return contact_attrs.get(source[len("contact."):])
    if isinstance(source, str) and source.startswith("context."):
        return context.get(source[len("context."):])
    return source


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
    kwargs = {}

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
                    kwargs[field_name] = _resolve_action_object(field_name, public_id, run)
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
        raise ValueError(f"action step {step.get('id')!r} ({step['action']}): {exc}") from exc


def _apply_set_attribute(run: WorkflowRun, step: dict) -> dict:
    contact = run.contact
    contact.attributes = {**(contact.attributes or {}), step["key"]: step.get("value")}
    contact.save(update_fields=["attributes", "updated_at"])
    return {"key": step["key"]}


def advance_run(run: WorkflowRun) -> WorkflowRun:
    """Execute steps until the run waits, stops, or completes."""
    if run.status not in (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING):
        return run

    steps = _steps_by_id(run.workflow)

    for _ in range(_MAX_STEPS_PER_CALL):
        step = steps.get(run.current_step)
        if step is None:
            return _complete(run)

        stype = step.get("type")

        if stype == "wait":
            if run.status == WorkflowRun.Status.WAITING and run.next_due_at and run.next_due_at <= timezone.now():
                # resuming from the wait
                _record(run, step, {"resumed": True})
                run.status = WorkflowRun.Status.ACTIVE
                run.next_due_at = None
                run.current_step = step.get("next", "")
                run.save(update_fields=["status", "next_due_at", "current_step"])
                continue
            run.status = WorkflowRun.Status.WAITING
            run.next_due_at = timezone.now() + timedelta(seconds=int(step.get("seconds", 0)))
            run.save(update_fields=["status", "next_due_at"])
            return run

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
            elif stype == "branch":
                matched = _condition_matches(run.contact, step)
                result = {"matched": matched}
            elif stype == "set_attribute":
                result = _apply_set_attribute(run, step)
            elif stype in ("stop", "exit"):
                _record(run, step, {})
                return _complete(run)
            else:
                raise ValueError(f"unsupported workflow step type {stype!r}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("workflow run %s step %s failed", run.pk, step.get("id"))
            _record(run, step, {"error": str(exc)}, status="error")
            run.status = WorkflowRun.Status.FAILED
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "completed_at"])
            return run

        _record(run, step, result)
        run.current_step = _next_after(step, run, branch_result=result)
        run.save(update_fields=["current_step"])
        if not run.current_step:
            return _complete(run)

    logger.warning("workflow run %s hit step limit; parking", run.pk)
    return run


def _next_after(step: dict, run: WorkflowRun, *, branch_result: dict | None = None) -> str:
    if step.get("type") == "branch":
        if branch_result is None:
            branch_result = {"matched": _condition_matches(run.contact, step)}
        return step["on_true"] if branch_result.get("matched") else step["on_false"]
    return step.get("next", "")


def _record(run: WorkflowRun, step: dict, result: dict, *, status: str = "ok") -> None:
    from django.db import IntegrityError

    try:
        WorkflowStepRun.objects.create(
            run=run, step_id=step["id"], step_type=step.get("type", ""),
            status=status, result=result,
            # Keep the FK in sync with the legacy JSON key so both the
            # reconciliation hook (apps.automation.integrations.whatsapp) and
            # any existing code reading result["outbound_message_id"] work.
            outbound_message_id=result.get("outbound_message_id"),
        )
    except IntegrityError:
        # uniq_workflowsteprun_run_step — a concurrent advance already recorded
        # this step. The .exists() guard in advance_run is best-effort; this
        # constraint is the real backstop against a double-send.
        logger.info("workflow run %s step %s already recorded", run.pk, step["id"])


def _complete(run: WorkflowRun) -> WorkflowRun:
    run.status = WorkflowRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.next_due_at = None
    run.save(update_fields=["status", "completed_at", "next_due_at"])
    _notify_workflow(run, "workflow.completed")
    return run


# ── Trigger evaluation ───────────────────────────────────────────────────────

def on_business_event(event, **kwargs) -> None:
    """Enroll the event's contact into workflows triggered by this event name."""
    if event.contact_id is None:
        return
    from apps.contacts.models import Contact

    contact = Contact.objects.filter(pk=event.contact_id).first()
    if contact is None:
        return
    workflows = Workflow.objects.filter(
        account_id=event.account_id, status=Workflow.Status.PUBLISHED
    )
    for wf in workflows:
        trig = (wf.definition or {}).get("trigger", {})
        if trig.get("type") == "business_event" and trig.get("name") == event.name:
            try:
                with transaction.atomic():
                    enroll(wf, contact, context={"event": event.data})
            except Exception:  # noqa: BLE001
                logger.exception("on_business_event: enroll failed wf=%s", wf.pk)


def enroll_for_trigger(
    account_id: int, trigger_type: str, contact, *, context: dict | None = None, subject_key: str = ""
) -> int:
    """Enroll ``contact`` into every published workflow whose trigger matches.

    Used for the lightweight contact-lifecycle triggers (``contact.created``,
    ``email.opened``, …). ``business_event`` has its own richer path in
    ``on_business_event``.
    """
    workflows = Workflow.objects.filter(
        account_id=account_id, status=Workflow.Status.PUBLISHED
    )
    n = 0
    for wf in workflows:
        if (wf.definition or {}).get("trigger", {}).get("type") != trigger_type:
            continue
        try:
            with transaction.atomic():
                if enroll(wf, contact, context=context or {}, subject_key=subject_key) is not None:
                    n += 1
        except Exception:  # noqa: BLE001
            logger.exception("enroll_for_trigger: wf=%s trigger=%s", wf.pk, trigger_type)
    return n


_RUN_DUE_BATCH = 200


def run_due() -> int:
    """Resume every WAITING run whose timer has elapsed. Called by a beat task.

    Rows are claimed under ``select_for_update(skip_locked=True)`` (where the
    backend supports it) so parallel beat ticks / workers process disjoint
    batches rather than racing the same run into a double-send.
    """
    from django.db import connection

    lock_kwargs = (
        {"skip_locked": True}
        if connection.features.has_select_for_update_skip_locked
        else {}
    )
    n = 0
    with transaction.atomic():
        due = (
            WorkflowRun.objects.select_for_update(**lock_kwargs)
            .filter(status=WorkflowRun.Status.WAITING, next_due_at__lte=timezone.now())
            .order_by("next_due_at", "id")[:_RUN_DUE_BATCH]
        )
        runs = list(due)

    for run in runs:
        try:
            with transaction.atomic():
                advance_run(run)
            n += 1
        except Exception:  # noqa: BLE001
            logger.exception("run_due: advance failed for run %s", run.pk)
    return n
