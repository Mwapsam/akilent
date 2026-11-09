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
from django.db.models import Count, Exists, OuterRef, Q, Subquery, Sum
from django.db.models.fields import CharField
from django.db.models.fields.json import KT
from django.utils import timezone

from apps.conversations.models import Conversation, InsightGoal, Message
from apps.conversations.state import RESPONSE_STATUSES

ANSWER_WITHIN = timedelta(hours=24)
AUTOMATIC = ("automation", "ai")
ROW_LIMIT = 5000  # conversations per period; a period this busy is sampled, never slow
PEAK_MIN_ENQUIRIES = 20  # below this, "your busiest time" would be noise
PEAK_WIDTH = 2  # hours
DEFAULT_MINUTES_PER_REPLY = 2
PERIODS = (7, 30, 90)
# Below this many, a rate reads "1 of 1" instead of "100%", a median says how few it's from,
# and no "vs before" change is shown: one conversation is an anecdote, not a trend.
MIN_SAMPLE = 5


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


def _outcome(row, now: datetime) -> str:
    """ "answered" (a real reply within 24 hours), "missed" (24 hours passed without one) or
    "pending" (still inside its 24 hours, no reply yet). The one rule behind every "answered",
    "unanswered" and "missed" count on Insights, the Starting point and the weekly report."""
    wait = _wait_seconds(row)
    if wait is not None and wait <= ANSWER_WITHIN.total_seconds():
        return "answered"
    if row["first_in"] + ANSWER_WITHIN <= now:
        return "missed"
    return "pending"


def response(
    account,
    period: Period,
    rows: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """Are we responding fast enough? Median first reply, share answered within 24 hours.

    An enquiry still inside its first 24 hours with no reply isn't counted either way yet
    (``pending``), so a message from an hour ago never reads as unanswered. ``unanswered`` and
    ``missed`` are the same number: no reply within 24 hours.
    """
    now = now or timezone.now()
    rows = first_replies(account, period.start, period.end) if rows is None else rows
    outcomes = [_outcome(r, now) for r in rows]
    answered = outcomes.count("answered")
    missed = outcomes.count("missed")
    waits = [_wait_seconds(r) for r in rows]
    median_seconds = _median(waits)
    return {
        "enquiries": len(rows),
        "answered": answered,
        "unanswered": missed,
        "missed": missed,
        "pending": outcomes.count("pending"),
        "settled": answered + missed,
        "answered_pct": pct(answered, answered + missed),
        # What the median is worked out from, and how many of those first replies were sent
        # automatically: a 4-second median is the automations, and the page says so.
        "replied": sum(1 for w in waits if w is not None),
        "automatic_first_replies": sum(
            1
            for r in rows
            if r["first_reply"] is not None and r["replied_by"] in AUTOMATIC
        ),
        "median_first_reply_seconds": round(median_seconds)
        if median_seconds is not None
        else None,
    }


def resolution(account, period: Period) -> dict:
    """How long a closed conversation took to resolve: median seconds from the
    customer's first message to ``Conversation.closed_at``, for conversations
    closed within the period. Complements ``response()`` — that's how fast the
    first reply was, this is how long the whole thing took."""
    from apps.conversations.state import resolution_seconds_of, with_resolution

    closed = with_resolution(
        Conversation.objects.filter(
            account=account,
            closed_at__isnull=False,
            closed_at__gte=period.start,
            closed_at__lt=period.end,
        )
    )
    samples = [s for c in closed if (s := resolution_seconds_of(c)) is not None]
    median_seconds = _median(samples)
    return {
        "resolved": len(samples),
        "median_resolution_seconds": round(median_seconds)
        if median_seconds is not None
        else None,
    }


def forms(account, period: Period) -> dict:
    """Forms sent in the period and how many customers finished them."""
    from apps.conversations.models import FormResponse

    rows = FormResponse.objects.filter(
        account=account, started_at__gte=period.start, started_at__lt=period.end
    )
    sent = rows.count()
    done = rows.filter(status=FormResponse.Status.COMPLETED).count()
    return {
        "sent": sent,
        "completed": done,
        "completion_pct": round(done / sent * 100) if sent else None,
    }


def recovery(
    account,
    period: Period,
    rows: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """Missed enquiries (no reply within 24 hours, the same rule as ``response``) and what
    became of them.

    One source with ``response``: ``missed`` here is always ``response()["missed"]`` for the same
    period, so the page can't say "1 unanswered" beside "no missed conversations". A missed
    enquiry counts as recovered once the business really replied, however late. Leads and paid
    orders are those credited to a recovered conversation after its 24 hours ran out.
    """
    from apps.commerce.models import Order
    from apps.crm.models import Lead

    now = now or timezone.now()
    rows = first_replies(account, period.start, period.end) if rows is None else rows
    missed_rows = [r for r in rows if _outcome(r, now) == "missed"]
    late = {
        r["id"]: r["first_in"] + ANSWER_WITHIN
        for r in missed_rows
        if r["first_reply"] is not None
    }
    leads = (
        sum(
            1
            for cid, created in Lead.objects.filter(
                account=account, conversation_id__in=list(late)
            ).values_list("conversation_id", "created_at")
            if created >= late[cid]
        )
        if late
        else 0
    )
    paid = (
        sum(
            1
            for cid, paid_at in Order.objects.filter(
                account=account,
                conversation_id__in=list(late),
                status=Order.Status.PAID,
                paid_at__isnull=False,
            ).values_list("conversation_id", "paid_at")
            if paid_at >= late[cid]
        )
        if late
        else 0
    )
    return {
        "missed": len(missed_rows),
        "recovered": len(late),
        "rate_pct": pct(len(late), len(missed_rows)),
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


# ---- instagram attribution funnel -----------------------------------------------------------


def instagram_funnel(account, period: Period) -> dict:
    """Social-commerce funnel for the Instagram channel: comment → DM → lead → order.

    All counts are scoped to the period. Conversion rates use ``pct()`` so a zero denominator
    always returns 0 rather than raising ZeroDivisionError.
    """
    try:
        from apps.instagram.models import CommentThread
    except ImportError:
        return _empty_instagram_funnel()

    threads = CommentThread.objects.filter(
        instagram_account__account=account,
        received_at__gte=period.start,
        received_at__lt=period.end,
    )
    return _comment_funnel(account, period, threads, Conversation.Channel.INSTAGRAM)


def facebook_funnel(account, period: Period) -> dict:
    """The same funnel for Facebook Page comments: comment → Messenger → lead → order."""
    from apps.facebook.models import FacebookCommentThread

    threads = FacebookCommentThread.objects.filter(
        account=account,
        received_at__gte=period.start,
        received_at__lt=period.end,
    )
    return _comment_funnel(account, period, threads, Conversation.Channel.FACEBOOK)


def _comment_funnel(account, period: Period, threads, channel: str) -> dict:
    from apps.commerce.models import Order
    from apps.crm.models import Lead

    comments_received = threads.count()
    threads_triggered = threads.filter(trigger_fired_at__isnull=False).count()
    dms_opened = threads.filter(conversation__isnull=False).count()

    # Credited to a conversation on this channel. Lead.source names how a lead was made
    # ("conversation", "manual"), not the channel, so it can't answer this.
    leads_confirmed = Lead.objects.filter(
        account=account,
        attribution__channel=channel,
        created_at__gte=period.start,
        created_at__lt=period.end,
    ).count()

    paid_qs = Order.objects.filter(
        account=account,
        status=Order.Status.PAID,
        paid_at__gte=period.start,
        paid_at__lt=period.end,
        attribution__channel=channel,
    )
    paid_rows = (
        paid_qs.values("currency")
        .annotate(orders=Count("id"), total=Sum("total"))
        .order_by("currency")
    )
    revenue_rows = [
        {
            "currency": r["currency"],
            "orders": r["orders"],
            "total": str(r["total"] or Decimal("0")),
        }
        for r in paid_rows
    ]
    paid_orders = sum(r["orders"] for r in revenue_rows)

    return {
        "comments_received": comments_received,
        "threads_triggered": threads_triggered,
        "dms_opened": dms_opened,
        "leads_confirmed": leads_confirmed,
        "paid_orders": paid_orders,
        "revenue": revenue_rows,
        "conversion": {
            "comment_to_dm": pct(dms_opened, comments_received),
            "dm_to_lead": pct(leads_confirmed, dms_opened),
            "lead_to_order": pct(paid_orders, leads_confirmed),
        },
    }


def _empty_instagram_funnel() -> dict:
    return {
        "comments_received": 0,
        "threads_triggered": 0,
        "dms_opened": 0,
        "leads_confirmed": 0,
        "paid_orders": 0,
        "revenue": [],
        "conversion": {
            "comment_to_dm": None,
            "dm_to_lead": None,
            "lead_to_order": None,
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
    from apps.conversations import followup_copy, followups
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
        # Customers with a follow-up due for anyone: the Whole team tab's own number.
        "followups_due": followups.due_for_team(account, now=now),
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
                "text": followup_copy.due_count(n, team=True),
                "url": "/inbox/followups/?list=team",
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
    resp_now = response(account, week, rows_now, now=now)
    resp_before = response(account, before, rows_before, now=now)
    median_now = resp_now["median_first_reply_seconds"]
    median_before = resp_before["median_first_reply_seconds"]
    # A change is only shown when both sides have enough replies to mean something.
    comparable = (
        resp_now["replied"] >= MIN_SAMPLE and resp_before["replied"] >= MIN_SAMPLE
    )
    replied_now = customers_replied(account, week)
    rec = recovery(account, week, rows_now, now=now)
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
            "change": change(median_now, median_before) if comparable else None,
            "replied": resp_now["replied"],
            "automatic": resp_now["automatic_first_replies"],
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


def _leads_from_conversations(account, period: Period) -> int:
    from apps.crm.models import Lead

    return Lead.objects.filter(
        account=account,
        conversation__isnull=False,
        created_at__gte=period.start,
        created_at__lt=period.end,
    ).count()


def headline(
    account,
    period: Period,
    rows: list[dict] | None = None,
    *,
    now: datetime | None = None,
    goals: dict | None = None,
) -> dict:
    """The four numbers at the top of Insights, each against the period before it.

    ``{"reply", "answered", "leads", "paid", "sentence", "active"}``. Each figure is
    ``{"value", "before", "change", "comparable", "goal"}``: ``change`` is None unless both
    periods have at least ``MIN_SAMPLE`` to compare (a rate or median from three conversations
    isn't a trend), and ``goal`` is this month's goal row for that metric from
    ``goal_progress``, if one is set. Counts (leads, paid orders) always compare.
    """
    now = now or timezone.now()
    before = period.previous()
    rows = first_replies(account, period.start, period.end) if rows is None else rows
    rows_before = first_replies(account, before.start, before.end)
    resp = response(account, period, rows, now=now)
    resp_before = response(account, before, rows_before, now=now)

    goal_for: dict[str, dict] = {}
    for g in (goals or {}).get("goals", []):
        goal_for.setdefault(g["metric"], g)

    def compare(value, value_before, enough):
        return change(value, value_before) if enough else None

    reply_ok = resp["replied"] >= MIN_SAMPLE and resp_before["replied"] >= MIN_SAMPLE
    answered_ok = resp["settled"] >= MIN_SAMPLE and resp_before["settled"] >= MIN_SAMPLE
    leads_now = _leads_from_conversations(account, period)
    leads_before = _leads_from_conversations(account, before)
    money = revenue(account, period)
    paid_now = sum(r["orders"] for r in money)
    paid_before = sum(r["orders"] for r in revenue(account, before))

    replied = customers_replied(account, period)
    parts = []
    if replied:
        parts.append(f"{replied} customer{'' if replied == 1 else 's'} got a reply")
    if resp["missed"]:
        rec = recovery(account, period, rows, now=now)
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
        "reply": {
            "value": resp["median_first_reply_seconds"],
            "before": resp_before["median_first_reply_seconds"],
            "change": compare(
                resp["median_first_reply_seconds"],
                resp_before["median_first_reply_seconds"],
                reply_ok,
            ),
            "comparable": reply_ok,
            "replied": resp["replied"],
            "automatic": resp["automatic_first_replies"],
            "goal": goal_for.get(InsightGoal.Metric.MEDIAN_FIRST_REPLY),
        },
        "answered": {
            "value": resp["answered_pct"],
            "answered": resp["answered"],
            "settled": resp["settled"],
            "before": resp_before["answered_pct"],
            "change": compare(
                resp["answered_pct"], resp_before["answered_pct"], answered_ok
            ),
            "comparable": answered_ok,
            "goal": goal_for.get(InsightGoal.Metric.ANSWERED_PCT),
        },
        "leads": {
            "value": leads_now,
            "before": leads_before,
            "change": leads_now - leads_before,
            "comparable": True,
            "goal": goal_for.get(InsightGoal.Metric.LEADS),
        },
        "paid": {
            "value": paid_now,
            "before": paid_before,
            "change": paid_now - paid_before,
            "comparable": True,
            "revenue": money,
            "goal": goal_for.get(InsightGoal.Metric.PAID_ORDERS)
            or goal_for.get(InsightGoal.Metric.REVENUE),
        },
        "sentence": oxford_join(parts),
        "active": bool(rows or replied or paid_now or leads_now),
    }


# ---- charts: the chosen period, day by day (week by week for 90 days) -----------------------

WEEKLY_AFTER_DAYS = 31


def trend(
    account,
    period: Period,
    rows: list[dict] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """The period in buckets, for each Insights tab's chart, in the business's time zone.

    Days for 7 and 30 days, weeks (Monday to Sunday) for 90. Same rules as the figures above
    the charts: ``_outcome`` for answered and missed, a median only where there were replies
    (None, a gap in the line, where there weren't: never a made-up 0), leads and paid orders
    from conversations. ``{"weekly", "labels", "reply_minutes", "answered", "missed",
    "leads", "paid", "revenue": {"currency", "values"} | None, "automatic", "people"}``,
    each a list as long as ``labels``. Revenue is the business's main currency (the one with
    the most paid in the period); a second currency is never added to it.
    """
    from datetime import date

    from apps.crm.models import Lead

    now = now or timezone.now()
    rows = first_replies(account, period.start, period.end) if rows is None else rows
    zone = business_zone(account)
    weekly = period.days > WEEKLY_AFTER_DAYS
    first_day = timezone.localtime(period.start, zone).date()
    last_day = timezone.localtime(period.end - timedelta(microseconds=1), zone).date()

    def bucket_start(day: date) -> date:
        return day - timedelta(days=day.weekday()) if weekly else day

    starts: list[date] = []
    day = bucket_start(first_day)
    step = timedelta(days=7 if weekly else 1)
    while day <= last_day:
        starts.append(day)
        day += step
    index = {d: i for i, d in enumerate(starts)}

    def at(moment) -> int | None:
        return index.get(bucket_start(timezone.localtime(moment, zone).date()))

    n = len(starts)
    waits: list[list[float]] = [[] for _ in range(n)]
    answered, missed = [0] * n, [0] * n
    automatic, people = [0] * n, [0] * n
    for row in rows:
        i = at(row["first_in"])
        if i is None:
            continue
        outcome = _outcome(row, now)
        if outcome == "answered":
            answered[i] += 1
        elif outcome == "missed":
            missed[i] += 1
        wait = _wait_seconds(row)
        if wait is not None:
            waits[i].append(wait)
            if row["replied_by"] in AUTOMATIC:
                automatic[i] += 1
            else:
                people[i] += 1

    leads = [0] * n
    for created in Lead.objects.filter(
        account=account,
        conversation__isnull=False,
        created_at__gte=period.start,
        created_at__lt=period.end,
    ).values_list("created_at", flat=True):
        i = at(created)
        if i is not None:
            leads[i] += 1

    paid = [0] * n
    by_currency: dict[str, list[float]] = {}
    for paid_at, currency, total in _paid_from_conversations(
        account, period
    ).values_list("paid_at", "currency", "total"):
        i = at(paid_at)
        if i is None:
            continue
        paid[i] += 1
        by_currency.setdefault(currency or "", [0.0] * n)[i] += float(total or 0)
    main = max(by_currency, key=lambda c: sum(by_currency[c]), default=None)

    def label(d: date) -> str:
        text = f"{d.day} {d.strftime('%b')}"
        return f"w/c {text}" if weekly else text

    return {
        "weekly": weekly,
        "unit": "Week" if weekly else "Day",
        "labels": [label(d) for d in starts],
        "reply_minutes": [round(median(w) / 60, 1) if w else None for w in waits],
        "answered": answered,
        "missed": missed,
        "leads": leads,
        "paid": paid,
        "revenue": {"currency": main, "values": by_currency[main]}
        if main is not None
        else None,
        "automatic": automatic,
        "people": people,
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
    rec = recovery(account, period, rows)
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
        "instagram": instagram_funnel(account, period),
        "facebook": facebook_funnel(account, period),
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
    # The charts run week by week from the first captured week to the last. A week with no
    # snapshot, or no reply to time, is a gap (None), never 0: a 0-minute median would read as
    # the best week on record, and joining the weeks either side would hide that it's missing.
    # A column per week: each captured row, with an empty column for any week missing between
    # two of them. "Missing" is a jump of well over a week, not exactly 7 days, so a clock change
    # moving a week start by an hour isn't taken for a lost week.
    columns: list[tuple[datetime, dict | None]] = []
    for r in rows:
        if columns:
            week = columns[-1][0]
            while r["week_start"] - week > timedelta(days=10):
                week += timedelta(days=7)
                columns.append((week, None))
        columns.append((r["week_start"], r))

    def series(key, scale=1):
        return [
            None if r is None or r.get(key) is None else r[key] / scale
            for _, r in columns
        ]

    # Not "%-d %b": that's a Linux-only strftime extension and breaks on Windows.
    labels = [f"{w.day} {w:%b}" for w, _ in columns]
    return {
        "rows": rows,
        "has_data": bool(rows),
        # One week is a number, not a trend: the charts wait for a second week.
        "enough_for_trend": len(rows) >= 2,
        "last_week": rows[-1]["week_start"] if rows else None,
        "labels": labels,
        "reply_series": [
            ("Median first reply (min)", series("median_first_reply_seconds", 60))
        ],
        "answered_series": [
            ("Answered", series("answered")),
            ("Unanswered", series("unanswered")),
        ],
        "paid_series": [("Paid orders", series("paid_orders"))],
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
