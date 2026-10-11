"""Simple allowlist-based template renderer for workflow step text.

``render_template(text, context, *, step_id="")`` replaces ``{variable}``
placeholders from a fixed allowlist.  Any unknown placeholder raises
``TemplateRenderError`` so the caller can route to ``on_error``.

Allowed placeholders:
  {contact.first_name}  {contact.last_name}  {contact.phone}
  {lead.status}
  {product.name}        {product.price}       {product.currency}
  {business.name}
"""

from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

_ALLOWLIST: frozenset[str] = frozenset(
    [
        "contact.first_name",
        "contact.last_name",
        "contact.phone",
        "lead.status",
        "product.name",
        "product.price",
        "product.currency",
        "business.name",
    ]
)


class TemplateRenderError(ValueError):
    """One or more ``{placeholder}`` values are not in the allowlist or context."""

    def __init__(self, missing: list[str], *, step_id: str = "") -> None:
        self.missing = missing
        self.step_id = step_id
        names = ", ".join(f"{{{m}}}" for m in missing)
        prefix = f"step {step_id!r}: " if step_id else ""
        super().__init__(f"{prefix}unknown or missing template variable(s): {names}")


def render_template(text: str, context: dict, *, step_id: str = "") -> str:
    """Replace ``{variable}`` placeholders in *text* using *context*.

    Raises ``TemplateRenderError`` if a placeholder is not in the allowlist
    or has no value in *context*.
    """
    if not text or "{" not in text:
        return text or ""

    missing: list[str] = []
    placeholders = _PLACEHOLDER_RE.findall(text)

    for name in placeholders:
        if name not in _ALLOWLIST or name not in context:
            missing.append(name)

    if missing:
        raise TemplateRenderError(missing, step_id=step_id)

    def _replace(m: re.Match) -> str:
        return str(context[m.group(1)])

    return _PLACEHOLDER_RE.sub(_replace, text)
