"""Meta's send-time ``components`` for a template message.

Distinct from ``apps.whatsapp.template_builder.build_meta_payload``, which
builds the *creation* schema (BODY/HEADER definitions with approval examples).
This builds the *send* schema: the actual values that fill an approved
template's placeholders.

Variable values arrive keyed by label (``{"Customer name": "Ada"}``) because
that's what an agent fills in, while Meta wants them positionally. The mapping
between the two is ``MessageTemplate.variables``, whose order defines which
label is ``{{1}}``, ``{{2}}``, and so on.
"""
from __future__ import annotations

import re

_VAR_RE = re.compile(r"\{\{(\d+)\}\}")


def _ordered_values(template, params: dict) -> list[str]:
    return [str(params.get(label, "") or "") for label in (template.variables or [])]


def _button_component(template, values: list[str]) -> dict | None:
    """The parameter for a dynamic URL button, if the template has one.

    A URL button's variable reuses the body's numbering — ``{{2}}`` in the
    button URL means the same ``{{2}}`` the body defines (see
    ``template_builder._validate_url_button``) — so its value comes from the
    body values at that position.
    """
    buttons = template.buttons or []
    if not buttons:
        return None
    button = buttons[0]
    kind = (button.get("type") or "").upper()
    if kind == "OTP" and values:
        # An authentication template's copy-code button must carry the same code
        # as the body's {{1}}. Meta names it sub_type "url" even though it copies.
        return {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": values[0]}],
        }
    if kind != "URL":
        return None  # phone and quick-reply buttons carry no send-time parameter
    numbers = _VAR_RE.findall(button.get("url") or "")
    if not numbers:
        return None
    index = int(numbers[0]) - 1
    if index < 0 or index >= len(values):
        return None
    return {
        "type": "button",
        "sub_type": "url",
        "index": "0",
        "parameters": [{"type": "text", "text": values[index]}],
    }


def build_send_components(template, params: dict | None = None) -> list[dict]:
    """Build the ``components`` list for ``provider.send_template``.

    Raises ``ValueError`` when the template needs a media header, since sending
    one requires a publicly reachable media URL (or an uploaded media id) that
    the stored creation handle can't supply.
    """
    params = params or {}
    values = _ordered_values(template, params)
    components: list[dict] = []

    if template.header_format != template.HeaderFormat.TEXT:
        raise ValueError(
            f"Template {template.name!r} has a media header, which can't be sent yet. "
            "Use a template with a text header, or send this one from WhatsApp directly."
        )

    if values:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in values],
        })

    button = _button_component(template, values)
    if button:
        components.append(button)

    return components
