"""The feature catalog: everything Akilent offers, as permanent keys owned by code.

Code decides what a feature *is*; the database only says which plans include it (``PlanFeature``)
and which businesses got an exception (``AccountFeatureOverride``). Operators can't invent keys,
because a key nobody's code checks would be sold but never enforced.

Rules for keys:
- A key is an API identifier: it's stored in PlanFeature rows, overrides and the audit log.
  ``name``, ``pitch`` and ``group`` are presentation and can change freely.
- A key is never removed or reused. A retired feature stays here with
  ``availability=DEPRECATED``; ``ISSUED_KEYS`` pins every key ever issued and a test enforces it.

Access checks go through ``apps.billing.api`` (``entitled`` / ``usable`` / ``access``), never
through this module or the models directly.
"""
from __future__ import annotations

from dataclasses import dataclass

# access_mode
CORE = "core"   # every business has it; never in the plan matrix, can't be granted or removed
PLAN = "plan"   # sold through PlanFeature; an operator can grant or remove it per business

# availability
SELLABLE = "sellable"      # built and sold: in the plan matrix and on pricing
NOT_SOLD = "not_sold"      # built but deliberately not sold yet: only an operator grant gives it
INTERNAL = "internal"      # platform-only: never in the matrix, on pricing, or overridable
DEPRECATED = "deprecated"  # retired: never entitled or shown; the key is never reused

GROUPS = (
    "Communication", "Customer management", "Automation", "AI", "Sales", "Commerce", "Email",
    "Developer/API", "Support",
)


@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    pitch: str
    group: str
    access_mode: str = PLAN
    availability: str = SELLABLE
    optional: bool = False  # the owner can switch it off in Settings > Optional tools
    nav_paths: tuple = ()   # sidebar links that show a lock when the business isn't entitled


FEATURES: tuple[Feature, ...] = (
    # --- core: every plan ---
    Feature("inbox", "Inbox", "Every customer conversation in one place.", "Communication", CORE),
    Feature("whatsapp", "WhatsApp", "Connect your number, chat with customers and send approved "
            "templates.", "Communication", CORE),
    Feature("customers", "Customers", "One record per customer, built from every conversation.",
            "Customer management", CORE),
    Feature("follow_ups", "Follow-ups", "Never forget to get back to a customer.",
            "Customer management", CORE),
    Feature("message_history", "Message history", "See every message sent and received.",
            "Customer management", CORE),
    Feature("dashboard", "Dashboard", "How your business is doing at a glance.",
            "Customer management", CORE),
    # --- sold through plans ---
    Feature("whatsapp_campaigns", "WhatsApp campaigns",
            "Send an approved WhatsApp message to a list of customers at once.", "Communication"),
    Feature("verification_codes", "Verification codes",
            "Send one-time login codes over WhatsApp from your own system.", "Developer/API"),
    Feature("automations", "Automations",
            "Reminders, follow-ups and replies that run on their own.", "Automation",
            nav_paths=("/automations/", "/build/")),
    Feature("ai_assistant", "AI assistant",
            "Reply suggestions and drafts written from your business's own information.", "AI"),
    Feature("sales", "Sales tracking", "See customers who are considering buying, and where each "
            "one stands.", "Sales", optional=True, nav_paths=("/sales/",)),
    Feature("orders", "Orders & payment links",
            "Record what a customer bought and send them a payment link.", "Commerce",
            optional=True, nav_paths=("/orders/",)),
    Feature("email_sending", "Email sending", "Send email from your own domain, by API or SMTP.",
            "Email"),
    Feature("email_templates", "Email templates", "Reusable email designs, with AI help to write "
            "them.", "Email"),
    Feature("email_campaigns", "Email campaigns", "Send an email to a list of customers at once.",
            "Email"),
    Feature("email_tracking", "Open & click tracking",
            "See who opened your emails and clicked your links.", "Email"),
    Feature("insights", "Detailed insights", "Detailed reports on how your messages perform.",
            "Email"),
    Feature("webhooks", "Webhooks", "Get delivery, open and click events sent to your own system.",
            "Developer/API"),
    Feature("priority_support", "Priority support", "Faster answers from the Akilent team.",
            "Support"),
)

BY_KEY: dict[str, Feature] = {f.key: f for f in FEATURES}

# Every key ever issued. Append only: removing or renaming one fails the catalog test.
ISSUED_KEYS = frozenset({
    "inbox", "whatsapp", "customers", "follow_ups", "message_history", "dashboard",
    "whatsapp_campaigns", "verification_codes", "automations", "ai_assistant", "sales", "orders",
    "email_sending", "email_templates", "email_campaigns", "email_tracking", "insights",
    "webhooks", "priority_support",
})

# The old Plan boolean columns and the feature each became. Only the billing shims and the data
# migration read these names.
LEGACY_FLAGS = {
    "email_apis": "email_sending",
    "email_templates": "email_templates",
    "bulk_email": "email_campaigns",
    "tracking_webhooks": "email_tracking",
    "outbound_webhooks": "webhooks",
    "detailed_analytics": "insights",
    "has_priority_support": "priority_support",
}

# Optional features whose owner on/off switch is stored as a ModuleSubscription row (the
# pre-catalog module names). Only optional features have an owner switch.
OWNER_SWITCH_MODULE = {"sales": "crm", "orders": "commerce"}

# Features that were per-business module switches before plans carried features, so no plan
# ever restricted them. Two things keep that true until the operator sets real tiers:
# - a business with no subscription row at all (possible only when no plan existed at signup)
#   keeps these; setting a plan replaces them with the plan's features;
# - a newly created plan starts with these plus its old capability columns (signals.py).
MODULE_FEATURES = frozenset({
    "whatsapp_campaigns", "verification_codes", "automations", "ai_assistant", "sales", "orders",
})


def get(key: str) -> Feature:
    """The catalog entry for ``key``. Raises KeyError for a key that was never issued."""
    return BY_KEY[key]


def core_keys() -> frozenset[str]:
    return frozenset(f.key for f in FEATURES if f.access_mode == CORE and f.availability != DEPRECATED)


def matrix_features() -> list[Feature]:
    """Features an operator ticks per plan: sold through plans and currently sellable."""
    return [f for f in FEATURES if f.access_mode == PLAN and f.availability == SELLABLE]


def overridable(key: str) -> bool:
    """Whether an operator may grant or remove ``key`` for one business."""
    f = BY_KEY.get(key)
    return bool(f) and f.access_mode == PLAN and f.availability in (SELLABLE, NOT_SOLD)


def plan_assignable(key: str) -> bool:
    """Whether ``key`` may be included in a plan (the matrix)."""
    f = BY_KEY.get(key)
    return bool(f) and f.access_mode == PLAN and f.availability == SELLABLE


def grouped(features) -> list[tuple[str, list[Feature]]]:
    """``features`` in catalog group order, skipping empty groups."""
    return [(g, [f for f in features if f.group == g]) for g in GROUPS
            if any(f.group == g for f in features)]
