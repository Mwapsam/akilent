"""Account analytics (Phase 2b, ``cs_analytics``): each channel's own numbers, a day at
a time, beside the per-post numbers and the sales the posts brought.

One ``AccountMetricSnapshot`` per channel per day of the business's own calendar.
Instagram says its numbers can be up to 48 hours late, so the day read is
``SETTLE_DAYS`` back, and a day still missing numbers is read again for a week. Which
metrics to ask for comes from ``metrics.ACCOUNT_REGISTRY``; a number that can't be read
keeps its state and reason, never 0.

Reach and accounts engaged count unique people per day, so they're averaged across a
period, never added up. Views and interactions are added.
"""

from __future__ import annotations

import datetime
import logging

from django.utils import timezone

from apps.accounts.business_hours import workspace_zone as _tz

from .metrics import STATE_REASONS, State, account_metrics_for
from .models import AccountMetricSnapshot, FacebookPageConnection
from .providers.errors import MetaPublishError

logger = logging.getLogger(__name__)

FLAG = "cs_analytics"
SETTLE_DAYS = 2  # Instagram: "data can be delayed up to 48 hours"
RETRY_DAYS = 7  # a day still missing numbers is read again for this long
ADDITIVE = frozenset({"views", "interactions"})
PLATFORMS = ("instagram", "facebook")
WEEKLY_HOUR = 8  # the weekly email goes out on Monday from this hour, local time


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def settled_day(account, now=None) -> datetime.date:
    """The latest day whose numbers have settled, on the business's calendar."""
    now = now or timezone.now()
    return timezone.localtime(now, _tz(account)).date() - datetime.timedelta(
        days=SETTLE_DAYS
    )


def connection(account, platform: str):
    """``(provider, object id)`` for the channel's account numbers, or None."""
    from .providers.facebook import FacebookPublishProvider
    from .providers.instagram import InstagramPublishProvider

    if platform == "instagram":
        ig = account.instagram_accounts.filter(is_active=True).first()
        if ig is None or not ig.access_token or ig.token_expired:
            return None
        return InstagramPublishProvider(
            ig.access_token
        ), ig.instagram_business_account_id
    conn = FacebookPageConnection.objects.filter(
        account=account, status=FacebookPageConnection.Status.ACTIVE
    ).first()
    if conn is None or not conn.access_token:
        return None
    return FacebookPublishProvider(conn.page_id, conn.access_token), conn.page_id


def _bounds(account, day) -> tuple[int, int]:
    tz = _tz(account)
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    return int(start.timestamp()), int((start + datetime.timedelta(days=1)).timestamp())


def collect_day(account, platform: str, day: datetime.date) -> AccountMetricSnapshot:
    """Read one day of a channel's numbers and store (or replace) its snapshot.

    Raises MetricsRateLimited when Meta throttles us; the caller tries again later.
    """
    from .metrics_collector import _raise_if_rate_limited, _state_for

    defs = account_metrics_for(platform)
    values: dict = {m.key: None for m in defs}
    states: dict = {m.key: State.PENDING for m in defs}
    error, error_type = "", ""

    def fail(keys, exc):
        nonlocal error, error_type
        _raise_if_rate_limited(exc)
        for key in keys:
            states[key] = _state_for(exc)
        error = str(exc)[:500]
        error_type = getattr(exc, "error_type", "") or "unknown"

    found = connection(account, platform)
    if found is None:
        for key in states:
            states[key] = State.NEEDS_PERMISSION
        error = "Not connected, or the connection expired."
        error_type = "permission"
    else:
        provider, target = found
        fields = [m for m in defs if m.source == "field"]
        insights = [m for m in defs if m.source == "insights"]
        # Followers is a reading taken now, so it is only stored on the newest settled
        # day; a day read again later keeps what it had.
        if day != settled_day(account):
            earlier = AccountMetricSnapshot.objects.filter(
                account=account, platform=platform, day=day
            ).first()
            for m in fields:
                values[m.key] = (earlier.values or {}).get(m.key) if earlier else None
                states[m.key] = (
                    (earlier.states or {}).get(m.key, State.UNSUPPORTED)
                    if earlier
                    else State.UNSUPPORTED
                )
            fields = []
        if fields:
            try:
                read = (
                    provider.fetch_media_fields
                    if platform == "instagram"
                    else provider.fetch_fields
                )
                data = read(target, [m.graph_name for m in fields])
                for m in fields:
                    if isinstance(data.get(m.graph_name), int):
                        values[m.key] = data[m.graph_name]
                        states[m.key] = State.AVAILABLE
            except Exception as exc:
                fail([m.key for m in fields], exc)
        if insights:
            since, until = _bounds(account, day)

            def fetch(batch):
                if platform == "instagram":
                    return provider.fetch_account_insights(
                        target, [m.graph_name for m in batch], since, until
                    )
                return provider.fetch_page_insights(
                    [m.graph_name for m in batch], since, until
                )

            def store(reported, batch):
                for m in batch:
                    if m.graph_name in reported:
                        values[m.key] = reported[m.graph_name]
                        states[m.key] = State.AVAILABLE

            try:
                store(fetch(insights), insights)
            except MetaPublishError as exc:
                if exc.code != 100:
                    fail([m.key for m in insights], exc)
                else:
                    # An invalid metric names none of them: ask for each on its own.
                    for m in insights:
                        try:
                            store(fetch([m]), [m])
                        except Exception as one:
                            fail([m.key], one)
            except Exception as exc:
                fail([m.key for m in insights], exc)

    snapshot, _ = AccountMetricSnapshot.objects.update_or_create(
        account=account,
        platform=platform,
        day=day,
        defaults={
            "values": values,
            "states": states,
            "error": error,
            "error_type": error_type,
            "collected_at": timezone.now(),
        },
    )
    return snapshot


def _incomplete(snapshot) -> bool:
    """A day still missing a daily number (followers is only read on the newest day)."""
    return any(
        (snapshot.states or {}).get(m.key) in (State.PENDING, State.FAILED)
        for m in account_metrics_for(snapshot.platform)
        if m.source == "insights"
    )


def days_to_collect(account, platform: str, now=None) -> list[datetime.date]:
    """The settled day, plus any of the week before still missing numbers."""
    last = settled_day(account, now)
    days = [last - datetime.timedelta(days=i) for i in range(RETRY_DAYS)]
    have = {
        s.day: s
        for s in AccountMetricSnapshot.objects.filter(
            account=account, platform=platform, day__in=days
        )
    }
    return [d for d in days if d not in have or _incomplete(have[d])]


# --- Reading it back -------------------------------------------------------------------


def summary(account, platform: str, days: int, now=None) -> dict:
    """The channel's numbers over the ``days`` settled days ending at ``settled_day``."""
    end = settled_day(account, now)
    start = end - datetime.timedelta(days=days - 1)
    snaps = list(
        AccountMetricSnapshot.objects.filter(
            account=account, platform=platform, day__gte=start, day__lte=end
        )
    )
    channel = "Instagram" if platform == "instagram" else "Facebook"
    rows = []
    for m in account_metrics_for(platform):
        got = [
            (s.day, s.values.get(m.key))
            for s in snaps
            if isinstance((s.values or {}).get(m.key), int | float)
        ]
        row = {
            "key": m.key,
            "label": m.label,
            "definition": m.definition,
            "days_with_data": len(got),
            "total": None,
            "average": None,
            "first": None,
            "last": None,
            "change": None,
            "reason": "",
        }
        if got:
            numbers = [v for _, v in got]
            if m.key == "followers":
                row["first"], row["last"] = numbers[0], numbers[-1]
                row["change"] = numbers[-1] - numbers[0]
            else:
                row["average"] = round(sum(numbers) / len(numbers))
                if m.key in ADDITIVE:
                    row["total"] = sum(numbers)
        else:
            states = [(s.states or {}).get(m.key) for s in snaps]
            state = next((st for st in states if st), State.PENDING)
            row["reason"] = STATE_REASONS.get(state, "").format(channel=channel)
        rows.append(row)
    return {
        "platform": platform,
        "channel": channel,
        "start": start,
        "end": end,
        "days": days,
        "rows": rows,
        "needs_permission": any(
            st == State.NEEDS_PERMISSION
            for s in snaps
            for st in (s.states or {}).values()
        ),
    }


def daily_rows(account, start: datetime.date, end: datetime.date) -> list[dict]:
    """Every channel's day, metric by metric, for the CSV export. A number that wasn't
    read is blank with its state, never 0."""
    out = []
    for s in AccountMetricSnapshot.objects.filter(
        account=account, day__gte=start, day__lte=end
    ).order_by("day", "platform"):
        for m in account_metrics_for(s.platform):
            value = (s.values or {}).get(m.key)
            out.append(
                {
                    "day": s.day.isoformat(),
                    "channel": s.platform,
                    "metric": m.label,
                    "value": "" if value is None else value,
                    "state": (s.states or {}).get(m.key, State.PENDING),
                }
            )
    return out


# --- The weekly email ------------------------------------------------------------------


def _fmt(value) -> str:
    return "—" if value is None else f"{value:,}"


def weekly_text(account, now=None) -> str:
    """The last 7 days in words: each channel's numbers, then what posts brought in."""
    from apps.conversations import reporting

    from .results import journey, publishing_channels

    lines = []
    for platform in PLATFORMS:
        if platform not in publishing_channels(account):
            continue
        s = summary(account, platform, 7, now)
        lines.append(f"{s['channel']} ({s['start']:%d %b} to {s['end']:%d %b}):")
        for r in s["rows"]:
            if r["key"] == "followers":
                if r["last"] is not None:
                    change = f"{r['change']:+,}" if r["change"] else "no change"
                    lines.append(f"- Followers: {r['last']:,} ({change})")
                continue
            if r["days_with_data"] == 0:
                lines.append(f"- {r['label']}: not available yet")
            elif r["total"] is not None:
                lines.append(f"- {r['label']}: {_fmt(r['total'])} in total")
            else:
                lines.append(f"- {r['label']}: {_fmt(r['average'])} a day on average")
        lines.append("")
    j = journey(account, reporting.last_days(7, now))
    if j:
        lines.append("From your posts in the last 7 days:")
        lines.append(f"- Posts published: {j['posts']}")
        if j["chats"] is not None:
            lines.append(f"- Chats started from a post: {j['chats']}")
            lines.append(f"- Orders credited to a post: {j['orders']}")
            for r in j["revenue"] or []:
                lines.append(f"- Paid ({r['currency']}): {r['total']}")
        lines.append(
            "These count only chats that came through a post's link or a reply to "
            "its comments, so they are the least your posts brought in."
        )
    return "\n".join(lines).strip()


def send_weekly_reports(now=None) -> int:
    from apps.accounts.notifications import notify_team

    from .models import PublishingSettings

    now = now or timezone.now()
    sent = 0
    for row in PublishingSettings.objects.filter(weekly_report=True).select_related(
        "account"
    ):
        account = row.account
        local = timezone.localtime(now, _tz(account))
        if local.weekday() != 0 or local.hour < WEEKLY_HOUR:
            continue
        year, week, _ = local.isocalendar()
        key = f"{year}-W{week:02d}"
        # Claimed in the database, so a second worker or a cache flush can't send twice.
        claimed = (
            PublishingSettings.objects.filter(pk=row.pk)
            .exclude(weekly_report_week=key)
            .update(weekly_report_week=key)
        )
        if not claimed:
            continue  # already sent this week
        text = weekly_text(account, now)
        if not text:
            continue
        try:
            notify_team(
                account,
                to="owners",
                subject="Your week on Instagram and Facebook",
                text=text,
                path="/publishing/analytics/",
                reason=(
                    "You're getting this because you're an Owner or Admin of "
                    f"{account.company_name} and the weekly email is on in Content "
                    "Studio → Analytics."
                ),
            )
            sent += 1
        except Exception:
            logger.exception("weekly report failed for account=%s", account.pk)
            # Give the claim back, so the next hourly run tries again.
            PublishingSettings.objects.filter(pk=row.pk, weekly_report_week=key).update(
                weekly_report_week=row.weekly_report_week
            )
    return sent
