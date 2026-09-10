"""Starter workflow definitions offered when creating a Workflow.

Each entry is a ready-to-edit ``definition`` (see workflow_engine for the shape).
``from_template`` on ``POST /v1/workflows`` copies one of these; the caller is
expected to fill in real ``from`` addresses and template slugs, then publish.
"""
from __future__ import annotations

_FROM = "hello@example.com"  # placeholder — caller must replace before publishing

STARTER_TEMPLATES: dict[str, dict] = {
    "welcome-series": {
        "name": "Welcome series",
        "description": "Three-touch onboarding for a newly created contact.",
        "definition": {
            "trigger": {"type": "contact.created"},
            "steps": [
                {"id": "welcome", "type": "send_email", "from": _FROM,
                 "template": "welcome", "subject": "Welcome aboard", "next": "wait1"},
                {"id": "wait1", "type": "wait", "seconds": 172800, "next": "tips"},
                {"id": "tips", "type": "send_email", "from": _FROM,
                 "template": "getting-started-tips", "subject": "Getting the most out of us",
                 "next": "wait2"},
                {"id": "wait2", "type": "wait", "seconds": 345600, "next": "check"},
                {"id": "check", "type": "branch", "field": "opened_in_last_90d",
                 "operator": "eq", "value": True, "on_true": "done", "on_false": "nudge"},
                {"id": "nudge", "type": "send_email", "from": _FROM,
                 "template": "welcome-nudge", "subject": "Still there?", "next": "done"},
                {"id": "done", "type": "stop"},
            ],
        },
    },
    "re-engagement": {
        "name": "Re-engagement (90 days no open)",
        "description": "Win back contacts with no email opens in the last 90 days.",
        "definition": {
            "trigger": {"type": "manual"},
            "steps": [
                {"id": "gate", "type": "branch", "field": "opened_in_last_90d",
                 "operator": "eq", "value": False, "on_true": "winback", "on_false": "done"},
                {"id": "winback", "type": "send_email", "from": _FROM,
                 "template": "we-miss-you", "subject": "We miss you", "next": "wait"},
                {"id": "wait", "type": "wait", "seconds": 604800, "next": "recheck"},
                {"id": "recheck", "type": "branch", "field": "opened_in_last_90d",
                 "operator": "eq", "value": True, "on_true": "done", "on_false": "last-call"},
                {"id": "last-call", "type": "send_email", "from": _FROM,
                 "template": "last-call", "subject": "Last call", "next": "mark"},
                {"id": "mark", "type": "set_attribute", "key": "reengagement_state",
                 "value": "lapsed", "next": "done"},
                {"id": "done", "type": "stop"},
            ],
        },
    },
    "post-purchase": {
        "name": "Post-purchase",
        "description": "Thank-you, review request, and replenishment nudge after a purchase.",
        "definition": {
            "trigger": {"type": "business_event", "name": "order.completed"},
            "steps": [
                {"id": "thanks", "type": "send_email", "from": _FROM,
                 "template": "order-thank-you", "subject": "Thanks for your order", "next": "wait1"},
                {"id": "wait1", "type": "wait", "seconds": 259200, "next": "review"},
                {"id": "review", "type": "send_email", "from": _FROM,
                 "template": "review-request", "subject": "How did we do?", "next": "wait2"},
                {"id": "wait2", "type": "wait", "seconds": 1814400, "next": "replenish"},
                {"id": "replenish", "type": "send_email", "from": _FROM,
                 "template": "replenishment", "subject": "Running low?", "next": "done"},
                {"id": "done", "type": "stop"},
            ],
        },
    },
}


def list_templates() -> list[dict]:
    return [
        {"id": key, "name": t["name"], "description": t["description"],
         "trigger": t["definition"]["trigger"], "step_count": len(t["definition"]["steps"])}
        for key, t in STARTER_TEMPLATES.items()
    ]


def get_template(key: str) -> dict | None:
    return STARTER_TEMPLATES.get(key)
