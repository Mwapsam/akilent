"""Business Health: what Akilent did for a business, measured from what was recorded.

Every number here is a count or a median over stored rows (messages, follow-ups, leads, orders,
attributions), derived on demand and never stored except as immutable weekly snapshots. The
wording rule the templates follow: counts, never causes ("37 missed conversations were answered",
not "Akilent saved 37 customers"). There is deliberately no composite score, and the one estimate
(time saved) always carries its formula.

One definition of "the business replied" everywhere, the inbox's: an outbound message that was
actually sent (``state.RESPONSE_STATUSES``). A queued or failed send is not a reply. A reply's
sender comes from ``Message.metadata["sent_by"]`` ("automation", "ai", "system"; absent for a
person).

Other apps' models are imported inside functions (module boundary rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from django.conf import settings
from django.db.models import Count, Exists, Max, Min, OuterRef, Q, Subquery, Sum
from django.db.models.fields import CharField
from django.db.models.fields.json import KT
from django.utils import timezone

from apps.conversations.models import Conversation, FollowUp, InsightGoal, Message
from apps.conversations.state import RESPONSE_STATUSES

ANSWER_WITHIN = timedelta(hours=24)
AUTOMATIC = ("automation", "ai")
ROW_LIMIT = 5000  # conversations per period; a period this busy is sampled, never slow
PEAK_MIN_ENQUIRIES = 20  # below this, "your busiest time" would be noise
PEAK_WIDTH = 2  # hours
DEFAULT_MINUTES_PER_REPLY = 2
PERIODS = (7, 30, 90)


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime

    @property
    def days(self) -> int:
        return max(1, round((self.end - self.start).total_seconds() / 86400))

    def previous(self) -> Period:
        return Period(self.start - (self.end - self.start), self.start)


def last_days(days: int, now: datetime | None = None) -> Period:
    now = now or timezone.now()
    return Period(now - timedelta(days=days), now)


def week_of(day: datetime) -> Period:
    """The Monday-to-Monday week (server time) containing ``day``."""
    local = timezone.localtime(day)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return Period(monday, monday + timedelta(days=7))


def pct(part: int, whole: int) -> int | None:
    """A whole percentage, or None when there is nothing to measure (never a made-up 0%)."""
    return round(part / whole * 100) if whole else None


def change(now_value, before_value) -> int | float | None:
    """``now - before`` when both exist, else None (no comparison is shown)."""
    if now_value is None or before_value is None:
        return None
    return now_value - before_value


# ---- the shared building block ---------------------------------------------------------------


def _replies(conversation_ref: str = "pk", after: str = "first_in"):
    return Message.objects.filter(
        conversation=OuterRef(conversation_ref),
        direction=Message.Direction.OUTBOUND,
        status__in=RESPONSE_STATUSES,
        timestamp__gte=OuterRef(after),
    ).order_by("timestamp", "id")


def first_replies(account, start: datetime, end: datetime) -> list[dict]:
    """One row per conversation whose customer first wrote in ``[start, end)``, in one query.

    ``{"id", "channel", "contact_id", "assigned_to_id", "first_in", "first_reply", "replied_by"}``;
    ``first_reply`` is the business's first real reply (None if none yet) and ``replied_by`` who
    sent it ("" for a person).
    """
    first_in = (
        Message.objects.filter(
            conversation=OuterRef("pk"), direction=Message.Direction.INBOUND
        )
        .order_by("timestamp", "id")
        .values("timestamp")[:1]
    )
    reply = _replies()
    rows = (
        Conversation.objects.filter(account=account)
        .annotate(first_in=Subquery(first_in))
        .filter(first_in__gte=start, first_in__lt=end)
        .annotate(
            first_reply=Subquery(reply.values("timestamp")[:1]),
            replied_by=Subquery(
                reply.annotate(by=KT("metadata__sent_by")).values("by")[:1],
                output_field=CharField(),
            ),
        )
        .order_by("first_in")
        .values(
            "id",
            "channel",
            "contact_id",
            "assigned_to_id",
            "first_in",
            "first_reply",
            "replied_by",
        )[:ROW_LIMIT]
    )
    out: list[dict] = [dict(row) for row in rows]
    for row in out:
        row["replied_by"] = row["replied_by"] or ""
    return out


def _wait_seconds(row) -> float | None:
    if row["first_reply"] is None:
        return None
    return (row["first_reply"] - row["first_in"]).total_seconds()


def _median(values) -> float | None:
    values = [v for v in values if v is not None]
    return median(values) if values else None


# ---- pillar 1: customer response -------------------------------------------------------------


def response(account, period: Period, rows: list[dict] | None = None) -> dict:
    """Are we responding fast enough? Median first reply, share answered within 24 hours."""
    rows = first_replies(account, period.start, period.end) if rows is None else rows
    waits = [_wait_seconds(r) for r in rows]
    answered = sum(
        1 for w in waits if w is not None and w <= ANSWER_WITHIN.total_seconds()
    )
    median_seconds = _median(waits)
    return {
        "enquiries": len(rows),
        "answered": answered,
        "unanswered": len(rows) - answered,
        "answered_pct": pct(answered, len(rows)),
        "median_first_reply_seconds": round(median_seconds)
        if median_seconds is not None
        else None,
    }


def recovery(account, period: Period) -> dict:
    """Missed conversations (recovery made a follow-up in the period) and what became of them.

    A conversation counts as recovered once the business really replied after the reminder was
    made; a reply before it doesn't count. Leads and paid orders are those credited to a recovered
    conversation after its reminder.
    """
    from apps.commerce.models import Order
    from apps.crm.models import Lead

    reminders = (
        FollowUp.objects.filter(
            account=account,
            source=FollowUp.Source.MISSED,
            conversation__isnull=False,
            created_at__gte=period.start,
            created_at__lt=period.end,
        )
        .values("conversation_id")
        .annotate(reminded_at=Min("created_at"))
    )
    reminded = {r["conversation_id"]: r["reminded_at"] for r in reminders}
    if not reminded:
        return {
            "missed": 0,
            "recovered": 0,
            "rate_pct": None,
            "became_leads": 0,
            "paid_orders": 0,
        }
    # The latest reply, not the first: an earlier reply predates the reminder, and any reply at or
    # after it means the conversation was picked back up.
    latest_reply = dict(
        Message.objects.filter(
            conversation_id__in=list(reminded),
            direction=Message.Direction.OUTBOUND,
            status__in=RESPONSE_STATUSES,
        )
        .values("conversation_id")
        .annotate(latest=Max("timestamp"))
        .values_list("conversation_id", "latest")
    )
    recovered = {
        cid
        for cid, at in reminded.items()
        if latest_reply.get(cid) is not None and latest_reply[cid] >= at
    }
    leads = sum(
        1
        for cid, created in Lead.objects.filter(
            account=account, conversation_id__in=list(recovered)
        ).values_list("conversation_id", "created_at")
        if created >= reminded[cid]
    )
    paid = sum(
        1
        for cid, paid_at in Order.objects.filter(
            account=account,
            conversation_id__in=list(recovered),
            status=Order.Status.PAID,
            paid_at__isnull=False,
        ).values_list("conversation_id", "paid_at")
        if paid_at >= reminded[cid]
    )
    return {
        "missed": len(reminded),
        "recovered": len(recovered),
        "rate_pct": pct(len(recovered), len(reminded)),
        "became_leads": leads,
        "paid_orders": paid,
    }


def business_zone(account):
    """The business's timezone: its opening hours' zone, else the site's."""
    from zoneinfo import ZoneInfo

    from apps.accounts import business_hours

    hours = business_hours.get_hours(account)
    name = (hours.timezone if hours else "") or settings.TIME_ZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def peak_hours(account, period: Period, rows: list[dict] | None = None) -> dict:
    """When new enquiries arrive, by hour of day in the business's timezone.

    ``{"enough", "total", "hours": [24 counts], "start_hour", "end_hour", "share_pct",
    "closed_share_pct"}``. The busiest window is ``PEAK_WIDTH`` hours and may wrap midnight.
    ``closed_share_pct`` is how many of those enquiries arrived while the business was closed
    (None when no hours are set).
    """
    from apps.accounts import business_hours

    rows = first_replies(account, period.start, period.end) if rows is None else rows
    zone = business_zone(account)
    hours = [0] * 24
    for row in rows:
        hours[row["first_in"].astimezone(zone).hour] += 1
    total = len(rows)
    if total < PEAK_MIN_ENQUIRIES:
        return {"enough": False, "total": total, "hours": hours}
    start = max(
        range(24),
        key=lambda h: (sum(hours[(h + i) % 24] for i in range(PEAK_WIDTH)), -h),
    )
    window = {(start + i) % 24 for i in range(PEAK_WIDTH)}
    in_window = [r for r in rows if r["first_in"].astimezone(zone).hour in window]
    opening = business_hours.get_hours(account)
    closed_share = None
    if opening is not None and opening.schedule:
        closed = sum(
            1 for r in in_window if not business_hours.open_in(opening, r["first_in"])
        )
        closed_share = pct(closed, len(in_window))
    return {
        "enough": True,
        "total": total,
        "hours": hours,
        "start_hour": start,
        "end_hour": (start + PEAK_WIDTH) % 24,
        "share_pct": pct(len(in_window), total),
        "closed_share_pct": closed_share,
    }


def team(account, period: Period, rows: list[dict] | None = None) -> list[dict]:
    """Per assignee, for conversations started in the period: how many, median first reply, and
    how many of their customers are waiting now. Sorted so the row that needs attention is first."""
    from django.contrib.auth import get_user_model

    from apps.conversations.state import needs_attention

    rows = first_replies(account, period.start, period.end) if rows is None else rows
    per_user: dict[int, dict] = {}
    for row in rows:
        if row["assigned_to_id"] is None:
            continue
        entry = per_user.setdefault(
            row["assigned_to_id"], {"conversations": 0, "_waits": []}
        )
        entry["conversations"] += 1
        entry["_waits"].append(_wait_seconds(row))
    waiting = dict(
        needs_attention(account)
        .filter(assigned_to__isnull=False)
        .values("assigned_to")
        .annotate(n=Count("id"))
        .values_list("assigned_to", "n")
    )
    users = get_user_model().objects.in_bulk(set(per_user) | set(waiting))
    out = []
    for user_id, user in users.items():
        entry = per_user.get(user_id, {"conversations": 0, "_waits": []})
        m = _median(entry["_waits"])
        out.append(
            {
                "user": user,
                "name": user.get_full_name() or user.get_username(),
                "conversations": entry["conversations"],
                "waiting": waiting.get(user_id, 0),
                "median_first_reply_seconds": round(m) if m is not None else None,
            }
        )
    return sorted(out, key=lambda r: (-r["waiting"], -r["conversations"], r["name"]))


# ---- pillar 2: automation & AI ---------------------------------------------------------------


def minutes_per_reply(account) -> int:
    from apps.conversations.models import InsightSettings

    row = InsightSettings.objects.filter(account=account).first()
    return row.minutes_per_reply if row else DEFAULT_MINUTES_PER_REPLY


def automation_impact(account, period: Period, rows: list[dict] | None = None) -> dict:
    """How much work Akilent handled: first replies sent automatically, customers who wrote while
    the business was closed and were answered before it opened, AI suggestions used, the time
    estimate (with its formula) and which automations were credited with leads and sales."""
    from apps.accounts import business_hours
    from apps.ai import api as ai_api

    rows = first_replies(account, period.start, period.end) if rows is None else rows
    automatic_first = sum(1 for r in rows if r["replied_by"] in AUTOMATIC)
    by_ai = sum(1 for r in rows if r["replied_by"] == "ai")

    hours = business_hours.get_hours(account)
    out_of_hours = None
    if hours is not None and hours.schedule:
        closed_rows = [
            r for r in rows if not business_hours.open_in(hours, r["first_in"])
        ]
        reached = 0
        for r in closed_rows:
            opens = business_hours.next_opening(hours, r["first_in"])
            if r["first_reply"] is not None and (
                opens is None or r["first_reply"] < opens
            ):
                reached += 1
        out_of_hours = {"closed_enquiries": len(closed_rows), "reached": reached}

    automatic_replies = Message.objects.filter(
        account=account,
        direction=Message.Direction.OUTBOUND,
        status__in=RESPONSE_STATUSES,
        timestamp__gte=period.start,
        timestamp__lt=period.end,
        metadata__sent_by__in=list(AUTOMATIC),
    ).count()
    minutes = minutes_per_reply(account)

    # Hidden when AI is off and made no suggestions in the period (nothing to report).
    usage = ai_api.usage_summary(account, since=period.start, until=period.end)
    ai = (
        {**usage, "used_pct": pct(usage["used"], usage["suggested"])}
        if usage["suggested"] or ai_api.is_available(account)
        else None
    )

    return {
        "first_replies_automatic": automatic_first,
        "first_replies_by_ai": by_ai,
        "enquiries": len(rows),
        "out_of_hours": out_of_hours,
        "automatic_replies": automatic_replies,
        "minutes_per_reply": minutes,
        "minutes_saved": automatic_replies * minutes,
        "ai": ai,
        "workflows": workflow_sales(account, period),
    }


def workflow_sales(account, period: Period) -> list[dict]:
    """Per automation: leads it created and paid orders credited to its runs in the period."""
    from apps.commerce.models import Order
    from apps.conversations.models import ConversationAttribution as A

    credited = A.objects.filter(account=account, workflow_run__isnull=False)
    leads = (
        credited.filter(
            lead__isnull=False,
            attributed_at__gte=period.start,
            attributed_at__lt=period.end,
        )
        .values(
            "workflow_run__workflow_id",
            "workflow_run__workflow__name",
            "workflow_run__workflow__slug",
        )
        .annotate(n=Count("id"))
    )
    orders = (
        credited.filter(
            order__status=Order.Status.PAID,
            order__paid_at__gte=period.start,
            order__paid_at__lt=period.end,
        )
        .values(
            "workflow_run__workflow_id",
            "workflow_run__workflow__name",
            "workflow_run__workflow__slug",
            "order__currency",
        )
        .annotate(n=Count("id"), total=Sum("order__total"))
    )
    out: dict[int, dict] = {}

    def entry(row):
        return out.setdefault(
            row["workflow_run__workflow_id"],
            {
                "name": row["workflow_run__workflow__name"],
                "slug": row["workflow_run__workflow__slug"],
                "leads": 0,
                "paid_orders": 0,
                "revenue": [],
            },
        )

    for row in leads:
        entry(row)["leads"] += row["n"]
    for row in orders:
        e = entry(row)
        e["paid_orders"] += row["n"]
        e["revenue"].append(
            {"currency": row["order__currency"], "total": row["total"] or Decimal("0")}
        )
    return sorted(
        out.values(), key=lambda r: (-r["paid_orders"], -r["leads"], r["name"])
    )


# ---- pillar 3: sales -------------------------------------------------------------------------


def _paid_from_conversations(account, period: Period):
    from apps.commerce.models import Order

    return Order.objects.filter(
        account=account,
        conversation__isnull=False,
        status=Order.Status.PAID,
        paid_at__gte=period.start,
        paid_at__lt=period.end,
    )


def revenue(account, period: Period) -> list[dict]:
    """Paid orders from conversations, per currency (never summed across currencies)."""
    rows = (
        _paid_from_conversations(account, period)
        .values("currency")
        .annotate(orders=Count("id"), total=Sum("total"))
        .order_by("currency")
    )
    return [
        {
            "currency": r["currency"],
            "orders": r["orders"],
            "total": r["total"] or Decimal("0"),
        }
        for r in rows
    ]


def sales(account, period: Period) -> dict:
    """Did conversations turn into money? Revenue, leads, paid orders, lead-to-sale and funnel."""
    from apps.commerce.models import Order
    from apps.conversations.models import ConversationAttribution as A
    from apps.crm.models import Deal, Lead

    leads = Lead.objects.filter(
        account=account,
        conversation__isnull=False,
        created_at__gte=period.start,
        created_at__lt=period.end,
    )
    bought = Order.objects.filter(
        account=account,
        contact=OuterRef("contact"),
        status=Order.Status.PAID,
        paid_at__gte=OuterRef("created_at"),
    )
    lead_counts = leads.annotate(_bought=Exists(bought)).aggregate(
        total=Count("id"), bought=Count("id", filter=Q(_bought=True))
    )
    paid = _paid_from_conversations(account, period)
    methods = dict(
        A.objects.filter(order__in=paid)
        .values("method")
        .annotate(n=Count("id"))
        .values_list("method", "n")
    )
    wrote_in = (
        Message.objects.filter(
            account=account,
            direction=Message.Direction.INBOUND,
            timestamp__gte=period.start,
            timestamp__lt=period.end,
        )
        .values("conversation__contact")
        .distinct()
        .count()
    )
    deals = Deal.objects.filter(account=account, conversation__isnull=False)
    return {
        "revenue": revenue(account, period),
        "leads": lead_counts["total"],
        "leads_bought": lead_counts["bought"],
        "lead_to_sale_pct": pct(lead_counts["bought"], lead_counts["total"]),
        "paid_orders": paid.count(),
        "named": methods.get(A.Method.EXPLICIT, 0),
        "recent": methods.get(A.Method.RECENT_CONVERSATION, 0),
        "funnel": {
            "wrote_in": wrote_in,
            "leads": lead_counts["total"],
            "deals": deals.filter(
                created_at__gte=period.start, created_at__lt=period.end
            ).count(),
            "won": deals.filter(
                status=Deal.Status.WON,
                closed_at__gte=period.start,
                closed_at__lt=period.end,
            ).count(),
            "paid": paid.count(),
        },
    }


# ---- pillar 4: channels ----------------------------------------------------------------------

UNATTRIBUTED = "none"


def channels(account, period: Period, rows: list[dict] | None = None) -> list[dict]:
    """Per channel with any activity: conversations started, leads, paid orders, revenue per
    currency; plus paid orders that no conversation led to. Outcomes only, no "best" label."""
    from apps.commerce.models import Order
    from apps.crm.models import Lead

    rows = first_replies(account, period.start, period.end) if rows is None else rows
    labels = dict(Conversation.Channel.choices)
    out: dict[str, dict] = {}

    def entry(channel):
        return out.setdefault(
            channel,
            {
                "channel": channel,
                "label": labels.get(channel, "Not from a conversation"),
                "conversations": 0,
                "leads": 0,
                "paid_orders": 0,
                "revenue": [],
                "attributed": channel != UNATTRIBUTED,
            },
        )

    for row in rows:
        entry(row["channel"])["conversations"] += 1
    for channel, n in (
        Lead.objects.filter(
            account=account,
            conversation__isnull=False,
            created_at__gte=period.start,
            created_at__lt=period.end,
        )
        .values("conversation__channel")
        .annotate(n=Count("id"))
        .values_list("conversation__channel", "n")
    ):
        entry(channel)["leads"] += n
    for order_row in (
        Order.objects.filter(
            account=account,
            status=Order.Status.PAID,
            paid_at__gte=period.start,
            paid_at__lt=period.end,
        )
        .values("conversation__channel", "currency")
        .annotate(n=Count("id"), total=Sum("total"))
        .order_by("currency")
    ):
        e = entry(order_row["conversation__channel"] or UNATTRIBUTED)
        e["paid_orders"] += order_row["n"]
        e["revenue"].append(
            {
                "currency": order_row["currency"],
                "total": order_row["total"] or Decimal("0"),
            }
        )
    ordered = sorted(
        (e for e in out.values() if e["attributed"]),
        key=lambda e: (-e["conversations"], e["label"]),
    )
    if UNATTRIBUTED in out:
        ordered.append(out[UNATTRIBUTED])
    return ordered


# ---- opportunities at risk -------------------------------------------------------------------

QUIET_AFTER = timedelta(days=7)


def at_risk(account, now: datetime | None = None) -> dict:
    """What to do first, as counts with somewhere to act on them.

    ``{"counts": {...}, "actions": [...]}``. The counts feed the dashboard's work queue too, so the
    two can never disagree. Each action is ``{"key", "count", "text", "url", "cta"}``; rows with
    nothing to do are left out.
    """
    from apps.accounts import business_hours
    from apps.ai import api as ai_api
    from apps.automation import api as automation_api
    from apps.conversations.state import missed, needs_attention
    from apps.crm.models import Lead

    now = now or timezone.now()
    open_leads = Lead.objects.filter(
        account=account,
        status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
    )
    recent_talk = Message.objects.filter(
        account=account,
        conversation__contact=OuterRef("contact"),
        timestamp__gte=now - QUIET_AFTER,
    )
    counts = {
        "waiting": needs_attention(account, now).count(),
        "missed": missed(account, now).count(),
        "followups_due": FollowUp.objects.filter(
            account=account, done_at__isnull=True, due_at__lte=now
        ).count(),
        "new_leads": open_leads.filter(status=Lead.Status.NEW).count(),
        "quiet_leads": open_leads.filter(created_at__lte=now - QUIET_AFTER)
        .annotate(talked=Exists(recent_talk))
        .filter(talked=False)
        .count(),
    }

    def plural(n, one, many):
        return one if n == 1 else many

    actions = []
    if counts["missed"]:
        n = counts["missed"]
        actions.append(
            {
                "key": "missed",
                "count": n,
                "text": f"{n} {plural(n, 'customer has', 'customers have')} waited over 24 hours for a reply",
                "url": "/inbox/?view=missed",
                "cta": "Open missed",
            }
        )
    if counts["waiting"]:
        n = counts["waiting"]
        actions.append(
            {
                "key": "waiting",
                "count": n,
                "text": f"{n} {plural(n, 'customer is', 'customers are')} waiting for a reply",
                "url": "/inbox/?view=needs_attention",
                "cta": "Open inbox",
            }
        )
    if counts["followups_due"]:
        n = counts["followups_due"]
        actions.append(
            {
                "key": "followups_due",
                "count": n,
                "text": f"{n} follow-up{plural(n, '', 's')} due",
                "url": "/inbox/followups/",
                "cta": "Open follow-ups",
            }
        )
    if counts["quiet_leads"]:
        n = counts["quiet_leads"]
        actions.append(
            {
                "key": "quiet_leads",
                "count": n,
                "text": f"{n} interested customer{plural(n, '', 's')} not heard from in 7 days",
                "url": _url("crm:lead-list", "/crm/leads/"),
                "cta": "See leads",
            }
        )
    if not business_hours.is_configured(account):
        actions.append(
            {
                "key": "hours",
                "count": None,
                "text": "Opening hours aren't set, so out-of-hours numbers can't be measured",
                "url": "/settings/hours/",
                "cta": "Set your hours",
            }
        )
    if not automation_api.adoption(account)["on"]:
        actions.append(
            {
                "key": "automations",
                "count": None,
                "text": "No automations are on, so every first reply waits for a person",
                "url": _url("automation:list", "/automation/"),
                "cta": "Turn one on",
            }
        )
    reason = ai_api.unavailable_reason(account)
    if reason == "AI suggestions aren't turned on below.":
        actions.append(
            {
                "key": "ai",
                "count": None,
                "text": "AI reply suggestions are included in your plan but switched off",
                "url": "/settings/ai/",
                "cta": "Review AI",
            }
        )
    return {"counts": counts, "actions": actions}


def _url(name: str, fallback: str) -> str:
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse(name)
    except NoReverseMatch:
        return fallback


# ---- the proof bar ---------------------------------------------------------------------------


def customers_replied(account, period: Period) -> int:
    """Customers who wrote in the period and got a real reply after they did, in the period."""
    first_in_period = (
        Message.objects.filter(
            conversation=OuterRef("pk"),
            direction=Message.Direction.INBOUND,
            timestamp__gte=period.start,
            timestamp__lt=period.end,
        )
        .order_by("timestamp")
        .values("timestamp")[:1]
    )
    replied = Message.objects.filter(
        conversation=OuterRef("pk"),
        direction=Message.Direction.OUTBOUND,
        status__in=RESPONSE_STATUSES,
        timestamp__gte=OuterRef("wrote_at"),
        timestamp__lt=period.end,
    )
    return (
        Conversation.objects.filter(account=account)
        .annotate(wrote_at=Subquery(first_in_period))
        .filter(wrote_at__isnull=False)
        .annotate(replied=Exists(replied))
        .filter(replied=True)
        .values("contact")
        .distinct()
        .count()
    )


def proof(account, week: Period | None = None, now: datetime | None = None) -> dict:
    """This week against last week, for the proof bar and the weekly report.

    ``{"week", "replied", "recovered", "paid", "median", "sentence_parts", "sentence", "active"}``.
    Clauses with a zero are left out of the sentence; ``active`` is False when nothing at all
    happened.
    """
    now = now or timezone.now()
    week = week or Period(now - timedelta(days=7), now)
    before = week.previous()
    rows_now = first_replies(account, week.start, week.end)
    rows_before = first_replies(account, before.start, before.end)
    median_now = response(account, week, rows_now)["median_first_reply_seconds"]
    median_before = response(account, before, rows_before)["median_first_reply_seconds"]
    replied_now = customers_replied(account, week)
    rec = recovery(account, week)
    money_now = revenue(account, week)
    money_before = revenue(account, before)
    paid_now = sum(r["orders"] for r in money_now)
    paid_before = sum(r["orders"] for r in money_before)

    parts = []
    if replied_now:
        parts.append(
            f"{replied_now} customer{'' if replied_now == 1 else 's'} got a reply"
        )
    if rec["recovered"]:
        n = rec["recovered"]
        parts.append(
            f"{n} missed conversation{' was' if n == 1 else 's were'} picked back up"
        )
    if paid_now:
        parts.append(
            f"{paid_now} paid order{'' if paid_now == 1 else 's'} came from customer conversations"
        )
    return {
        "week": week,
        "replied": replied_now,
        "median": {
            "seconds": median_now,
            "change": change(median_now, median_before),
        },
        "recovered": rec,
        "paid": {
            "orders": paid_now,
            "change": change(paid_now, paid_before),
            "revenue": money_now,
        },
        "sentence_parts": parts,
        "sentence": oxford_join(parts),
        "active": bool(rows_now or replied_now or paid_now or rec["missed"]),
    }


def oxford_join(parts: list[str]) -> str:
    """ "a" / "a and b" / "a, b, and c" — never built in a template, where the comma logic would
    be one more thing to get wrong per clause count."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


# ---- snapshots -------------------------------------------------------------------------------


def period_metrics(account, start: datetime, end: datetime) -> dict:
    """Everything a weekly snapshot keeps, JSON-safe. A superset of the Starting point's keys."""
    period = Period(start, end)
    rows = first_replies(account, start, end)
    resp = response(account, period, rows)
    rec = recovery(account, period)
    money = revenue(account, period)
    from apps.crm.models import Lead

    median_seconds = resp["median_first_reply_seconds"]
    return {
        "conversations": resp["enquiries"],
        "answered": resp["answered"],
        "unanswered": resp["unanswered"],
        "answered_pct": resp["answered_pct"],
        "median_first_reply_seconds": median_seconds,
        "median_first_reply_minutes": round(median_seconds / 60)
        if median_seconds is not None
        else None,
        "automatic_first_replies": sum(1 for r in rows if r["replied_by"] in AUTOMATIC),
        "missed": rec["missed"],
        "recovered": rec["recovered"],
        "interested": Lead.objects.filter(
            account=account,
            conversation__isnull=False,
            created_at__gte=start,
            created_at__lt=end,
        ).count(),
        "paid_orders": sum(r["orders"] for r in money),
        "revenue": [
            {"currency": r["currency"], "total": str(r["total"])} for r in money
        ],
    }


# ---- pillar 5: business momentum -------------------------------------------------------------


def momentum(account, *, weeks: int = 12) -> dict:
    """Are we improving over time? The most recent captured weeks (``WeeklySnapshot``), shaped for
    a line chart plus a table underneath.

    Two charts only, each combining series that share a unit (a chart mixing minutes and counts
    on one axis would be misleading): median first reply on its own, and answered/unanswered
    conversations together. Revenue is per currency and is never summed onto one axis, so it's a
    table column, not a chart series.
    """
    from apps.conversations import snapshots

    rows = snapshots.trend(account, weeks=weeks)
    # Not "%-d %b": that's a Linux-only strftime extension and breaks on Windows.
    labels = [f"{r['week_start'].day} {r['week_start']:%b}" for r in rows]
    return {
        "rows": rows,
        "has_data": bool(rows),
        "labels": labels,
        "reply_series": [
            (
                "Median first reply (min)",
                [
                    (r["median_first_reply_seconds"] or 0) / 60
                    if r["median_first_reply_seconds"] is not None
                    else 0
                    for r in rows
                ],
            )
        ],
        "answered_series": [
            ("Answered", [r["answered"] for r in rows]),
            ("Unanswered", [r["unanswered"] for r in rows]),
        ],
        "paid_series": [("Paid orders", [r["paid_orders"] for r in rows])],
    }


# ---- goals --------------------------------------------------------------------------------

# The one place a goal metric is defined. "direction" says whether lower or higher is better;
# "paced" metrics are cumulative-for-the-month counts, on track once progress has kept pace with
# how much of the month has passed — a rate (reply time, answered %) is compared with the target
# directly, since there's nothing to "keep pace with" in a rate.
GOAL_METRICS: dict[str, dict[str, object]] = {
    InsightGoal.Metric.MEDIAN_FIRST_REPLY: {
        "label": "Median first reply",
        "unit": "minutes",
        "direction": "lower",
        "paced": False,
        "needs_currency": False,
    },
    InsightGoal.Metric.ANSWERED_PCT: {
        "label": "Conversations answered within 24 hours",
        "unit": "percent",
        "direction": "higher",
        "paced": False,
        "needs_currency": False,
    },
    InsightGoal.Metric.LEADS: {
        "label": "Leads this month",
        "unit": "count",
        "direction": "higher",
        "paced": True,
        "needs_currency": False,
    },
    InsightGoal.Metric.PAID_ORDERS: {
        "label": "Paid orders this month",
        "unit": "count",
        "direction": "higher",
        "paced": True,
        "needs_currency": False,
    },
    InsightGoal.Metric.REVENUE: {
        "label": "Revenue this month",
        "unit": "money",
        "direction": "higher",
        "paced": True,
        "needs_currency": True,
    },
}


def month_to_date(now: datetime | None = None) -> Period:
    """The calendar month containing ``now``, from its 1st to ``now`` (not the whole month —
    goals track progress against a month still in flight)."""
    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return Period(start, now)


def month_elapsed_share(now: datetime | None = None) -> float:
    """How much of the current calendar month has passed, as 0..1. A paced goal is on track once
    its progress has kept up with this."""
    import calendar

    now = now or timezone.now()
    _, days_in_month = calendar.monthrange(now.year, now.month)
    return min(1.0, now.day / days_in_month)


def _goal_actual(account, metric: str, currency: str, period: Period) -> float | None:
    """The metric's current value for ``period``, or None when there's nothing to measure yet."""
    if metric == InsightGoal.Metric.MEDIAN_FIRST_REPLY:
        seconds = response(account, period)["median_first_reply_seconds"]
        return None if seconds is None else seconds / 60
    if metric == InsightGoal.Metric.ANSWERED_PCT:
        pct_value = response(account, period)["answered_pct"]
        return None if pct_value is None else float(pct_value)
    if metric == InsightGoal.Metric.LEADS:
        return float(sales(account, period)["leads"])
    if metric == InsightGoal.Metric.PAID_ORDERS:
        return float(sales(account, period)["paid_orders"])
    if metric == InsightGoal.Metric.REVENUE:
        row = next(
            (r for r in revenue(account, period) if r["currency"] == currency), None
        )
        return float(row["total"]) if row else 0.0
    return None


def goal_progress(account, *, now: datetime | None = None) -> dict:
    """Are we on track? Each active goal's current value against its target, for the calendar
    month to date. ``{"on_track", "total", "goals": [...]}``.

    A goal row: ``{"goal", "metric", "label", "target", "currency", "unit", "actual",
    "progress_pct", "direction", "paced", "on_track"}``. ``on_track`` is None (shown as unknown,
    never guessed) when there's no data yet for a rate metric.
    """
    now = now or timezone.now()
    period = month_to_date(now)
    elapsed_share = month_elapsed_share(now)
    rows = []
    for goal in InsightGoal.objects.filter(account=account).select_related(
        "created_by"
    ):
        info = GOAL_METRICS[goal.metric]
        actual = _goal_actual(account, goal.metric, goal.currency, period)
        target = float(goal.target)
        on_track: bool | None
        if actual is None:
            on_track = None
        elif info["direction"] == "lower":
            on_track = actual <= target
        elif info["paced"]:
            progress = (actual / target) if target else 1.0
            on_track = progress >= elapsed_share
        else:
            on_track = actual >= target
        rows.append(
            {
                "goal": goal,
                "metric": goal.metric,
                "label": info["label"],
                "target": goal.target,
                "currency": goal.currency,
                "unit": info["unit"],
                "actual": actual,
                "progress_pct": min(100, round(actual / target * 100))
                if actual is not None and target
                else None,
                "direction": info["direction"],
                "paced": info["paced"],
                "on_track": on_track,
            }
        )
    return {
        "on_track": sum(1 for r in rows if r["on_track"]),
        "total": len(rows),
        "goals": rows,
    }


# ---- formatting ------------------------------------------------------------------------------


def duration(seconds) -> str:
    """ "45s", "2m 14s", "3h 5m", "2d 4h"; "—" for None."""
    if seconds is None:
        return "—"
    seconds = round(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
