"""Business context helpers — objective→capability mapping and workspace personalisation.

``BusinessContext`` records *who the business is* and *what they want to achieve*.
``BusinessKnowledge`` records *what the business knows* for AI retrieval.

This module owns the canonical objective catalogue, the objective→feature mapping,
and helper functions that keep both models consistent.

Capability evolution path
-------------------------
1. At onboarding, the business selects objectives from ``OBJECTIVES``.
2. ``activate_objective()`` updates ``BusinessContext.capability_profile`` with the
   features recommended for each objective.
3. When an insight fires (e.g. "80% of enquiries outside business hours"), the insight
   engine calls ``activate_objective(context, "automate_support")`` so the profile grows
   without re-running onboarding.
4. ``recommended_features(context)`` flattens the profile into a de-duplicated feature
   list the workspace uses to personalise navigation and dashboard order.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Objective catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Objective:
    key: str
    label: str
    description: str
    # Feature keys from apps.billing.features that this objective recommends.
    # Only valid, non-deprecated feature keys are listed here.
    features: tuple[str, ...]


OBJECTIVES: tuple[Objective, ...] = (
    Objective(
        key="increase_sales",
        label="Increase sales",
        description="Track leads, follow up consistently, and close more deals.",
        features=("sales", "follow_ups", "orders"),
    ),
    Objective(
        key="respond_faster",
        label="Respond to customers faster",
        description="Reply quickly to every enquiry, even outside business hours.",
        features=("ai_assistant", "inbox"),
    ),
    Objective(
        key="retain_customers",
        label="Retain existing customers",
        description="Re-engage customers before they drift away.",
        features=("whatsapp_campaigns", "follow_ups", "customers"),
    ),
    Objective(
        key="automate_support",
        label="Automate common questions",
        description="Let AI handle FAQs so your team focuses on complex conversations.",
        features=("ai_assistant", "automations"),
    ),
    Objective(
        key="run_campaigns",
        label="Run promotional campaigns",
        description="Send broadcast messages to targeted customer segments.",
        features=("whatsapp_campaigns",),
    ),
    Objective(
        key="manage_orders",
        label="Manage orders and payments",
        description="Record orders and send payment links directly in WhatsApp.",
        features=("orders", "sales"),
    ),
)

OBJECTIVES_BY_KEY: dict[str, Objective] = {o.key: o for o in OBJECTIVES}

# ---------------------------------------------------------------------------
# BusinessContext helpers
# ---------------------------------------------------------------------------


def get_or_create_context(account):
    """Return the BusinessContext for *account*, creating it if absent."""
    from apps.accounts.models import BusinessContext

    ctx, _ = BusinessContext.objects.get_or_create(account=account)
    return ctx


def activate_objective(context, objective_key: str) -> bool:
    """Add *objective_key* to *context* and record its recommended features.

    Idempotent: calling twice with the same key is safe.
    Returns True if the context was modified (and saved), False if already present.
    """
    obj = OBJECTIVES_BY_KEY.get(objective_key)
    if obj is None:
        raise ValueError(f"Unknown objective key: {objective_key!r}")

    objectives: list = list(context.objectives or [])
    profile: dict = dict(context.capability_profile or {})

    if objective_key in objectives:
        return False

    objectives.append(objective_key)
    profile[objective_key] = list(obj.features)

    context.objectives = objectives
    context.capability_profile = profile
    context.save(update_fields=["objectives", "capability_profile", "updated_at"])
    return True


def deactivate_objective(context, objective_key: str) -> bool:
    """Remove *objective_key* from *context*.

    Returns True if the context was modified, False if the key was not present.
    """
    objectives: list = list(context.objectives or [])
    profile: dict = dict(context.capability_profile or {})

    if objective_key not in objectives:
        return False

    objectives.remove(objective_key)
    profile.pop(objective_key, None)

    context.objectives = objectives
    context.capability_profile = profile
    context.save(update_fields=["objectives", "capability_profile", "updated_at"])
    return True


def recommended_features(context) -> list[str]:
    """Flat, de-duplicated list of feature keys recommended for *context*'s objectives.

    Preserves insertion order (Python 3.7+ dict semantics).
    """
    seen: set[str] = set()
    result: list[str] = []
    for features in (context.capability_profile or {}).values():
        for key in features:
            if key not in seen:
                seen.add(key)
                result.append(key)
    return result


# ---------------------------------------------------------------------------
# BusinessKnowledge helpers
# ---------------------------------------------------------------------------


def get_or_create_knowledge(account):
    """Return the BusinessKnowledge for *account*, creating it if absent."""
    from apps.accounts.models import BusinessKnowledge

    knowledge, _ = BusinessKnowledge.objects.get_or_create(account=account)
    return knowledge


def is_knowledge_useful(knowledge) -> bool:
    """True if the business has filled in enough for AI retrieval to be meaningful."""
    return bool(
        knowledge.who_is_it_for
        or knowledge.problem_solved
        or knowledge.common_questions
        or knowledge.faqs
    )


# ---------------------------------------------------------------------------
# Workspace personalisation
# ---------------------------------------------------------------------------

# Dashboard section order per business type. The first matching key in
# BusinessContext.objectives wins. Falls back to DEFAULT.
_DASHBOARD_ORDER: dict[str, list[str]] = {
    "increase_sales": ["sales", "customers", "inbox", "campaigns"],
    "retain_customers": ["customers", "campaigns", "inbox", "sales"],
    "run_campaigns": ["campaigns", "customers", "inbox", "sales"],
    "respond_faster": ["inbox", "customers", "sales", "campaigns"],
    "automate_support": ["inbox", "customers", "sales", "campaigns"],
    "manage_orders": ["orders", "customers", "sales", "inbox"],
    "DEFAULT": ["inbox", "customers", "sales", "campaigns"],
}


def dashboard_section_order(context) -> list[str]:
    """Preferred dashboard section order for this business's primary objective."""
    for objective in context.objectives or []:
        if objective in _DASHBOARD_ORDER:
            return _DASHBOARD_ORDER[objective]
    return _DASHBOARD_ORDER["DEFAULT"]
