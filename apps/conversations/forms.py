"""Native forms inside a conversation: ask, validate deterministically (no AI), store, map.

One form definition, two ways to fill it in:

* **WhatsApp Flow**: a native in-chat form (``flow_json``). The answers come back in one
  ``nfm_reply`` (``complete_from_flow``).
* **One message at a time**: every other channel, and WhatsApp forms not set to Flow.
  Each answer is checked before the next question (``record_answer``).

Both validate with the same rules, write with the same mapping, and finish in ``_finish``:
thank the customer, optionally track them as a lead, record ``conversation.form_completed``
and start any automation triggered by it.

Sends go through ``run_action("reply", ...)`` (same 24h-window and opt-out checks as a human
reply). Runtime state lives on ``FormResponse``; there is no parallel state machine.

Flow tokens carry the form (``f<form id>.<random>``), so a Flow opened from a template
button, where no response row existed before the customer tapped it, still completes.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.conversations.flow_json import (
    CHOICE_TYPES,
    FLOW_MESSAGE_VERSION,
    SCREEN_ID,
    option_title,
)
from apps.conversations.models import ConversationForm, Event, FormResponse

logger = logging.getLogger(__name__)

SKIP_WORDS = {"skip", "none", "n/a", "na", "-"}
YES_WORDS = {"yes", "y", "ok", "okay", "sure", "agree", "i agree", "yep", "yeah"}
NO_WORDS = {"no", "n", "nope", "don't", "dont"}


class FormError(Exception):
    """A caller mistake (e.g. starting an empty form). Never a customer's invalid answer,
    which is handled by asking again, not raising."""


# ── Validation: one set of rules for typed answers and Flow answers ────────────────────


def _validate_text(value, question=None) -> str | None:
    return str(value).strip() or None


def _validate_email(value, question=None) -> str | None:
    from django.core.validators import validate_email

    value = str(value).strip()
    try:
        validate_email(value)
    except ValidationError:
        return None
    return value


def _validate_phone(value, question=None) -> str | None:
    from apps.whatsapp.models.contact import normalize_phone

    try:
        return normalize_phone(str(value))
    except ValidationError:
        return None


def _validate_number(value, question=None) -> str | None:
    try:
        float(str(value).strip().replace(",", ""))
    except ValueError:
        return None
    return str(value).strip()


def _options(question) -> list[str]:
    return [
        " ".join(str(o).split())
        for o in (question or {}).get("options") or []
        if str(o).strip()
    ]


def _validate_choice(value, question=None) -> str | None:
    """A Flow option id, the choice's own words, or its number in the list."""
    options = _options(question)
    raw = str(value).strip()
    from_flow = option_title(question or {}, raw)
    if from_flow:
        return from_flow
    for o in options:
        if raw.casefold() == o.casefold():
            return o
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return None


def _validate_multi(value, question=None) -> list[str] | None:
    parts = (
        value
        if isinstance(value, list)
        else str(value).replace(" and ", ",").split(",")
    )
    picked = []
    for part in parts:
        choice = _validate_choice(part, question)
        if choice is None:
            return None
        if choice not in picked:
            picked.append(choice)
    return picked or None


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %B %Y", "%d %b %Y")


def _validate_date(value, question=None) -> str | None:
    raw = str(value).strip()
    if raw.isdigit() and len(raw) >= 12:  # a Flow DatePicker's epoch milliseconds
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).date().isoformat()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _validate_opt_in(value, question=None) -> bool | None:
    if isinstance(value, bool):
        return value
    raw = str(value).strip().casefold()
    if raw in YES_WORDS or raw == "true":
        return True
    if raw in NO_WORDS or raw == "false":
        return False
    return None


_VALIDATORS = {
    "text": _validate_text,
    "long_text": _validate_text,
    "email": _validate_email,
    "phone": _validate_phone,
    "number": _validate_number,
    "choice": _validate_choice,
    "dropdown": _validate_choice,
    "multi_choice": _validate_multi,
    "date": _validate_date,
    "opt_in": _validate_opt_in,
}

_RETRY_PROMPTS = {
    "email": "That doesn't look like an email address. ",
    "phone": "That doesn't look like a phone number. ",
    "number": "That doesn't look like a number. ",
    "choice": "Please pick one of the choices. ",
    "dropdown": "Please pick one of the choices. ",
    "multi_choice": "Please pick from the choices (you can list more than one). ",
    "date": "Please send a date like 25/12/2026. ",
    "opt_in": "Please reply yes or no. ",
}


def validate(question: dict, value):
    """The cleaned answer, or None if it isn't a valid answer to ``question``."""
    validator = _VALIDATORS.get(question.get("field_type", "text"), _validate_text)
    return validator(value, question)


# ── Sending ──────────────────────────────────────────────────────────────────────────


def _send(
    conversation, text: str, *, idempotency_key: str, required: bool = False
) -> None:
    """Send a form message as the automation. ``required``: a failed send raises
    ``FormError`` instead of being logged (the form can't start without it)."""
    from apps.conversations.actions import ActionError, run_action

    try:
        run_action(
            "reply",
            {"account": conversation.account},
            conversation=conversation,
            body=text,
            idempotency_key=idempotency_key,
            sent_by="automation",
        )
    except ActionError as exc:
        if required:
            raise FormError(str(exc)) from exc
        logger.exception(
            "conversations.forms: couldn't send to conversation=%s", conversation.pk
        )


def _question_text(question: dict) -> str:
    """The question as a chat message: options numbered, optional ones skippable."""
    text = question.get("label") or question.get("key") or "?"
    if question.get("helper"):
        text += f"\n{question['helper']}"
    ft = question.get("field_type", "text")
    if ft in CHOICE_TYPES:
        text += "\n" + "\n".join(
            f"{n}. {o}" for n, o in enumerate(_options(question), 1)
        )
        text += (
            "\nReply with the numbers (e.g. 1, 3)."
            if ft == "multi_choice"
            else "\nReply with the number."
        )
    elif ft == "opt_in":
        text += "\nReply yes or no."
    if not question.get("required", True):
        text += "\n(Reply “skip” to leave this out.)"
    return text


# ── Where answers go ─────────────────────────────────────────────────────────────────


def _apply_mapping(contact, maps_to: str, value, *, lead=None) -> None:
    """Write a completed answer where ``maps_to`` says.

    ``contact.first_name`` / ``last_name`` / ``attributes.<key>`` are written; ``contact.email``
    and ``contact.phone`` only fill a blank (they're identity fields: a form never replaces
    one we already have). ``lead.attributes.<key>`` writes on the customer's open lead.
    """
    if not maps_to or value is None:
        return
    stored = ", ".join(value) if isinstance(value, list) else value
    if maps_to.startswith("lead.attributes."):
        key = maps_to.removeprefix("lead.attributes.")
        if key and lead is not None:
            lead.attributes = {**(lead.attributes or {}), key: stored}
            lead.save(update_fields=["attributes", "updated_at"])
        return
    if not maps_to.startswith("contact."):
        return
    field = maps_to.removeprefix("contact.")
    if field.startswith("attributes."):
        key = field.removeprefix("attributes.")
        if not key:
            return
        contact.attributes = {**(contact.attributes or {}), key: stored}
        contact.save(update_fields=["attributes", "updated_at"])
    elif field in ("first_name", "last_name"):
        setattr(contact, field, str(stored)[:150])
        contact.save(update_fields=[field, "updated_at"])
    elif field in ("email", "phone") and not getattr(contact, field):
        from django.db import IntegrityError

        setattr(contact, field, str(stored))
        try:
            with transaction.atomic():
                contact.save(update_fields=[field, "updated_at"])
        except IntegrityError:
            # Another customer already has it: keep the answer on the response only.
            setattr(contact, field, "")


def _open_lead(response):
    from apps.crm.models import Lead

    return (
        Lead.objects.filter(
            account=response.account,
            contact=response.contact,
            status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
        )
        .order_by("-id")
        .first()
    )


def _lead_for(response, conversation):
    """The lead answers should land on, creating one when the form says so."""
    from apps.core.actions import ActionError, run_action

    lead = _open_lead(response)
    if lead is None and response.form.lead_action:
        try:
            run_action(
                "capture_conversation_lead",
                {"account": response.account},
                account=response.account,
                contact=response.contact,
                conversation_id=conversation.public_id,
                signal=f"Filled in the form “{response.form.name}”",
            )
        except ActionError:
            logger.info("form %s: couldn't track lead (feature off?)", response.form_id)
        lead = _open_lead(response)
    return lead


# ── Starting ─────────────────────────────────────────────────────────────────────────


def new_flow_token(form: ConversationForm) -> str:
    return f"f{form.pk}.{uuid.uuid4().hex}"


def form_for_token(account, token: str) -> ConversationForm | None:
    """The form a Flow token belongs to (tokens are ``f<form id>.<random>``)."""
    head = (token or "").split(".", 1)[0]
    if not head.startswith("f") or not head[1:].isdigit():
        return None
    return ConversationForm.objects.filter(account=account, pk=int(head[1:])).first()


def start_form(conversation, form: ConversationForm) -> FormResponse:
    """Begin ``form`` on ``conversation``: create the tracking row and send the first
    question, or launch the WhatsApp Flow. Raises ``FormError`` for an empty or unpublished
    form, ``IntegrityError`` if a form is already in progress here (check
    ``active_response_for`` first)."""
    if not form.questions:
        raise FormError(f"{form.name!r} has no questions.")
    # One unit: if the launch fails the tracking row goes too, otherwise an IN_PROGRESS
    # response with nothing sent would block every later form on this conversation and
    # swallow the customer's next message as an "answer".
    with transaction.atomic():
        response = FormResponse.objects.create(
            account=conversation.account,
            form=form,
            conversation=conversation,
            contact=conversation.contact,
        )
        if uses_flow(form, conversation):
            _start_flow(conversation, form, response)
        else:
            if form.intro_text:
                _send(
                    conversation,
                    form.intro_text,
                    idempotency_key=f"form:{response.pk}:intro",
                    required=True,
                )
            _send(
                conversation,
                _question_text(form.questions[0]),
                idempotency_key=f"form:{response.pk}:q0",
                required=True,
            )
    return response


def uses_flow(form: ConversationForm, conversation) -> bool:
    """Whether ``form`` runs as a WhatsApp Flow on ``conversation``.

    Flows only exist on WhatsApp; on any other channel the same form asks its questions one
    message at a time, with the same validation and the same field mapping.
    """
    return (
        form.presentation == ConversationForm.Presentation.WHATSAPP_FLOW
        and conversation.channel == conversation.Channel.WHATSAPP
    )


def can_start(form: ConversationForm, conversation) -> bool:
    """Whether ``form`` can be launched on ``conversation`` right now (for pickers)."""
    if not form.questions or form.status != ConversationForm.Status.PUBLISHED:
        return False
    if uses_flow(form, conversation):
        return form.flow_status == ConversationForm.FlowStatus.PUBLISHED and bool(
            form.flow_id
        )
    return True


def flow_interactive(form: ConversationForm, token: str) -> dict:
    return {
        "type": "flow",
        "body": {"text": form.intro_text or f"Please fill in {form.name}."},
        "action": {
            "name": "flow",
            "parameters": {
                "flow_message_version": FLOW_MESSAGE_VERSION,
                "flow_token": token,
                "flow_id": form.flow_id,
                "flow_cta": form.cta_label or "Start",
                "flow_action": "navigate",
                "flow_action_payload": {"screen": SCREEN_ID},
            },
        },
    }


def _start_flow(conversation, form: ConversationForm, response: FormResponse) -> None:
    from apps.whatsapp import api as whatsapp_api

    if form.flow_status != ConversationForm.FlowStatus.PUBLISHED or not form.flow_id:
        raise FormError(
            f"{form.name!r} is set to WhatsApp Flow but hasn't been published yet."
        )
    token = new_flow_token(form)
    response.flow_token = token
    response.save(update_fields=["flow_token"])
    whatsapp_api.send_interactive(
        conversation.account,
        conversation.whatsapp_conversation.contact,
        flow_interactive(form, token),
        idempotency_key=f"form:{response.pk}:flow-launch",
    )


def active_response_for(conversation) -> FormResponse | None:
    return FormResponse.objects.filter(
        conversation=conversation, status=FormResponse.Status.IN_PROGRESS
    ).first()


def cancel(response: FormResponse) -> None:
    """Stop waiting for this form (from the inbox, or when it times out)."""
    if response.status == FormResponse.Status.IN_PROGRESS:
        response.status = FormResponse.Status.ABANDONED
        response.completed_at = timezone.now()
        response.save(update_fields=["status", "completed_at"])
        _resume_waiting_runs(response, outcome="timed_out")


def abandon_stale(now=None) -> int:
    """Let go of forms nobody finished within the form's own time limit. Returns how many."""
    now = now or timezone.now()
    count = 0
    # Oldest first, and only ones at least an hour old (the shortest limit a form can have
    # that isn't "now"), so a backlog of fresh forms can't hide the stale ones.
    candidates = (
        FormResponse.objects.filter(
            status=FormResponse.Status.IN_PROGRESS,
            started_at__lte=now - timedelta(hours=1),
        )
        .select_related("form")
        .order_by("started_at")
    )
    for response in candidates.iterator(chunk_size=500):
        if (
            response.started_at + timedelta(hours=response.form.timeout_hours or 72)
            <= now
        ):
            cancel(response)
            count += 1
            if count >= 1000:
                break
    return count


# ── Answers ──────────────────────────────────────────────────────────────────────────


def record_answer(conversation, text: str) -> bool:
    """The customer's latest message, if it's an answer to a form question in progress.

    True when a form took the message (accepted, or invalid and asked again): callers must
    not also run keyword workflows or AI on it. False: handle the message normally.
    """
    response = active_response_for(conversation)
    if response is None:
        return False
    if response.flow_token:
        # The customer is filling in a WhatsApp Flow; its answers arrive as one
        # ``nfm_reply``. Text typed meanwhile is an ordinary message, not an answer.
        return False

    questions = response.form.questions
    if response.current_index >= len(questions):
        # Data got out of sync (e.g. the form was edited mid-flight); close it out
        # rather than loop or crash on a customer's reply.
        response.status = FormResponse.Status.ABANDONED
        response.save(update_fields=["status"])
        return False

    question = questions[response.current_index]
    raw = (text or "").strip()
    if not question.get("required", True) and raw.casefold() in SKIP_WORDS:
        value = None
    else:
        value = validate(question, raw)
        if value is None:
            digest = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:10]
            _send(
                conversation,
                _RETRY_PROMPTS.get(question.get("field_type", "text"), "")
                + _question_text(question),
                idempotency_key=f"form:{response.pk}:retry:{response.current_index}:{digest}",
            )
            return True
        response.answers = {
            **response.answers,
            question.get("key", str(response.current_index)): value,
        }
    response.current_index += 1

    if response.current_index < len(questions):
        response.save(update_fields=["answers", "current_index"])
        _send(
            conversation,
            _question_text(questions[response.current_index]),
            idempotency_key=f"form:{response.pk}:q{response.current_index}",
        )
        return True

    _finish(response, conversation)
    return True


def complete_from_flow(conversation, flow_token: str, fields: dict) -> bool:
    """A customer's ``nfm_reply`` completing a WhatsApp Flow form, applied in one go.

    True when the reply belonged to a form (completed now, or already completed: a repeated
    reply is consumed, not passed on to automations or AI). False: not a form's reply.

    A Flow opened from a template button has no response row until now; the token names the
    form, so one is made. A field that fails Akilent's own validation (rare: the Flow already
    checked it) is left out rather than failing the whole completion.
    """
    if not flow_token:
        return False
    response = FormResponse.objects.filter(
        conversation=conversation, flow_token=flow_token
    ).first()
    if response is not None and response.status == FormResponse.Status.COMPLETED:
        return True
    if response is None:
        form = form_for_token(conversation.account, flow_token)
        if form is None:
            return False
        # Whatever was in progress here is superseded by the form they just submitted.
        for stale in FormResponse.objects.filter(
            conversation=conversation, status=FormResponse.Status.IN_PROGRESS
        ):
            stale.status = FormResponse.Status.ABANDONED
            stale.save(update_fields=["status"])
        response = FormResponse.objects.create(
            account=conversation.account,
            form=form,
            conversation=conversation,
            contact=conversation.contact,
            flow_token=flow_token,
        )
    elif response.status != FormResponse.Status.IN_PROGRESS:
        return True  # abandoned (timed out or cancelled); don't resurrect it

    answers = dict(response.answers)
    for question in response.form.questions:
        key = question.get("key", "")
        if key not in fields or fields[key] in (None, "", []):
            continue
        value = validate(question, fields[key])
        if value is None:
            continue
        answers[key] = value
    response.answers = answers
    response.current_index = len(response.form.questions)
    _finish(response, conversation)
    return True


def _finish(response: FormResponse, conversation) -> None:
    """Mark ``response`` completed, write the mapped answers, thank the customer, record
    ``conversation.form_completed`` and start automations listening for it."""
    response.status = FormResponse.Status.COMPLETED
    response.completed_at = timezone.now()
    response.save(update_fields=["answers", "current_index", "status", "completed_at"])

    form = response.form
    lead = _lead_for(response, conversation)
    for question in form.questions:
        key = question.get("key", "")
        if key in response.answers:
            _apply_mapping(
                response.contact,
                question.get("maps_to", ""),
                response.answers[key],
                lead=lead,
            )
    if lead is not None and form.lead_action == ConversationForm.LeadAction.QUALIFY:
        from apps.core.actions import ActionError, run_action

        try:
            run_action(
                "update_lead_status",
                {"account": response.account},
                lead=lead,
                status="qualified",
            )
        except ActionError:
            logger.info("form %s: couldn't qualify lead %s", form.pk, lead.pk)

    _send(
        conversation,
        form.thanks_text or ConversationForm.DEFAULT_THANKS,
        idempotency_key=f"form:{response.pk}:done",
    )
    Event.objects.create(
        account_id=conversation.account_id,
        type="conversation.form_completed",
        occurred_at=timezone.now(),
        source="conversations",
        subject_type="conversation",
        subject_id=str(conversation.pk),
        payload={"form_id": response.form_id, "response_id": response.pk},
    )
    _resume_waiting_runs(response, outcome="completed")
    _start_automations(response, conversation)


def _resume_waiting_runs(response: FormResponse, *, outcome: str) -> None:
    try:
        from apps.automation.api import form_finished

        form_finished(response, outcome=outcome)
    except Exception:
        logger.exception("form %s: couldn't resume waiting automations", response.pk)


def _start_automations(response: FormResponse, conversation) -> None:
    try:
        from apps.automation.workflow_engine import enroll_for_trigger

        enroll_for_trigger(
            conversation.account_id,
            "conversation.form_completed",
            response.contact,
            context={
                "conversation_id": conversation.public_id,
                "form": response.form.public_id,
                "answers": {
                    k: (", ".join(v) if isinstance(v, list) else v)
                    for k, v in response.answers.items()
                },
            },
            subject_key=f"form:{response.pk}",
        )
    except Exception:
        logger.exception(
            "form %s: couldn't start form-completed automations", response.pk
        )
