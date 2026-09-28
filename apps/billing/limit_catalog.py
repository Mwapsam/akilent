"""The limit catalog: how much of each thing a plan allows, as permanent keys owned by code.

Features answer "can they use it?"; limits answer "how much?". Like feature keys, a limit key is
an API identifier (stored in PlanLimit, overrides, counters, reservations and the audit log) and
is never removed or reused; ``ISSUED_KEYS`` pins them and a test enforces it.

``cost_bearing`` means Akilent directly pays for each unit used (email provider, AI model). Only
those count toward a plan's cost and margin. WhatsApp sends are NOT cost-bearing: Meta bills each
business directly and Akilent must not resell or mark that up, so WhatsApp limits are plan tiers
and abuse protection, not cost protection.

Periods:
- MONTH / DAY: a counter per calendar month / day (UTC), reset by starting a new period;
- TOTAL: how many exist right now (numbers, automations, contacts, seats), counted live;
- PER_USE: a rule checked once per operation (recipients in one campaign), never counted.

When reached:
- HOLD: the operation isn't attempted and is marked "held: limit" (never failed, never lost);
- REFUSE: the action is refused with a message (e.g. "can't connect another number");
- SOFT: counted and warned about only. Used where blocking would lose a customer's message.
"""

from __future__ import annotations

from dataclasses import dataclass

MONTH, DAY, TOTAL, PER_USE = "month", "day", "total", "per_use"
HOLD, REFUSE, SOFT = "hold", "refuse", "soft"
UNLIMITED = -1


@dataclass(frozen=True)
class Limit:
    key: str
    name: str
    unit: str
    period: str
    cost_bearing: bool
    over_limit: str
    default: int = UNLIMITED
    legacy_column: str = ""  # the old Plan column this limit's plan value came from


LIMITS: tuple[Limit, ...] = (
    Limit(
        "conversations_month",
        "Customer conversations",
        "conversations",
        MONTH,
        False,
        SOFT,
        legacy_column="max_conversations_per_month",
    ),
    Limit(
        "whatsapp_numbers",
        "WhatsApp numbers",
        "numbers",
        TOTAL,
        False,
        REFUSE,
        default=1,
        legacy_column="max_whatsapp_numbers",
    ),
    Limit(
        "whatsapp_marketing_msgs",
        "WhatsApp marketing messages",
        "messages",
        MONTH,
        False,
        HOLD,
    ),
    Limit(
        "whatsapp_utility_msgs",
        "WhatsApp utility messages",
        "messages",
        MONTH,
        False,
        HOLD,
    ),
    Limit(
        "verification_codes_month", "Verification codes", "codes", MONTH, False, HOLD
    ),
    Limit(
        "whatsapp_campaign_recipients",
        "Recipients per WhatsApp campaign",
        "recipients",
        PER_USE,
        False,
        REFUSE,
    ),
    Limit(
        "emails_month",
        "Emails",
        "emails",
        MONTH,
        True,
        HOLD,
        legacy_column="max_emails_per_month",
    ),
    Limit("emails_day", "Emails per day", "emails", DAY, True, HOLD),
    Limit(
        "email_campaign_recipients",
        "Recipients per email campaign",
        "recipients",
        PER_USE,
        False,
        REFUSE,
        legacy_column="max_bulk_recipients_per_campaign",
    ),
    Limit(
        "ai_actions_day", "AI actions per day", "actions", DAY, True, HOLD, default=500
    ),
    Limit(
        "automation_rules", "Active automations", "automations", TOTAL, False, REFUSE
    ),
    Limit("contacts", "Customers stored", "customers", TOTAL, False, REFUSE),
    Limit("team_members", "Team members", "people", TOTAL, False, REFUSE),
)

BY_KEY: dict[str, Limit] = {lim.key: lim for lim in LIMITS}

ISSUED_KEYS = frozenset(
    {
        "conversations_month",
        "whatsapp_numbers",
        "whatsapp_marketing_msgs",
        "whatsapp_utility_msgs",
        "verification_codes_month",
        "whatsapp_campaign_recipients",
        "emails_month",
        "emails_day",
        "email_campaign_recipients",
        "ai_actions_day",
        "automation_rules",
        "contacts",
        "team_members",
    }
)

# WhatsApp template category -> the monthly limit a send of that category uses.
TEMPLATE_CATEGORY_LIMIT = {
    "marketing": "whatsapp_marketing_msgs",
    "utility": "whatsapp_utility_msgs",
    "authentication": "verification_codes_month",
}


def get(key: str) -> Limit:
    """The catalog entry for ``key``. Raises KeyError for a key that was never issued."""
    return BY_KEY[key]


def metered() -> list[Limit]:
    """Limits with a per-period counter (month/day)."""
    return [lim for lim in LIMITS if lim.period in (MONTH, DAY)]
