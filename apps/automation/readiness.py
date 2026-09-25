"""Can this automation actually work? Answered in terms of what the owner can fix.

Two kinds of finding, kept apart on purpose:

* **blocking**: it cannot work until fixed (no WhatsApp number, no approved message to send);
* **advice**: it can work, but something the owner probably wants is missing (opening hours for
  an after-hours reply).

Never having run is not a problem: a brand-new automation with zero runs is perfectly ready.
"""
from __future__ import annotations

from django.urls import reverse


def check(account, starter: dict, *, approved_template_count: int) -> dict:
    """``{"ready": bool, "items": [{"ok", "blocking", "headline", "detail", "fix"}]}`` for one starter."""
    from apps.accounts import business_hours
    from apps.whatsapp import api as whatsapp_api

    items = []
    connected = whatsapp_api.count_active_business_numbers(account) > 0
    items.append({
        "ok": connected, "blocking": not connected,
        "headline": "WhatsApp is connected" if connected else "Your WhatsApp number isn't connected yet",
        "detail": "" if connected else "Nothing can be sent until you connect the number your customers message.",
        "fix": None if connected else {"label": "Connect WhatsApp", "url": "/whatsapp/numbers/"},
    })
    needs_template = not (
        starter.get("reply") or starter.get("menu") or starter.get("team"))
    if needs_template:
        has = approved_template_count > 0
        items.append({
            "ok": has, "blocking": not has,
            "headline": "You have an approved message to send" if has else "You don't have a message WhatsApp has approved yet",
            "detail": "" if has else "This automation can't send anything until WhatsApp approves a message for it.",
            "fix": None if has else {"label": "Create a message", "url": "/whatsapp/templates/new/"},
        })
    if starter.get("needs_hours"):
        configured = business_hours.is_configured(account)
        items.append({
            "ok": configured, "blocking": False,
            "headline": "Your opening hours are set" if configured else "You haven't set your opening hours",
            "detail": "" if configured else "Without them Akilent treats you as always open, so this reply would never be sent.",
            "fix": None if configured else {"label": "Set opening hours", "url": reverse("settings-hours")},
        })
    return {"ready": not any(i["blocking"] for i in items), "items": items}
