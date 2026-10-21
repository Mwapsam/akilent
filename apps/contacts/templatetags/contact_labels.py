"""Readable names for values stored on a customer."""

from django import template

register = template.Library()

_SOURCES = {
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
    "api": "API",
    "csv": "CSV import",
    "import": "Import",
    "paste": "Pasted numbers",
    "manual": "Added by hand",
}


@register.filter
def source_label(value) -> str:
    """How a customer arrived, as a person reads it ("whatsapp" → "WhatsApp")."""
    text = (value or "").strip()
    return _SOURCES.get(text.lower(), text[:1].upper() + text[1:])
