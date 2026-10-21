"""Pacing campaign sends (audit 2026-10-19, Phase 10).

Three things decide *when* a campaign message may go out, on top of the plan
limits and rate limits the send queue already enforces:

- **Meta's daily limit.** A number may start conversations with only so many
  different customers in a rolling 24 hours (its messaging tier). A campaign
  bigger than the room left today is spread over the following days instead of
  failing past the limit.
- **Quiet hours.** The business chooses when campaign messages may arrive
  (default 08:00–20:00 in its own timezone); anything due outside waits for the
  next opening. Replies and one-off messages are never held.
- **Fairness.** One business's 10,000-person campaign can't fill every slot of
  a send pass: each pass shares its batch between the businesses waiting, and
  replies go before campaign messages (``OutboundMessage.priority``).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.whatsapp.models import (
    CampaignSendWindow,
    OutboundMessage,
    WhatsAppBusinessNumber,
)

_DEFAULT_START = time(8, 0)
_DEFAULT_END = time(20, 0)


# --- Quiet hours --------------------------------------------------------------


def send_window(account) -> CampaignSendWindow:
    """The business's window (unsaved defaults if they never changed it)."""
    from django.conf import settings

    return CampaignSendWindow.objects.filter(account=account).first() or (
        CampaignSendWindow(
            account=account,
            start=_DEFAULT_START,
            end=_DEFAULT_END,
            enabled=getattr(settings, "WHATSAPP_CAMPAIGN_QUIET_HOURS_DEFAULT", True),
        )
    )


def _zone(account) -> ZoneInfo:
    from apps.accounts.business_hours import workspace_timezone

    return ZoneInfo(workspace_timezone(account))


def _inside(t: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= t < end
    return t >= start or t < end  # overnight window, e.g. 18:00-06:00


def next_open(dt: datetime, window, zone: ZoneInfo) -> datetime:
    """``dt``, or the next moment the window opens if ``dt`` falls outside it."""
    if window is None or not window.enabled:
        return dt
    local = dt.astimezone(zone)
    if _inside(local.time(), window.start, window.end):
        return dt
    opening = local.replace(
        hour=window.start.hour, minute=window.start.minute, second=0, microsecond=0
    )
    if opening <= local:
        opening = (local + timedelta(days=1)).replace(
            hour=window.start.hour, minute=window.start.minute, second=0, microsecond=0
        )
    return opening.astimezone(ZoneInfo("UTC"))


# --- Meta's daily limit ------------------------------------------------------------


def daily_limit(account) -> int | None:
    """The tightest known messaging limit across the business's active numbers."""
    limits = [
        n.daily_limit
        for n in WhatsAppBusinessNumber.objects.filter(account=account, is_active=True)
        if n.messaging_limit
    ]
    known = [lim for lim in limits if lim is not None]
    return min(known) if known else None


def used_last_24h(account, now=None) -> int:
    """Different customers the business started a conversation with in 24 hours."""
    now = now or timezone.now()
    return (
        OutboundMessage.objects.filter(
            account=account,
            payload__type="template",
            sent_at__gte=now - timedelta(hours=24),
        )
        .values("contact_id")
        .distinct()
        .count()
    )


def send_plan_for(account, recipients: int, now=None) -> dict:
    """How a campaign of ``recipients`` will be spread out, decided when it starts."""
    now = now or timezone.now()
    plan: dict = {"start": now.isoformat()}
    limit = daily_limit(account)
    if limit:
        room = max(limit - used_last_24h(account, now), 0)
        if recipients > room:
            plan.update(limit=limit, room=room)
    window = send_window(account)
    if window.enabled:
        plan["window"] = [window.start.strftime("%H:%M"), window.end.strftime("%H:%M")]
    return plan


def forecast(account, recipients: int, now=None) -> dict:
    """For the composer: will this go out today, or over several days, and when."""
    now = now or timezone.now()
    plan = send_plan_for(account, recipients, now)
    days = 1
    if plan.get("limit"):
        beyond = max(recipients - plan["room"], 0)
        days = 1 + -(-beyond // plan["limit"]) if beyond else 1
    window = send_window(account)
    zone = _zone(account)
    starts = next_open(now, window, zone)
    return {
        "limit": plan.get("limit"),
        "room": plan.get("room"),
        "days": days,
        "waits_for_window": starts > now,
        "starts_local": starts.astimezone(zone),
        "window": window if window.enabled else None,
    }


def scheduled_for(account, plan: dict, index: int, now=None, *, zone=None, window=None):
    """When the ``index``-th message of a campaign (0-based) may go out."""
    now = now or timezone.now()
    base = now
    if plan.get("limit"):
        room, limit = plan.get("room", 0), plan["limit"]
        if index >= room:
            day = 1 + (index - room) // limit
            start = datetime.fromisoformat(plan["start"])
            base = max(now, start + timedelta(days=day))
    if plan.get("window"):
        window = window or send_window(account)
        zone = zone or _zone(account)
        base = next_open(base, window, zone)
    return base


# --- Fair share of one send pass ---------------------------------------------------------


def fair_slice(candidates: list, batch: int) -> list:
    """Up to ``batch`` messages from ``candidates`` (already in priority order),
    no business taking more than its share while others are waiting."""
    if len(candidates) <= batch:
        return candidates
    accounts = {m.account_id for m in candidates}
    share = max(1, batch // max(len(accounts), 1))
    taken: dict = {}
    picked, rest = [], []
    for m in candidates:
        if taken.get(m.account_id, 0) < share:
            taken[m.account_id] = taken.get(m.account_id, 0) + 1
            picked.append(m)
        else:
            rest.append(m)
        if len(picked) == batch:
            return picked
    return (picked + rest)[:batch]
