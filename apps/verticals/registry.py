"""Phase 5 vertical Starter packs: pre-built module + workflow bundles.

This is configuration on top of Phases 1-4, not new platform code — every
workflow here is expressed purely as a trigger + one or more generic
``action`` steps (Phase 4's Action Registry), the same way a business owner
could build one by hand in the workflow editor. Nothing here is
vertical-specific *infrastructure*; the platform doesn't know "restaurant" or
"real estate" exist, it only knows Contacts/Events/Workflows/Actions/Modules.

Every workflow deliberately avoids steps that need external setup before
they can run (an approved WhatsApp template, a verified sending domain) —
``add_internal_note``/``create_lead``/``create_deal``
work the moment a vertical is activated, so "Activate" is never a lie.
"""

from __future__ import annotations

RESTAURANT = {
    "key": "restaurant",
    "name": "Restaurant",
    "tagline": "Take orders over WhatsApp and bring customers back.",
    "modules": ["orders"],
    "checklist": [
        "Win back customers who haven't ordered in 2 weeks",
    ],
    "workflows": [
        {
            "slug": "restaurant-win-back",
            "name": "Win back repeat customers",
            "definition": {
                "trigger": {"type": "order.paid"},
                "steps": [
                    {"id": "wait", "type": "wait", "seconds": 1209600, "next": "lead"},
                    {
                        "id": "lead",
                        "type": "action",
                        "action": "create_lead",
                        "params": {"source": "win_back"},
                        "next": "done",
                    },
                    {"id": "done", "type": "stop"},
                ],
            },
        },
    ],
}

REAL_ESTATE = {
    "key": "real_estate",
    "name": "Real Estate",
    "tagline": "Turn WhatsApp enquiries into tracked deals.",
    "modules": ["sales"],
    "checklist": [
        "Capture every WhatsApp enquiry as a lead automatically",
        "Move unattended leads into your sales pipeline after a day",
    ],
    "workflows": [
        {
            "slug": "real-estate-capture-enquiry",
            "name": "Capture enquiry as a lead",
            "definition": {
                "trigger": {"type": "conversation.message_received"},
                "steps": [
                    {
                        "id": "lead",
                        "type": "action",
                        "action": "create_lead",
                        "params": {"source": "whatsapp_enquiry"},
                        "next": "done",
                    },
                    {"id": "done", "type": "stop"},
                ],
            },
        },
        {
            "slug": "real-estate-auto-qualify",
            "name": "Auto-qualify after a day",
            "definition": {
                "trigger": {"type": "lead.created"},
                "steps": [
                    {"id": "wait", "type": "wait", "seconds": 86400, "next": "deal"},
                    {
                        "id": "deal",
                        "type": "action",
                        "action": "create_deal",
                        "params": {
                            "lead_id": "context.lead_id",
                            "title": "New enquiry",
                        },
                        "next": "done",
                    },
                    {"id": "done", "type": "stop"},
                ],
            },
        },
    ],
}

VERTICALS: dict[str, dict] = {
    RESTAURANT["key"]: RESTAURANT,  # type: ignore[dict-item]
    REAL_ESTATE["key"]: REAL_ESTATE,  # type: ignore[dict-item]
}


def list_verticals() -> list[dict]:
    return list(VERTICALS.values())


def get_vertical(key: str) -> dict | None:
    return VERTICALS.get(key)
