"""Phase C item 11: native WhatsApp forms — ask one question at a time, validate the
answer deterministically (no AI), store it, and optionally write it onto the Contact.

Reuses the send path every other reply goes through (``run_action("reply", ...)``, so a
question is subject to the same 24h-window/opt-out checks a human reply would be) and the
mapping-JSON idea already proven by ``ContactImport.mapping``. Runtime state lives entirely
on ``FormResponse`` — no parallel workflow/state machine.
"""

from __future__ import annotations

import hashlib
import logging

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.conversations.models import ConversationForm, Event, FormResponse

logger = logging.getLogger(__name__)


class FormError(Exception):
    """Raised for a caller mistake (e.g. starting an empty form) — never for a customer's
    invalid answer, which is handled by re-prompting, not raising."""


def _validate_text(value: str) -> str | None:
    return value.strip() or None


def _validate_email(value: str) -> str | None:
    from django.core.validators import validate_email

    value = value.strip()
    try:
        validate_email(value)
    except ValidationError:
        return None
    return value


def _validate_phone(value: str) -> str | None:
    from apps.whatsapp.models.contact import normalize_phone

    try:
        return normalize_phone(value)
    except ValidationError:
        return None


def _validate_number(value: str) -> str | None:
    try:
        float(value.strip().replace(",", ""))
    except ValueError:
        return None
    return value.strip()


_VALIDATORS = {
    "text": _validate_text,
    "email": _validate_email,
    "phone": _validate_phone,
    "number": _validate_number,
}

_RETRY_PROMPTS = {
    "email": "That doesn't look like an email address. ",
    "phone": "That doesn't look like a phone number. ",
    "number": "That doesn't look like a number. ",
    "text": "",
}


def _send(conversation, text: str, *, idempotency_key: str) -> None:
    from apps.conversations.actions import ActionError, run_action

    try:
        run_action(
            "reply",
            {"account": conversation.account},
            conversation=conversation,
            body=text,
            idempotency_key=idempotency_key,
        )
    except ActionError:
        logger.exception(
            "conversations.forms: couldn't send to conversation=%s", conversation.pk
        )


def _apply_mapping(contact, maps_to: str, value: str) -> None:
    """Write a completed answer onto the Contact, per ``maps_to``.

    Deliberately narrow: only ``first_name``/``last_name`` and custom
    ``attributes.<key>`` — never ``email``/``phone``, which are identity
    fields with their own uniqueness rules and are not something a form
    answer should silently change.
    """
    if not maps_to or not maps_to.startswith("contact."):
        return
    field = maps_to.removeprefix("contact.")
    if field.startswith("attributes."):
        key = field.removeprefix("attributes.")
        if not key:
            return
        contact.attributes = {**(contact.attributes or {}), key: value}
        contact.save(update_fields=["attributes", "updated_at"])
    elif field in ("first_name", "last_name"):
        setattr(contact, field, value)
        contact.save(update_fields=[field, "updated_at"])


def _question_text(question: dict) -> str:
    return question.get("label") or question.get("key") or "?"


def start_form(conversation, form: ConversationForm) -> FormResponse:
    """Begin ``form`` on ``conversation``: sends the first question and creates the
    tracking row. Raises ``FormError`` if the form has no questions, or a
    ``django.db.IntegrityError`` (via the model's constraint) if this conversation
    already has a form in progress — callers should check first with
    ``active_response_for``.
    """
    if not form.questions:
        raise FormError(f"{form.name!r} has no questions.")
    response = FormResponse.objects.create(
        account=conversation.account,
        form=form,
        conversation=conversation,
        contact=conversation.contact,
    )
    _send(
        conversation,
        _question_text(form.questions[0]),
        idempotency_key=f"form:{response.pk}:q0",
    )
    return response


def active_response_for(conversation) -> FormResponse | None:
    return FormResponse.objects.filter(
        conversation=conversation, status=FormResponse.Status.IN_PROGRESS
    ).first()


def record_answer(conversation, text: str) -> bool:
    """The customer's latest message, if it's an answer to a form question in progress.

    Returns True when the message was consumed by a form (whether the answer was valid
    and accepted, or invalid and re-prompted) — callers must not also run keyword
    workflows or an AI suggestion on it either way. False means there's nothing to do
    here and the message should be handled normally.
    """
    response = active_response_for(conversation)
    if response is None:
        return False

    questions = response.form.questions
    if response.current_index >= len(questions):
        # Data got out of sync (e.g. the form was edited mid-flight); close it out
        # rather than loop or crash on a customer's reply.
        response.status = FormResponse.Status.ABANDONED
        response.save(update_fields=["status"])
        return False

    question = questions[response.current_index]
    validator = _VALIDATORS.get(question.get("field_type", "text"), _validate_text)
    value = validator(text or "")
    if value is None:
        digest = hashlib.sha1((text or "").encode()).hexdigest()[:10]
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
    _apply_mapping(response.contact, question.get("maps_to", ""), value)
    response.current_index += 1

    if response.current_index < len(questions):
        response.save(update_fields=["answers", "current_index"])
        _send(
            conversation,
            _question_text(questions[response.current_index]),
            idempotency_key=f"form:{response.pk}:q{response.current_index}",
        )
        return True

    response.status = FormResponse.Status.COMPLETED
    response.completed_at = timezone.now()
    response.save(update_fields=["answers", "current_index", "status", "completed_at"])
    _send(
        conversation,
        "Thanks - got everything I need!",
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
    return True
