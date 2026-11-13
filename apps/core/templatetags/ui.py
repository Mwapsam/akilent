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


@register.simple_tag
def icon_paths(name):
    """The SVG paths of a named icon (apps/core/icons.py), for components/icon.html; nothing
    for a name it doesn't know, as the template's if/elif chain did."""
    from apps.core.icons import ICONS

    return mark_safe(ICONS.get(name, ""))  # nosec B703 B308 - our own constant SVG paths


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


def _workspace_zone(context):
    """The current workspace's time zone, looked up once per request (UTC if none)."""
    from zoneinfo import ZoneInfo

    request = context.get("request")
    cached = getattr(request, "_ak_zone", None) if request is not None else None
    if cached is not None:
        return cached
    zone = ZoneInfo("UTC")
    account = context.get("account")
    if account is None and request is not None and getattr(request, "user", None):
        try:
            from apps.accounts.utils import get_current_account

            account = get_current_account(request)
        except Exception:
            account = None
    if account is not None:
        from apps.conversations.followups import business_zone

        zone = business_zone(account)
    if request is not None:
        request._ak_zone = zone
    return zone


@register.simple_tag(takes_context=True)
def when(context, value):
    """``{% when comment.timestamp %}``: "12 days ago" / "yesterday" / "8 Nov, 13:29", in the
    workspace's time zone, as a <time> with the exact time on hover (apps.core.display)."""
    from django.utils.html import format_html

    from apps.core.display import full_text, when_text

    if not value:
        return ""
    zone = _workspace_zone(context)
    return format_html(
        '<time datetime="{}" title="{}">{}</time>',
        value.isoformat(),
        full_text(value, zone),
        when_text(value, zone),
    )


@register.simple_block_tag
def row_menu(content, label="More actions"):
    """A row's ⋯ menu: ``{% row_menu "Actions for X" %}<a class="row-menu-item">…{% endrow_menu %}``
    (components/_row_menu.html)."""
    return mark_safe(  # nosec B703 B308
        render_to_string(
            "components/_row_menu.html",
            {"slot": mark_safe(content), "label": label},  # nosec B703 B308
        )
    )


@register.simple_tag(takes_context=True)
def marketing_nav(context):
    """The public header's page links before Help and Developers: [(label, url, current)].
    Product and Integrations show only while their hubs have an open page."""
    from apps.accounts import marketing_pages as mp
    from apps.accounts.views import marketing_channels

    ch = context.get("ch") or marketing_channels()
    request = context.get("request")
    path = getattr(request, "path", "")
    items = []
    if mp.visible(mp.PRODUCT, ch):
        items.append(("Product", mp.PREFIX[mp.PRODUCT]))
    if mp.visible(mp.INTEGRATION, ch):
        items.append(("Integrations", mp.PREFIX[mp.INTEGRATION]))
    items.append(("Pricing", "/pricing/"))
    return [(label, url, path.startswith(url)) for label, url in items]


@register.inclusion_tag("public/_site_footer.html", takes_context=True)
def site_footer(context, p="pub", home="/"):
    """The public site's footer contents (company block and link columns), the same on the
    landing page and on every other public page. ``p`` is the shell's class prefix ("lp" or
    "pub"); ``home`` is "" on the landing page, whose section links are in-page anchors."""
    from apps.accounts import marketing_pages as mp
    from apps.accounts.views import marketing_channels

    site = context.get("site")
    ch = context.get("ch") or marketing_channels()
    return {
        "p": p,
        "home": home,
        "ch": ch,
        "product_pages": mp.visible(mp.PRODUCT, ch),
        "integration_pages": mp.visible(mp.INTEGRATION, ch),
        "site": site,
        "app": getattr(site, "app_name", "") or "Akilent",
        "user": context.get("user"),
        "STATUS_PAGE_URL": context.get("STATUS_PAGE_URL", ""),
    }
