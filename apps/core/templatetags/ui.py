"""Block tags for the shared UI component layer.

These wrap the canonical chrome (overlay, focus trap, Esc handling, z-index
tokens) so feature templates stop hand-rolling their own modals/menus.

    {% load ui %}

    {% modal "invite" title="Invite a teammate" %}
      <form> … </form>
    {% endmodal %}

Open it from anywhere with:  @click="$dispatch('modal-open', 'invite')"
"""

from __future__ import annotations

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

register = template.Library()

_MODAL_SIZES = {
    "sm": "max-w-sm",
    "md": "max-w-md",
    "lg": "max-w-lg",
    "xl": "max-w-xl",
    "2xl": "max-w-2xl",
}


@register.simple_block_tag
def modal(content, id, title="", size="lg", footer=""):
    """Canonical modal. `id` is the event key used to open it."""
    return mark_safe(  # nosec B703 B308
        render_to_string(
            "components/_modal.html",
            {
                "slot": mark_safe(content),  # nosec B703 B308
                "footer": mark_safe(footer),  # nosec B703 B308
                "id": id,
                "title": title,
                "size_class": _MODAL_SIZES.get(size, _MODAL_SIZES["lg"]),
            },
        )
    )


# Field names that mean a phone number, a person's name or an organisation. Matched
# on the whole name or a _-separated part, so "whatsapp_number" counts and
# "phonetic" does not.
_PHONE_PARTS = {"phone", "whatsapp", "mobile", "tel", "msisdn"}
_AUTOCOMPLETE_BY_NAME = {
    "first_name": "given-name",
    "last_name": "family-name",
    "full_name": "name",
    "company_name": "organization",
    "legal_name": "organization",
    "city": "address-level2",
    "country": "country-name",
    "password": "current-password",
    "password1": "new-password",
    "password2": "new-password",
    "new_password1": "new-password",
    "new_password2": "new-password",
}


def _mobile_hints(bound_field, widget):
    """Attributes that make a phone show the right keyboard and offer autofill.

    Without them every field opened the full letter keyboard - a phone number
    typed on letters, an email with no @ key - and nothing was offered from the
    phone's saved details. Only what can be told from the field itself is set;
    anything the widget already declares wins (render_field skips those keys).
    """
    from django.forms import widgets as w

    name = bound_field.html_name.rsplit("-", 1)[-1].lower()
    parts = set(name.split("_"))
    hints: dict[str, str] = {}
    if isinstance(widget, w.EmailInput):
        hints.update(inputmode="email", autocomplete="email")
    elif isinstance(widget, w.URLInput):
        hints["inputmode"] = "url"
    elif isinstance(widget, w.NumberInput):
        step = str(widget.attrs.get("step", ""))
        hints["inputmode"] = "decimal" if step and step not in ("1", "") else "numeric"
    elif isinstance(widget, w.TextInput) and (parts & _PHONE_PARTS):
        hints.update(inputmode="tel", autocomplete="tel")
    if name in _AUTOCOMPLETE_BY_NAME and "autocomplete" not in hints:
        hints["autocomplete"] = _AUTOCOMPLETE_BY_NAME[name]
    if name in ("q", "search", "query"):
        hints["enterkeyhint"] = "search"
    return hints


_DRAWER_SIZES = {
    "sm": "sm:max-w-sm",
    "md": "sm:max-w-md",
    "lg": "sm:max-w-lg",
    "xl": "sm:max-w-xl",
}


@register.simple_block_tag
def drawer(content, id, title, size="md", footer=""):
    """Side sheet with the modal's contract (same open/close events, same dialog
    stack). Full screen on a phone, a right-hand sheet from sm up."""
    return mark_safe(  # nosec B703 B308
        render_to_string(
            "components/_drawer.html",
            {
                "slot": mark_safe(content),  # nosec B703 B308
                "footer": mark_safe(footer),  # nosec B703 B308
                "id": id,
                "title": title,
                "size_class": _DRAWER_SIZES.get(size, _DRAWER_SIZES["md"]),
            },
        )
    )


@register.simple_tag
def render_field(bound_field, extra_class=""):
    """Render a Django BoundField's widget with the right design-system class
    (.input / .select / .textarea) plus error styling. Checkboxes/radios are
    rendered bare — bound_field.html lays them out."""
    from django.forms import widgets as w

    widget = bound_field.field.widget
    if isinstance(widget, (w.CheckboxInput, w.RadioSelect, w.CheckboxSelectMultiple)):
        base = ""
    elif isinstance(widget, w.Select):
        base = "select"
    elif isinstance(widget, w.Textarea):
        base = "textarea"
    else:
        base = "input"

    existing = widget.attrs.get("class", "")
    css = " ".join(p for p in (existing, base, extra_class) if p)
    attrs = {"class": css} if css else {}
    for key, value in _mobile_hints(bound_field, widget).items():
        if key not in widget.attrs:
            attrs[key] = value
    if bound_field.errors:
        attrs["class"] = (css + " input-error").strip()
        attrs["aria-invalid"] = "true"
        attrs["aria-describedby"] = f"err_{bound_field.html_name}"
    return bound_field.as_widget(attrs=attrs)


@register.simple_block_tag
def dropdown(content, label="", align="right", button_class="btn btn-secondary btn-sm"):
    """Canonical dropdown menu. Put the trigger label in `label` (or pass
    `button_class="unstyled"` and supply your own trigger as the first child
    wrapped in <template x-slot:trigger> … not supported; keep it simple)."""
    import hashlib

    # Generate a stable unique id based on content hash for a11y linkage
    menu_id = (
        "dropdown-"
        + hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:8]
    )
    return mark_safe(  # nosec B703 B308
        render_to_string(
            "components/_dropdown.html",
            {
                "slot": mark_safe(content),  # nosec B703 B308
                "label": label,
                "align_class": "left-0" if align == "left" else "right-0",
                "button_class": button_class,
                "id": menu_id,
            },
        )
    )


@register.filter
def ago(value, now=None):
    """A relative time short enough for a list row: "now", "7m", "5h", "2d".

    Django's ``timesince`` says "5 hours, 5 minutes", which is precise and far
    too long for a phone: in the inbox it ate the width the contact's name
    needed, so names truncated to "+2609710…" to make room for a timestamp.
    Past a week the age stops being the useful fact and the date takes over.

    Pair it with the full date in a ``title``/``datetime`` so the precise value
    is still one hover or one screen-reader announcement away.
    """
    from django.utils import timezone
    from django.utils.formats import date_format

    if not value:
        return ""
    now = now or timezone.now()
    seconds = int((now - value).total_seconds())
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d"
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    same_year = (
        local.year == timezone.localtime(now).year
        if timezone.is_aware(now)
        else local.year == now.year
    )
    return date_format(local, "j M" if same_year else "j M Y")
