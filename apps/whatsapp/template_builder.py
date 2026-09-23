"""In-Akilent WhatsApp template creation (R1.5c follow-up amendment, 2026-09-22).

Meta's own template UI is too complex for a non-technical business owner.
This module is the "simplified builder" that sits in front of it: validate a
plain form, build Meta's template-creation payload, submit it, and store the
result locally as a normal ``MessageTemplate`` in ``PENDING`` status — the
existing sync path (``apps.whatsapp.tasks.sync_templates_for_account``) then
picks up Meta's eventual approved/rejected decision the same way it already
does for templates created directly in Meta Business Manager. Meta stays the
one source of truth for approval; Akilent never marks a template approved
itself.
"""
from __future__ import annotations

import re

from apps.whatsapp.models import MessageTemplate, WhatsAppBusinessNumber
from apps.whatsapp.providers import WhatsAppProviderError, get_whatsapp_provider

_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_VAR_RE = re.compile(r"\{\{(\d+)\}\}")
_URL_RE = re.compile(r"^https?://")


class TemplateBuilderError(Exception):
    """Raised for both local validation failures and Meta rejecting the submission."""


def _extract_variable_numbers(body: str) -> list[int]:
    return [int(n) for n in _VAR_RE.findall(body)]


def _validate_buttons(buttons: list[dict], body_variable_numbers: list[int]) -> None:
    """Button URL variables aren't a separate namespace from the body's — a
    button referencing {{2}} means "the same {{2}} the body already defines"
    (same label/example), giving Akilent one coherent variable system instead
    of Meta's independent per-component numbering.
    """
    if len(buttons) > 1:
        raise TemplateBuilderError("Only one button is supported per template.")
    if not buttons:
        return
    button = buttons[0]
    text = (button.get("text") or "").strip()
    url = (button.get("url") or "").strip()
    example = (button.get("example") or "").strip()

    if not text:
        raise TemplateBuilderError("The button needs text, e.g. \"Visit website\".")
    if len(text) > 25:
        raise TemplateBuilderError("Button text must be 25 characters or fewer.")
    if not url:
        raise TemplateBuilderError("The button needs a URL.")
    if not _URL_RE.match(url):
        raise TemplateBuilderError("The button URL must start with http:// or https://.")

    numbers = _extract_variable_numbers(url)
    if len(numbers) > 1:
        raise TemplateBuilderError("The button URL can contain only one variable.")
    if numbers and numbers[0] not in body_variable_numbers:
        raise TemplateBuilderError("The button URL must use a variable that's already in the message.")
    if numbers and not example:
        raise TemplateBuilderError(
            f"Give an example URL for the button's {{{{{numbers[0]}}}}} — required by WhatsApp for approval."
        )
    if not numbers and example:
        raise TemplateBuilderError("Remove the button's example URL, or reference a message variable in the URL to use it.")


def validate_fields(
    *, name: str, category: str, language: str, body: str,
    variable_labels: list[str], variable_examples: list[str], header: str = "", footer: str = "",
    buttons: list[dict] | None = None,
) -> None:
    if not name or not _NAME_RE.match(name):
        raise TemplateBuilderError(
            "Template name can only use lowercase letters, numbers and underscores "
            "(e.g. payment_reminder)."
        )
    if category not in {c[0] for c in MessageTemplate.Category.choices}:
        raise TemplateBuilderError("Choose a valid category.")
    if not language:
        raise TemplateBuilderError("Choose a language.")
    if not body.strip():
        raise TemplateBuilderError("Write the message body.")

    numbers = _extract_variable_numbers(body)
    if numbers != list(range(1, len(numbers) + 1)):
        raise TemplateBuilderError(
            "Variables must be numbered in order starting at {{1}} — e.g. {{1}}, {{2}}, {{3}}."
        )
    if len(variable_labels) != len(numbers) or len(variable_examples) != len(numbers):
        raise TemplateBuilderError(
            "Every variable needs a label and an example value (used to show agents "
            "what to fill in, and required by WhatsApp for approval)."
        )
    if any(not label.strip() for label in variable_labels):
        raise TemplateBuilderError("Every variable needs a label.")
    if any(not example.strip() for example in variable_examples):
        raise TemplateBuilderError("Every variable needs an example value.")

    _validate_buttons(buttons or [], numbers)


def _build_button(*, text: str, url: str, example: str = "") -> dict:
    btn = {"type": "URL", "text": text.strip(), "url": url.strip()}
    if example.strip():
        btn["example"] = [example.strip()]
    return btn


def build_meta_payload(
    *, name: str, category: str, language: str, body: str,
    variable_examples: list[str], header: str = "", footer: str = "",
    buttons: list[dict] | None = None,
) -> dict:
    """Meta's template-creation schema — the only place this shape is built."""
    components = []
    if header.strip():
        components.append({"type": "HEADER", "format": "TEXT", "text": header.strip()})

    body_component = {"type": "BODY", "text": body.strip()}
    if variable_examples:
        body_component["example"] = {"body_text": [variable_examples]}
    components.append(body_component)

    if footer.strip():
        components.append({"type": "FOOTER", "text": footer.strip()})
    if buttons:
        components.append({"type": "BUTTONS", "buttons": buttons})

    return {
        "name": name,
        "language": language,
        "category": category.upper(),
        "components": components,
    }


def create_and_submit_template(
    account, *, name: str, category: str, language: str, body: str,
    variable_labels: list[str] | None = None, variable_examples: list[str] | None = None,
    header: str = "", footer: str = "", buttons: list[dict] | None = None,
) -> MessageTemplate:
    """Validate, submit to Meta, and store the resulting template locally.

    Raises ``TemplateBuilderError`` for both local validation failures and a
    Meta-side rejection of the submission — the caller doesn't need to
    distinguish the two, both mean "fix the form and try again".
    """
    variable_labels = variable_labels or []
    variable_examples = variable_examples or []
    buttons = buttons or []
    validate_fields(
        name=name, category=category, language=language, body=body,
        variable_labels=variable_labels, variable_examples=variable_examples,
        header=header, footer=footer, buttons=buttons,
    )

    number = (
        WhatsAppBusinessNumber.objects.filter(account=account, is_active=True)
        .exclude(waba_id__isnull=True).exclude(waba_id="").first()
    )
    if number is None:
        raise TemplateBuilderError("Connect a WhatsApp Business number before creating templates.")

    if MessageTemplate.objects.filter(
        account=account, whatsapp_template_name=name, language_code=language
    ).exists():
        raise TemplateBuilderError("A template with that name and language already exists.")

    meta_buttons = [_build_button(**buttons[0])] if buttons else []
    payload = build_meta_payload(
        name=name, category=category, language=language, body=body,
        variable_examples=variable_examples, header=header, footer=footer, buttons=meta_buttons,
    )
    try:
        provider = get_whatsapp_provider(account)
        provider.create_template(number.waba_id, payload)
    except (WhatsAppProviderError, NotImplementedError) as exc:
        raise TemplateBuilderError(f"WhatsApp rejected the template: {exc}") from exc

    return MessageTemplate.objects.create(
        account=account, name=name, whatsapp_template_name=name, language_code=language,
        category=category, approval_status=MessageTemplate.ApprovalStatus.PENDING,
        content=body.strip(), variables=variable_labels, variable_examples=variable_examples,
        header=header.strip(), footer=footer.strip(), buttons=meta_buttons,
    )
