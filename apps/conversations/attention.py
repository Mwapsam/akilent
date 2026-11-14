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
# Home's list is "right now": an observation that started longer ago than this waits behind
# "See all" (the drawer) instead of taking one of the three lines.
FRESH_DAYS = 14

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
    "ad_waiting": 1,
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
    # An observation that can become an automation policy ("Automate", in the drawer).
    automate_pk: int | None = None
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


def short_name(contact) -> str:
    """ "Samuel C.": a first name and an initial, else how the inbox names them."""
    if contact is None:
        return ""
    if contact.first_name and contact.last_name:
        return f"{contact.first_name} {contact.last_name[0]}."
    return contact.display_name


# One of a kind: the line names the person ("Follow-up due · Samuel C."), so it's something
# to do, not the same count the figures above it show.
_ONE = {
    "followups_due": "Follow-up due",
    "waiting": "Waiting for a reply",
    "missed": "No reply for over 24 hours",
}


def _live_who(account, now, keys) -> dict[str, tuple[str, str]]:
    """For each live problem with exactly one case: (who, where to open it)."""
    from django.urls import reverse

    from apps.conversations import followups
    from apps.conversations.state import missed, needs_attention

    out: dict[str, tuple[str, str]] = {}
    if "followups_due" in keys:
        f = (
            followups._open_followups(account)
            .filter(due_at__lte=now)
            .select_related("contact", "conversation")
            .order_by("due_at")
            .first()
        )
        if f is not None:
            url = (
                reverse("conversations:detail", args=[f.conversation.public_id])
                if f.conversation_id
                else "/inbox/followups/?list=team"
            )
            out["followups_due"] = (short_name(f.contact), url)
    for key, view in (("waiting", needs_attention), ("missed", missed)):
        if key in keys:
            c = view(account, now).select_related("contact").first()
            if c is not None:
                out[key] = (
                    short_name(c.contact),
                    reverse("conversations:detail", args=[c.public_id]),
                )
    return out


def _from_at_risk(account, at_risk, now, *, name_people=False) -> list[Item]:
    since = _live_since(account, now) if at_risk["actions"] else {}
    tone = {
        "missed": "coral",
        "ad_waiting": "coral",  # paid for, and waiting
        "waiting": "highlight",
        "followups_due": "highlight",
    }
    single = {
        a["key"] for a in at_risk["actions"] if a["count"] == 1 and a["key"] in _ONE
    }
    who = _live_who(account, now, single) if name_people and single else {}
    items = []
    for a in at_risk["actions"]:
        text, url, cta = a["text"], a["url"], a["cta"]
        if a["key"] in who and who[a["key"]][0]:
            text = f"{_ONE[a['key']]} · {who[a['key']][0]}"
            url, cta = who[a["key"]][1], "Open"
        items.append(
            Item(
                key=a["key"],
                text=text,
                cta=cta,
                url=url,
                since=since.get(a["key"]),
                tone=tone.get(a["key"], "highlight" if a["count"] else "neutral"),
                rank=_RANK.get(a["key"], 99),
            )
        )
    return items


def _automatable() -> frozenset:
    from apps.insights.models import BusinessPolicy

    return frozenset(BusinessPolicy.INSIGHT_TYPE_TO_TRIGGER)


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
                automate_pk=insight.pk if insight.type in _automatable() else None,
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
    account,
    *,
    now=None,
    at_risk=None,
    insights=None,
    include_setup=True,
    home=False,
) -> dict:
    """``{"items": [the first LIMIT Item], "more": int, "total": int}``.

    ``at_risk`` and ``insights`` (open Insight rows) are taken from the caller when it has
    them already, so a page that shows both doesn't count twice. ``include_setup=False``
    leaves out the set-up gaps (Home lists those in its own Setup section).

    ``home``: Home's "Needs you". A live problem with one case names the person ("Follow-up
    due · Samuel C.", opening that conversation), and an observation that started more than
    FRESH_DAYS ago waits in the drawer ("See all") instead of the top three.
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
    items = _from_at_risk(account, at_risk, now, name_people=home) + _from_insights(
        insights
    )
    if not include_setup:
        items = [i for i in items if i.key not in SETUP]
    items.sort(key=lambda i: (i.rank, i.since or now))
    top = items
    if home:
        from datetime import timedelta

        cutoff = now - timedelta(days=FRESH_DAYS)
        # Live problems are right now by definition; observations must be recent.
        top = [
            i
            for i in items
            if not i.key.startswith(("insight:", "family:"))
            or not i.since
            or i.since >= cutoff
        ]
    shown = top[:LIMIT]
    return {
        "items": shown,
        "all_items": items,
        "more": len(items) - len(shown),
        "total": len(items),
    }
