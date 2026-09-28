"""Limits and usage: how much a business may use, how much it has used, and reservations.

Reached through ``apps.billing.api`` (which re-exports the public names); other apps never import
this module or the models.

The contract for anything that costs a unit (a WhatsApp template send, an email, an AI call):

    reservation = reserve(account, key, operation_id=..., units=1)
    if reservation is None:      -> "held: limit" - the operation is NOT attempted
    ... external call ...
    commit(reservation)          -> on success (or when the outcome is unknown: cost-safe)
    release(reservation)         -> only when the provider definitely refused it

``operation_id`` is stable for one logical operation, so a retry or replayed task gets the same
reservation back and is never counted twice. Reservation happens before the external call, never
after, so concurrent sends can't overspend.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import cast

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.billing import limit_catalog as limits_catalog
from apps.billing.models import (
    AccountLimitOverride,
    PlanLimit,
    Subscription,
    UsageCounter,
    UsageReservation,
)

logger = logging.getLogger(__name__)

WARN_AT = 0.8


class LimitReached(Exception):
    """A REFUSE-type limit stops an action. The message is shown to the owner as is."""

    def __init__(self, key: str, message: str):
        self.key = key
        super().__init__(message)


def period_start(key: str, now=None) -> date | None:
    lim = limits_catalog.get(key)
    today = (now or timezone.now()).date()
    if lim.period == limits_catalog.MONTH:
        return today.replace(day=1)
    if lim.period == limits_catalog.DAY:
        return today
    return None


def period_end(key: str, now=None) -> date | None:
    """The first day of the next period (when the counter starts again)."""
    start = period_start(key, now)
    if start is None:
        return None
    if limits_catalog.get(key).period == limits_catalog.DAY:
        return start + timedelta(days=1)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


# ---- how much is allowed ----------------------------------------------------------------------


def _resolve(
    default: int, *, plan_value=None, has_plan: bool = False, override=None
) -> tuple[int, str]:
    """The one precedence rule, shared by ``limit_source`` (one key) and ``usage_report`` (all of
    them, prefetched): an unexpired operator override, else the plan, else the catalog default."""
    if override is not None and (
        override.expires_at is None or override.expires_at > timezone.now()
    ):
        return override.value, "override"
    if has_plan:
        return plan_value, "plan"
    return default, "default"


def limit_source(account, key: str) -> tuple[int, str]:
    """(effective limit, where it came from): an unexpired operator override, else the plan,
    else the catalog default. -1 means unlimited."""
    lim = limits_catalog.get(key)
    override = AccountLimitOverride.objects.filter(account=account, key=key).first()
    sub = Subscription.objects.filter(account=account).only("plan_id").first()
    row = (
        PlanLimit.objects.filter(plan_id=sub.plan_id, key=key).first()
        if sub is not None
        else None
    )
    return _resolve(
        lim.default,
        plan_value=row.value if row else None,
        has_plan=row is not None,
        override=override,
    )


def limit(account, key: str) -> int:
    return limit_source(account, key)[0]


# ---- how much is used -------------------------------------------------------------------------


def _live_total(account, key: str) -> int:
    """TOTAL limits are counted from what exists now, through each app's api.

    Every catalog key with ``period == TOTAL`` needs a branch here; a missing one is a
    maintenance mistake, caught by ``test_every_total_limit_has_a_live_total_branch``
    (not a 500 an operator hits in production)."""
    if key == "whatsapp_numbers":
        from apps.whatsapp import api as whatsapp_api

        return whatsapp_api.count_active_business_numbers(account)
    if key == "automation_rules":
        from apps.automation import api as automation_api

        return automation_api.count_active_rules(account)
    if key == "contacts":
        from apps.contacts import api as contacts_api

        return contacts_api.count_contacts(account)
    if key == "team_members":
        from apps.accounts import api as accounts_api

        return accounts_api.count_members(account)
    raise KeyError(key)


def used(account, key: str) -> int:
    lim = limits_catalog.get(key)
    if lim.period == limits_catalog.TOTAL:
        return _live_total(account, key)
    if lim.period == limits_catalog.PER_USE:
        return 0
    start = period_start(key)
    assert start is not None
    row = UsageCounter.objects.filter(
        account=account, key=key, period_start=start
    ).first()
    return row.used if row else 0


def usage_report(account, *, metered_only: bool = False) -> list[dict]:
    """Every limit with its effective value, source, use this period and percentage.

    Overrides, plan limits and counters are read in three queries, not per limit.
    ``metered_only`` skips the live counts of TOTAL limits (the banners don't need them)."""
    overrides = {o.key: o for o in AccountLimitOverride.objects.filter(account=account)}
    sub = Subscription.objects.filter(account=account).only("plan_id").first()
    plan_rows = (
        {r.key: r.value for r in PlanLimit.objects.filter(plan_id=sub.plan_id)}
        if sub
        else {}
    )
    starts: dict[str, date] = {
        lim.key: cast("date", period_start(lim.key))
        for lim in limits_catalog.LIMITS
        if lim.period in (limits_catalog.MONTH, limits_catalog.DAY)
    }
    counters = {
        (c.key, c.period_start): c.used
        for c in UsageCounter.objects.filter(
            account=account, key__in=list(starts), period_start__in=set(starts.values())
        )
    }
    rows = []
    for lim in limits_catalog.LIMITS:
        if metered_only and lim.period not in (
            limits_catalog.MONTH,
            limits_catalog.DAY,
        ):
            continue
        # Same precedence rule as limit_source(), from the dicts prefetched above.
        value, source = _resolve(
            lim.default,
            plan_value=plan_rows.get(lim.key),
            has_plan=lim.key in plan_rows,
            override=overrides.get(lim.key),
        )
        try:
            if lim.key in starts:
                n = counters.get((lim.key, starts[lim.key]), 0)
            else:
                n = used(account, lim.key)
        except Exception:  # an app without the counter we need must not break the page
            logger.exception("usage_report: couldn't count %s", lim.key)
            n = 0
        if value < 0 or lim.period == limits_catalog.PER_USE:
            pct = None
        elif value == 0 or n >= value:
            pct = 100  # truly used up, not just close: rounding must never claim 100% early
        else:
            pct = min(
                99, (n * 100) // value
            )  # floored, so "used up" only ever means used up
        rows.append(
            {
                "key": lim.key,
                "name": lim.name,
                "unit": lim.unit,
                "period": lim.period,
                "limit": value,
                "source": source,
                "used": n,
                "percent": pct,
                "resets": period_end(lim.key),
                "cost_bearing": lim.cost_bearing,
                "over_limit": lim.over_limit,
            }
        )
    return rows


def warnings(account) -> list[dict]:
    """Limits at 80% or more this period (for the dashboard and billing banners)."""
    return [
        r
        for r in usage_report(account, metered_only=True)
        if r["percent"] is not None
        and r["percent"] >= 80
        and r["period"] in (limits_catalog.MONTH, limits_catalog.DAY)
    ]


# ---- rules (TOTAL and PER_USE limits) ---------------------------------------------------------


def check_rule(account, key: str, value: int) -> bool:
    """Whether ``value`` is within the limit: recipients in one campaign (PER_USE), or the new
    total after adding one (TOTAL). Never counts anything."""
    allowed = limit(account, key)
    return allowed < 0 or value <= allowed


def require_room(account, key: str, adding: int = 1) -> None:
    """Refuse with LimitReached if adding ``adding`` more would pass a TOTAL limit."""
    lim = limits_catalog.get(key)
    allowed = limit(account, key)
    if allowed < 0:
        return
    if _live_total(account, key) + adding > allowed:
        raise LimitReached(
            key, f"Your plan allows {allowed} {lim.unit}. Upgrade to add more."
        )


# ---- metered use (MONTH / DAY) ----------------------------------------------------------------


def _counter(account, key: str, start: date) -> UsageCounter:
    try:
        row, _ = UsageCounter.objects.get_or_create(
            account=account, key=key, period_start=start
        )
    except IntegrityError:  # created by a concurrent caller
        row = UsageCounter.objects.get(account=account, key=key, period_start=start)
    return row


def _after_use(account, key: str, start: date, allowed: int) -> None:
    """Tell the owner once per period when a limit passes 80% and when it's reached."""
    if allowed <= 0:
        return
    row = UsageCounter.objects.filter(
        account=account, key=key, period_start=start
    ).first()
    if row is None:
        return
    for threshold, field in ((1.0, "warned_100"), (WARN_AT, "warned_80")):
        if row.used >= allowed * threshold and not getattr(row, field):
            # Reaching the limit covers the 80% notice too, so it is never sent afterwards.
            flags = (
                {"warned_100": True, "warned_80": True}
                if threshold == 1.0
                else {field: True}
            )
            claimed = UsageCounter.objects.filter(pk=row.pk, **{field: False}).update(
                **flags
            )
            if claimed:
                try:
                    from apps.billing.tasks import send_limit_warning

                    send_limit_warning.delay(account.pk, key, int(threshold * 100))
                except Exception:
                    logger.exception("couldn't queue the limit warning for %s", key)
            break


def count(account, key: str, units: int = 1) -> None:
    """Count use of a SOFT limit (never blocks: customer conversations). Warns at 80%/100%."""
    lim = limits_catalog.get(key)
    if lim.period not in (limits_catalog.MONTH, limits_catalog.DAY):
        raise ValueError(f"{key} isn't a metered limit")
    start = period_start(key)
    if start is None:
        return
    row = _counter(account, key, start)
    UsageCounter.objects.filter(pk=row.pk).update(used=F("used") + units)
    _after_use(account, key, start, limit(account, key))


def reserve(
    account,
    key: str,
    *,
    operation_id: str,
    units: int = 1,
    renew_released: bool = False,
) -> UsageReservation | None:
    """Claim ``units`` for one operation, atomically, or return None when the limit is reached
    (the caller marks the operation "held: limit" and doesn't attempt it).

    Idempotent: the same ``operation_id`` returns its existing reservation and never counts
    again. A RELEASED one (the provider refused an earlier attempt) is returned as is, unless
    ``renew_released``: then the operation is being deliberately tried again and reserves anew.
    """
    lim = limits_catalog.get(key)
    if lim.period not in (limits_catalog.MONTH, limits_catalog.DAY):
        raise ValueError(f"{key} isn't a metered limit")
    existing = UsageReservation.objects.filter(
        account=account, key=key, operation_id=operation_id
    ).first()
    if existing is not None:
        if not (renew_released and existing.status == UsageReservation.RELEASED):
            return existing
        existing.delete()
    allowed = limit(account, key)
    start = period_start(key)
    assert start is not None
    row = _counter(account, key, start)
    try:
        with transaction.atomic():
            counters = UsageCounter.objects.filter(pk=row.pk)
            if allowed >= 0:
                counters = counters.filter(used__lte=allowed - units)
            if not counters.update(used=F("used") + units):
                return None
            reservation = UsageReservation.objects.create(
                account=account,
                key=key,
                units=units,
                operation_id=operation_id,
                period_start=start,
            )
    except (
        IntegrityError
    ):  # the same operation reserved concurrently: the counter rolled back
        return UsageReservation.objects.get(
            account=account, key=key, operation_id=operation_id
        )
    _after_use(account, key, start, allowed)
    return reservation


def reserve_all(
    account, keys: list[str], *, operation_id: str, units: int = 1
) -> list | None:
    """Reserve several limits for one operation (an email uses emails_month AND emails_day), all
    or none: if any is reached, the ones already taken are released."""
    return reserve_all_verbose(account, keys, operation_id=operation_id, units=units)[0]


def reserve_all_verbose(
    account, keys: list[str], *, operation_id: str, units: int = 1
) -> tuple[list | None, str | None]:
    """Same as ``reserve_all``, plus which key actually blocked it (or None on success), so a
    caller can report the real cause without re-querying usage after the fact — a re-query would
    race a concurrent change and could name the wrong limit."""
    taken: list[UsageReservation] = []
    for key in keys:
        r = reserve(account, key, operation_id=operation_id, units=units)
        if r is None or r.status == UsageReservation.RELEASED:
            for t in taken:
                if release(t):
                    # A rolled-back reservation is not a refused send: forget it so the operation
                    # can reserve again once the other limit has room.
                    UsageReservation.objects.filter(
                        pk=t.pk, status=UsageReservation.RELEASED
                    ).delete()
            return None, key
        taken.append(r)
    return taken, None


def _settle(reservation: UsageReservation, status: str) -> bool:
    now = timezone.now()
    changed = UsageReservation.objects.filter(
        pk=reservation.pk, status=UsageReservation.RESERVED
    ).update(status=status, settled_at=now)
    reservation.status = status if changed else reservation.status
    return bool(changed)


def commit(reservation: UsageReservation | None) -> bool:
    """The operation happened (or may have): its units stay used."""
    return reservation is not None and _settle(reservation, UsageReservation.COMMITTED)


def release(reservation: UsageReservation | None) -> bool:
    """The provider definitely refused it: give the units back. Only this reservation's units,
    only once."""
    if not reservation or not _settle(reservation, UsageReservation.RELEASED):
        return False
    UsageCounter.objects.filter(
        account_id=reservation.account_id,
        key=reservation.key,
        period_start=reservation.period_start,
        used__gte=reservation.units,
    ).update(used=F("used") - reservation.units)
    return True


def settle_operation(account, operation_id: str, *, ok: bool) -> None:
    """Commit (``ok``) or release every open reservation of one operation."""
    for r in UsageReservation.objects.filter(
        account=account, operation_id=operation_id, status=UsageReservation.RESERVED
    ):
        commit(r) if ok else release(r)


def commit_stale(older_than=timedelta(hours=24)) -> int:
    """Reservations left open (a worker died mid-operation) are assumed used: cost-safe."""
    cutoff = timezone.now() - older_than
    stale = UsageReservation.objects.filter(
        status=UsageReservation.RESERVED, created_at__lt=cutoff
    )
    n = stale.update(status=UsageReservation.COMMITTED, settled_at=timezone.now())
    if n:
        logger.warning(
            "commit_stale: %s open reservation(s) older than %s assumed used",
            n,
            older_than,
        )
    return n
