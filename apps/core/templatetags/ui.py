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

_MODAL_SIZES = {"sm": "max-w-sm", "md": "max-w-md", "lg": "max-w-lg", "xl": "max-w-xl", "2xl": "max-w-2xl"}


@register.simple_block_tag
def modal(content, id, title="", size="lg", footer=""):
    """Canonical modal. `id` is the event key used to open it."""
    return mark_safe(
        render_to_string(
            "components/_modal.html",
            {
                "slot": mark_safe(content),
                "footer": mark_safe(footer),
                "id": id,
                "title": title,
                "size_class": _MODAL_SIZES.get(size, _MODAL_SIZES["lg"]),
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
    if bound_field.errors:
        attrs["class"] = (css + " input-error").strip()
        attrs["aria-invalid"] = "true"
    return bound_field.as_widget(attrs=attrs)


@register.simple_block_tag
def dropdown(content, label="", align="right", button_class="btn btn-secondary btn-sm"):
    """Canonical dropdown menu. Put the trigger label in `label` (or pass
    `button_class="unstyled"` and supply your own trigger as the first child
    wrapped in <template x-slot:trigger> … not supported; keep it simple)."""
    import hashlib
    # Generate a stable unique id based on content hash for a11y linkage
    menu_id = "dropdown-" + hashlib.md5(content.encode()).hexdigest()[:8]
    return mark_safe(
        render_to_string(
            "components/_dropdown.html",
            {
                "slot": mark_safe(content),
                "label": label,
                "align_class": "left-0" if align == "left" else "right-0",
                "button_class": button_class,
                "id": menu_id,
            },
        )
    )
