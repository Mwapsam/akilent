"""Derived rate metrics from the MessageStatsDaily rollup (Epic P0.4)."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from apps.logs.models import MessageStatsDaily

_MEASURES = ("sent", "delivered", "bounced", "complained", "opened", "clicked", "unique_opens", "unique_clicks")


def totals_for(account, *, domain=None, since_days: int = 30, key_mode: str = "live") -> dict:
    since = (timezone.now() - timedelta(days=since_days)).date()
    qs = MessageStatsDaily.objects.filter(account=account, day__gte=since, key_mode=key_mode)
    if domain is not None:
        qs = qs.filter(domain=domain)
    agg = qs.aggregate(**{m: Sum(m) for m in _MEASURES})
    return {m: (agg.get(m) or 0) for m in _MEASURES}


def rates_for(account, *, domain=None, since_days: int = 30) -> dict:
    """Return delivery/bounce/complaint/open/click rates in [0, 1].

    Rates are over *sent* (what SES accepted). ``volume`` is the sent count so
    callers can suppress noisy percentages on tiny samples.
    """
    t = totals_for(account, domain=domain, since_days=since_days)
    sent = t["sent"] or 0

    def r(n: int) -> float:
        return (n / sent) if sent else 0.0

    return {
        "volume": sent,
        "delivered": t["delivered"],
        "delivery_rate": r(t["delivered"]),
        "bounce_rate": r(t["bounced"]),
        "complaint_rate": r(t["complained"]),
        "open_rate": (t["unique_opens"] / t["delivered"]) if t["delivered"] else 0.0,
        "click_rate": (t["unique_clicks"] / t["delivered"]) if t["delivered"] else 0.0,
    }
