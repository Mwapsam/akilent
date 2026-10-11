"""Workstream F analytics: pipeline history metrics backed by StageTransition.

All range-based metrics accept naive-datetime boundaries (account local time)
or UTC-aware datetimes. The helpers convert to UTC for DB queries via
``zoneinfo`` + the account's ``timezone`` field (falls back to UTC if absent).
"""

from __future__ import annotations

from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone


def _to_utc(dt, account_tz: ZoneInfo):
    """Return a UTC-aware datetime from ``dt``.

    If ``dt`` is already aware, return as-is (converted to UTC). If naive,
    treat it as account-local and make it aware.
    """
    if dt is None:
        return None
    if timezone.is_aware(dt):
        import datetime as _dt

        return dt.astimezone(_dt.UTC)
    return dt.replace(tzinfo=account_tz).astimezone(ZoneInfo("UTC"))


def _account_tz(account) -> ZoneInfo:
    tz_str = getattr(account, "timezone", None) or "UTC"
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lead_conversion(account, start, end, *, to_stage="qualified"):
    """Of leads created in [start, end), the share that ever reached ``to_stage`` by ``end``.

    Returns {"denominator": int, "numerator": int, "rate": float | None}
    """
    from apps.crm.models import StageTransition

    tz = _account_tz(account)
    start_utc = _to_utc(start, tz)
    end_utc = _to_utc(end, tz)

    # Creation rows: from_value="" in [start, end)
    creation_qs = StageTransition.objects.filter(
        account=account,
        lead__isnull=False,
        from_value="",
        occurred_at__gte=start_utc,
        occurred_at__lt=end_utc,
    )
    denominator = creation_qs.count()
    if denominator == 0:
        return {"denominator": 0, "numerator": 0, "rate": None}

    lead_ids = creation_qs.values_list("lead_id", flat=True)

    numerator = (
        StageTransition.objects.filter(
            account=account,
            lead_id__in=lead_ids,
            to_value=to_stage,
            occurred_at__lt=end_utc,
        )
        .values("lead_id")
        .distinct()
        .count()
    )

    return {
        "denominator": denominator,
        "numerator": numerator,
        "rate": numerator / denominator,
    }


def time_in_stage(account, stage_value, *, lead_or_deal="lead"):
    """Entry → next transition durations for each lead/deal in ``stage_value``.

    Returns {"closed": [{"id": int, "duration_seconds": float}],
             "open":   [{"id": int, "age_seconds": float}]}
    """
    from apps.crm.models import StageTransition

    is_lead = lead_or_deal == "lead"
    filter_key = "lead__isnull" if is_lead else "deal__isnull"
    id_field = "lead_id" if is_lead else "deal_id"

    # Rows that enter stage_value (to_value=stage_value)
    entries = StageTransition.objects.filter(
        account=account,
        to_value=stage_value,
        **{filter_key: False},
    ).order_by(id_field, "occurred_at")

    now = timezone.now()
    closed = []
    open_ = []

    for entry in entries:
        obj_id = getattr(entry, id_field)
        # Next transition out of this stage
        next_tr = (
            StageTransition.objects.filter(
                account=account,
                **{id_field: obj_id},
                from_value=stage_value,
                occurred_at__gt=entry.occurred_at,
            )
            .order_by("occurred_at")
            .first()
        )
        if next_tr is not None:
            duration = (next_tr.occurred_at - entry.occurred_at).total_seconds()
            closed.append({"id": obj_id, "duration_seconds": duration})
        else:
            age = (now - entry.occurred_at).total_seconds()
            open_.append({"id": obj_id, "age_seconds": age})

    return {"closed": closed, "open": open_}


def win_rate(account, start, end):
    """Deals whose final state within [start, end) is won or lost.

    A deal's "state as of range end" = its latest StageTransition with
    occurred_at <= end. We count it iff that transition's occurred_at >= start
    AND its to_value matches a won/lost stage name.

    Returns {"won": int, "lost": int, "rate": float | None}
    """
    from apps.crm.models import Stage, StageTransition

    tz = _account_tz(account)
    start_utc = _to_utc(start, tz)
    end_utc = _to_utc(end, tz)

    # Collect won/lost stage names for this account's pipelines
    won_names = set(
        Stage.objects.filter(pipeline__account=account, is_won=True).values_list(
            "name", flat=True
        )
    )
    lost_names = set(
        Stage.objects.filter(pipeline__account=account, is_lost=True).values_list(
            "name", flat=True
        )
    )
    # Also treat Deal.Status choices
    won_names.add("won")
    lost_names.add("lost")

    # For each deal, find its latest transition up to end
    deal_ids = (
        StageTransition.objects.filter(
            account=account,
            deal__isnull=False,
            occurred_at__lte=end_utc,
        )
        .values_list("deal_id", flat=True)
        .distinct()
    )

    won_count = 0
    lost_count = 0
    for deal_id in deal_ids:
        latest = (
            StageTransition.objects.filter(
                account=account,
                deal_id=deal_id,
                occurred_at__lte=end_utc,
            )
            .order_by("-occurred_at")
            .first()
        )
        if latest is None:
            continue
        if latest.occurred_at < start_utc:
            continue
        if latest.to_value in won_names:
            won_count += 1
        elif latest.to_value in lost_names:
            lost_count += 1

    total = won_count + lost_count
    return {
        "won": won_count,
        "lost": lost_count,
        "rate": won_count / total if total > 0 else None,
    }


def pipeline_value(account):
    """Current open deals, per currency, no FX conversion.

    Returns [{"currency": str, "total": Decimal}]
    """
    from django.db.models import Sum

    from apps.crm.models import Deal

    rows = (
        Deal.objects.filter(account=account, status=Deal.Status.OPEN)
        .values("currency")
        .annotate(total=Sum("value"))
        .order_by("currency")
    )
    return [
        {"currency": r["currency"], "total": r["total"] or Decimal(0)} for r in rows
    ]


def owner_performance(account, start, end):
    """Count transitions and conversions (to_value='qualified') by owner_at_transition.

    Returns [{"owner_id": int, "owner_name": str, "transitions": int, "conversions": int}]
    """
    from apps.crm.models import StageTransition

    tz = _account_tz(account)
    start_utc = _to_utc(start, tz)
    end_utc = _to_utc(end, tz)

    qs = StageTransition.objects.filter(
        account=account,
        owner_at_transition__isnull=False,
        occurred_at__gte=start_utc,
        occurred_at__lt=end_utc,
    ).select_related("owner_at_transition")

    stats: dict[int, dict] = {}
    for tr in qs:
        u = tr.owner_at_transition
        if u is None:
            continue
        if u.id not in stats:
            name = u.get_full_name() or u.username
            stats[u.id] = {
                "owner_id": u.id,
                "owner_name": name,
                "transitions": 0,
                "conversions": 0,
            }
        stats[u.id]["transitions"] += 1
        if tr.to_value == "qualified":
            stats[u.id]["conversions"] += 1

    return sorted(stats.values(), key=lambda x: x["owner_id"])
