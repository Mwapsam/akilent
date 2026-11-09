"""What needs you: the short action list on Insights and Home.

One list from two sources, with nothing said twice:

- ``reporting.at_risk``: the live counts (missed, waiting, follow-ups due, quiet leads) and the
  set-up gaps (opening hours, automations, AI);
- open ``apps.insights`` observations, minus the ones the live counts already say
  (``COVERED``), with each Instagram/Facebook pair (``FAMILIES``) merged into one line.

Each item is one line with somewhere to act and, where it's known, when the issue started.
Ordered by urgency; the page shows the first ``LIMIT`` and says how many more there are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Min
from django.utils import timezone

LIMIT = 3

# Observations that repeat a live count: waiting 2h+ and waiting 24h+ are "waiting" and
# "missed" in at_risk, counted the inbox's way.
COVERED = frozenset({"unanswered_conversations", "lead_followup_gap"})

# One line for both channels: "4 engaged contacts worth a message (Instagram 3, Facebook 1)".
FAMILIES = {
    "instagram_high_engagement_contact": "engaged",
    "facebook_high_engagement_contact": "engaged",
    "instagram_unanswered_intent": "intent",
    "facebook_unanswered_intent": "intent",
}
_FAMILY_TEXT = {
    "engaged": ("engaged contact worth a message", "engaged contacts worth a message"),
    "intent": (
        "comment asking to buy with no reply in a chat",
        "comments asking to buy with no reply in a chat",
    ),
}
_CHANNEL = {"instagram": "Instagram", "facebook": "Facebook"}

# Set-up gaps from at_risk. Home leaves them out: its Setup section already lists them.
SETUP = frozenset({"hours", "automations", "ai"})

# Lower first. Live problems, then what the observations found, then set-up gaps.
_RANK = {
    "missed": 0,
    "waiting": 1,
    "urgent": 2,
    "followups_due": 3,
    "warning": 4,
    "quiet_leads": 5,
    "opportunity": 6,
    "info": 7,
    "hours": 8,
    "automations": 9,
    "ai": 10,
}


@dataclass
class Item:
    key: str
    text: str
    cta: str
    url: str = ""
    # An observation's action is a POST (it records that the owner acted on it).
    act_pk: int | None = None
    since: datetime | None = None
    tone: str = (
        "neutral"  # coral (losing customers), highlight (to do), neutral (set-up)
    )
    rank: int = 99
    insight_pks: list[int] = field(default_factory=list)


def _live_since(account, now) -> dict[str, datetime | None]:
    """When each live problem started: the longest wait, the oldest due follow-up."""
    from apps.conversations import followups
    from apps.conversations.state import missed, needs_attention

    # Both views are ordered longest-waiting first; last_in is an aggregate, so take the first
    # row's rather than aggregating it again.
    return {
        "missed": missed(account, now).values_list("last_in", flat=True).first(),
        "waiting": needs_attention(account, now)
        .values_list("last_in", flat=True)
        .first(),
        "followups_due": followups._open_followups(account)
        .filter(due_at__lte=now)
        .aggregate(m=Min("due_at"))["m"],
    }


def _from_at_risk(account, at_risk, now) -> list[Item]:
    since = _live_since(account, now) if at_risk["actions"] else {}
    tone = {"missed": "coral", "waiting": "highlight", "followups_due": "highlight"}
    return [
        Item(
            key=a["key"],
            text=a["text"],
            cta=a["cta"],
            url=a["url"],
            since=since.get(a["key"]),
            tone=tone.get(a["key"], "highlight" if a["count"] else "neutral"),
            rank=_RANK.get(a["key"], 99),
        )
        for a in at_risk["actions"]
    ]


def _from_insights(insights) -> list[Item]:
    items: list[Item] = []
    families: dict[str, list] = {}
    for insight in insights:
        if insight.type in COVERED:
            continue
        family = FAMILIES.get(insight.type)
        if family:
            families.setdefault(family, []).append(insight)
            continue
        action = (insight.suggested_action or {}).get("action")
        items.append(
            Item(
                key=f"insight:{insight.type}",
                text=insight.title,
                cta=(insight.suggested_action or {}).get("label") or "See why",
                url="" if action else "#observations",
                act_pk=insight.pk if action else None,
                since=insight.issue_since or insight.created_at,
                tone="coral" if insight.severity == "urgent" else "highlight",
                rank=_RANK.get(insight.severity, 99),
                insight_pks=[insight.pk],
            )
        )
    for family, group in families.items():
        total = sum(i.evidence_count or 0 for i in group) or len(group)
        one, many = _FAMILY_TEXT[family]
        per_channel = ", ".join(
            f"{_CHANNEL[i.type.split('_', 1)[0]]} {i.evidence_count}"
            for i in sorted(group, key=lambda i: i.type)
        )
        text = f"{total} {one if total == 1 else many}"
        if len(group) > 1:
            text += f" ({per_channel})"
        starts = [i.issue_since or i.created_at for i in group]
        best = min(group, key=lambda i: _RANK.get(i.severity, 99))
        items.append(
            Item(
                key=f"family:{family}",
                text=text,
                cta="See them",
                url="#observations",
                since=min(starts),
                tone="highlight",
                rank=_RANK.get(best.severity, 99),
                insight_pks=[i.pk for i in group],
            )
        )
    return items


def attention(
    account, *, now=None, at_risk=None, insights=None, include_setup=True
) -> dict:
    """``{"items": [the first LIMIT Item], "more": int, "total": int}``.

    ``at_risk`` and ``insights`` (open Insight rows) are taken from the caller when it has
    them already, so a page that shows both doesn't count twice. ``include_setup=False``
    leaves out the set-up gaps (Home lists those in its own Setup section).
    """
    from apps.conversations import reporting
    from apps.insights.models import Insight

    now = now or timezone.now()
    at_risk = at_risk if at_risk is not None else reporting.at_risk(account, now)
    if insights is None:
        insights = list(
            Insight.objects.filter(
                account=account,
                status__in=[Insight.Status.NEW, Insight.Status.ACKNOWLEDGED],
            )
        )
    items = _from_at_risk(account, at_risk, now) + _from_insights(insights)
    if not include_setup:
        items = [i for i in items if i.key not in SETUP]
    items.sort(key=lambda i: (i.rank, i.since or now))
    return {
        "items": items[:LIMIT],
        "more": max(0, len(items) - LIMIT),
        "total": len(items),
    }
