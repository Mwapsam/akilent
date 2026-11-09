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


# ── For the Teams & routing page (Stage 5 rule builder) ─────────────────────


def describe(rule, channel_labels: dict | None = None) -> str:
    """The rule in one sentence: "New conversations on WhatsApp from first-time customers go
    to Sales." Built from the rule's own conditions, so it can't drift from what it does."""
    conditions = rule.conditions or {}
    parts = ["New conversations"]
    channel = conditions.get("channel")
    if channel:
        parts.append(f"on {(channel_labels or {}).get(channel, channel)}")
    who = "first-time customers" if conditions.get("is_new_customer") else ""
    if conditions.get("tag"):
        who = f"{who or 'customers'} tagged “{conditions['tag']}”"
    if who:
        parts.append(f"from {who}")
    extra = []
    if conditions.get("has_open_lead"):
        extra.append("an open lead")
    if conditions.get("has_open_order"):
        extra.append("an open order")
    if extra:
        parts.append("with " + " and ".join(extra))
    if len(parts) == 1:
        return f"Every other new conversation goes to {rule.team.name}."
    return " ".join(parts) + f" go to {rule.team.name}."


def lint(rules) -> dict[int, str]:
    """Rules that can never run, by pk. Conditions are an AND of exact matches, so an
    earlier active rule whose conditions are a subset of a later one's catches every
    conversation the later one would: the later rule never decides anything."""
    active = [r for r in rules if r.is_active]
    warnings: dict[int, str] = {}
    for i, rule in enumerate(active):
        mine = (rule.conditions or {}).items()
        for earlier in active[:i]:
            theirs = (earlier.conditions or {}).items()
            if theirs <= mine:
                if not theirs:
                    why = f"“{earlier.name}” comes first and has no conditions, so it catches every conversation."
                else:
                    why = f"“{earlier.name}” comes first and catches every conversation this one would."
                warnings[rule.pk] = (
                    f"Never runs: {why} Move this rule above it, or change its conditions."
                )
                break
    return warnings
