"""Can this automation actually work? Answered in terms of what the owner can fix.

Two kinds of finding, kept apart on purpose:

* **blocking**: it cannot work until fixed (no WhatsApp number, no approved message to send);
* **advice**: it can work, but something the owner probably wants is missing (opening hours for
  an after-hours reply).

Never having run is not a problem: a brand-new automation with zero runs is perfectly ready.
"""

from __future__ import annotations

from django.urls import reverse


def _once(name: str, account, load):
    """Asked once per page: the Automations list checks every starter (11 of them), and
    each asked the same three channel questions again (36 queries on one page)."""
    from apps.core.request_context import remembered

    return remembered(("readiness", name, account.pk), load)


def instagram_connected(account) -> bool:
    """An Instagram account is connected and its channel is switched on."""
    return _once("instagram", account, lambda: _instagram_connected(account))


def facebook_connected(account) -> bool:
    """A Facebook Page is connected for the inbox and its channel is switched on."""
    return _once("facebook", account, lambda: _facebook_connected(account))


def _whatsapp_connected(account) -> bool:
    from apps.whatsapp import api as whatsapp_api

    return whatsapp_api.count_active_business_numbers(account) > 0


def _instagram_connected(account) -> bool:
    from django.apps import apps

    from apps.core.channels import enabled_channels

    if "instagram" not in enabled_channels():
        return False
    model = apps.get_model("instagram", "InstagramBusinessAccount")
    return model.objects.filter(
        account=account, is_active=True, token_expired=False
    ).exists()


def _facebook_connected(account) -> bool:
    from apps.billing import api as billing_api
    from apps.core.channels import enabled_channels
    from apps.facebook.models.page import active_page

    if "facebook" not in enabled_channels():
        return False
    return billing_api.usable(account, "facebook") and active_page(account) is not None


def check(account, starter: dict, *, approved_template_count: int) -> dict:
    """``{"ready": bool, "items": [{"ok", "blocking", "headline", "detail", "fix"}]}`` for one starter."""
    from apps.accounts import business_hours

    items = []
    needs_template = not (
        starter.get("reply")
        or starter.get("menu")
        or starter.get("team")
        or starter.get("qualification")
    )
    whatsapp = _once("whatsapp", account, lambda: _whatsapp_connected(account))
    if needs_template:
        # Approved templates are WhatsApp's; nothing else can send these.
        items.append(
            {
                "ok": whatsapp,
                "blocking": not whatsapp,
                "headline": "WhatsApp is connected"
                if whatsapp
                else "Your WhatsApp number isn't connected yet",
                "detail": ""
                if whatsapp
                else "This one sends an approved WhatsApp message, so it needs WhatsApp.",
                "fix": None
                if whatsapp
                else {"label": "Connect WhatsApp", "url": "/whatsapp/numbers/"},
            }
        )
    else:
        # Replies, menus and questions work in any conversation customers start with you.
        connected = (
            whatsapp or instagram_connected(account) or facebook_connected(account)
        )
        items.append(
            {
                "ok": connected,
                "blocking": not connected,
                "headline": "A messaging channel is connected"
                if connected
                else "Connect WhatsApp, Instagram or Facebook first",
                "detail": ""
                if connected
                else "This answers customers who message you, so connect where they do.",
                "fix": None
                if connected
                else {"label": "Connect a channel", "url": "/onboarding/"},
            }
        )
    if needs_template:
        has = approved_template_count > 0
        items.append(
            {
                "ok": has,
                "blocking": not has,
                "headline": "You have an approved message to send"
                if has
                else "You don't have a message WhatsApp has approved yet",
                "detail": ""
                if has
                else "This automation can't send anything until WhatsApp approves a message for it.",
                "fix": None
                if has
                else {"label": "Create a message", "url": "/whatsapp/templates/new/"},
            }
        )
    if starter.get("needs_hours"):
        configured = business_hours.is_configured(account)
        items.append(
            {
                "ok": configured,
                "blocking": False,
                "headline": "Your opening hours are set"
                if configured
                else "You haven't set your opening hours",
                "detail": ""
                if configured
                else "Without them Akilent treats you as always open, so this reply would never be sent.",
                "fix": None
                if configured
                else {"label": "Set opening hours", "url": reverse("settings-hours")},
            }
        )
    return {"ready": not any(i["blocking"] for i in items), "items": items}
