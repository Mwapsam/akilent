"""Phase B.2: a deliberately small structured-signal router.

``RoutingRule.conditions`` is matched here — the one place the matching rule
lives, mirroring ``conversations.state``'s own "one place the rules live"
discipline. Only reliable, already-computed facts are supported (channel,
contact tags, an existing open lead/order, new vs. returning customer) — no
AI classification, no free-form scripting, no boolean nesting. A rule with
no matching signal, or an account with no active rules at all, simply
leaves the conversation unassigned: the Unassigned inbox tab is the safety
net this router relies on, not a failure case to special-case around.
"""

from __future__ import annotations

from apps.conversations.models import Conversation, RoutingRule

# The only condition keys a RoutingRule may use. Kept short deliberately —
# see the module docstring.
SIGNAL_KEYS = ("channel", "tag", "is_new_customer", "has_open_lead", "has_open_order")


def _signals(conversation: Conversation) -> dict:
    contact = conversation.contact
    account = conversation.account

    has_prior_conversation = (
        Conversation.objects.filter(account=account, contact=contact)
        .exclude(pk=conversation.pk)
        .exists()
    )

    has_open_lead = False
    try:
        from apps.crm.models import Lead

        has_open_lead = Lead.objects.filter(
            account=account,
            contact=contact,
            status__in=[
                Lead.Status.NEW,
                Lead.Status.CONTACTED,
                Lead.Status.QUALIFIED,
            ],
        ).exists()
    except Exception:
        pass  # crm app not installed/migrated on this account's deployment

    has_open_order = False
    try:
        from apps.commerce.models import Order

        has_open_order = Order.objects.filter(
            account=account,
            contact=contact,
            status__in=[Order.Status.PENDING, Order.Status.AWAITING_PAYMENT],
        ).exists()
    except Exception:
        pass  # commerce app not installed/migrated on this account's deployment

    return {
        "channel": conversation.channel,
        "is_new_customer": not has_prior_conversation,
        "has_open_lead": has_open_lead,
        "has_open_order": has_open_order,
        "tags": set(contact.tags.values_list("slug", flat=True)),
    }


def _matches(conditions: dict, signals: dict) -> bool:
    """All condition keys must match (AND). An empty dict always matches —
    that's how a business sets up a catch-all/default team."""
    for key, expected in conditions.items():
        if key == "tag":
            if expected not in signals["tags"]:
                return False
        elif signals.get(key) != expected:
            return False
    return True


def match_team(conversation: Conversation):
    """The first active RoutingRule (by priority, lowest first) whose
    conditions all match this conversation, or ``None``."""
    signals = _signals(conversation)
    rules = (
        RoutingRule.objects.filter(account=conversation.account, is_active=True)
        .select_related("team")
        .order_by("priority", "id")
    )
    for rule in rules:
        if _matches(rule.conditions, signals):
            return rule.team
    return None
