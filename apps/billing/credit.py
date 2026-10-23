"""Account credit: money an operator gives a business (early adopters, goodwill, unused time on a
plan change) that pays for its subscription before any provider is charged.

Credit is applied when a ``BillingPayment`` is created, so a provider only ever sees the smaller
amount still due; nothing here knows about providers. Grants are used earliest-expiring first.
Every change is a ``CreditTransaction`` row; ``CreditGrant.remaining`` is the running total.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from .models import BillingPayment, CreditGrant, CreditTransaction
from .money import billing_currency, quantize

ZERO = Decimal("0.00")


class CreditError(ValueError):
    """A credit change that isn't allowed; the message is shown to the operator as is."""


def _usable(account):
    now = timezone.now()
    return CreditGrant.objects.filter(
        account=account,
        revoked_at__isnull=True,
        remaining__gt=0,
        currency=billing_currency(),
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def balance(account) -> Decimal:
    return _usable(account).aggregate(total=Sum("remaining"))["total"] or ZERO


def next_expiry(account):
    """The soonest date some of the balance expires, or None."""
    grant = (
        _usable(account).filter(expires_at__isnull=False).order_by("expires_at").first()
    )
    return grant.expires_at if grant else None


def summary(account) -> dict:
    return {"balance": balance(account), "expires_at": next_expiry(account)}


@transaction.atomic
def grant(
    account, amount, *, reason=CreditGrant.OTHER, note="", expires_at=None, by=None
) -> CreditGrant:
    try:
        amount = quantize(Decimal(str(amount)))
    except Exception as exc:
        raise CreditError("Enter an amount, e.g. 150.") from exc
    if amount <= 0:
        raise CreditError("Credit must be more than zero.")
    if expires_at is not None and expires_at <= timezone.now():
        raise CreditError("The expiry date must be in the future.")
    if reason not in dict(CreditGrant.REASON_CHOICES):
        reason = CreditGrant.OTHER
    g = CreditGrant.objects.create(
        account=account,
        amount=amount,
        remaining=amount,
        currency=billing_currency(),
        reason=reason,
        note=(note or "").strip()[:255],
        expires_at=expires_at,
        granted_by=by,
    )
    CreditTransaction.objects.create(
        account=account,
        grant=g,
        kind=CreditTransaction.GRANT,
        amount=amount,
        created_by=by,
    )
    return g


@transaction.atomic
def apply(payment: BillingPayment, max_amount: Decimal | None = None) -> Decimal:
    """Use the account's credit towards ``payment`` (at most ``max_amount``, default the gross
    amount). Idempotent: a payment that already holds credit gets nothing more. After a
    ``restore`` it can apply again (a payment that completes after being given up on)."""
    held = _net_applied(payment)
    if held > 0:
        return held
    if payment.currency.upper() != billing_currency():
        return ZERO
    want = quantize(payment.gross_amount if max_amount is None else max_amount)
    used = ZERO
    grants = (
        _usable(payment.account)
        .select_for_update()
        .order_by(F("expires_at").asc(nulls_last=True), "created_at", "pk")
    )
    for g in grants:
        if used >= want:
            break
        take = min(g.remaining, want - used)
        g.remaining -= take
        g.save(update_fields=["remaining"])
        CreditTransaction.objects.create(
            account=payment.account,
            grant=g,
            kind=CreditTransaction.APPLIED,
            amount=-take,
            payment=payment,
        )
        used += take
    return used


def _net_applied(payment) -> Decimal:
    total = CreditTransaction.objects.filter(
        payment=payment,
        kind__in=[CreditTransaction.APPLIED, CreditTransaction.RESTORED],
    ).aggregate(total=Sum("amount"))["total"]
    return -(total or ZERO)


@transaction.atomic
def restore(payment: BillingPayment) -> Decimal:
    """Give back credit a payment holds, when that payment won't complete. Idempotent."""
    held = (
        CreditTransaction.objects.filter(
            payment=payment,
            kind__in=[CreditTransaction.APPLIED, CreditTransaction.RESTORED],
        )
        .order_by()  # the default ordering would split the GROUP BY
        .values("grant_id")
        .annotate(net=Sum("amount"))
        .filter(net__lt=0)
    )
    returned = ZERO
    for row in held:
        g = CreditGrant.objects.select_for_update().get(pk=row["grant_id"])
        amount = -row["net"]
        if g.revoked_at is None:
            g.remaining += amount
            g.save(update_fields=["remaining"])
        CreditTransaction.objects.create(
            account=payment.account,
            grant=g,
            kind=CreditTransaction.RESTORED,
            amount=amount,
            payment=payment,
        )
        returned += amount
    return returned


@transaction.atomic
def revoke(g: CreditGrant, *, by=None) -> Decimal:
    g = CreditGrant.objects.select_for_update().get(pk=g.pk)
    if g.revoked_at is not None:
        raise CreditError("That credit was already revoked.")
    taken = g.remaining
    g.remaining = ZERO
    g.revoked_at = timezone.now()
    g.save(update_fields=["remaining", "revoked_at"])
    CreditTransaction.objects.create(
        account=g.account,
        grant=g,
        kind=CreditTransaction.REVOKED,
        amount=-taken,
        created_by=by,
    )
    return taken


def expire_due() -> int:
    """Zero the remainder of grants past their expiry. Returns how many were expired."""
    count = 0
    due = CreditGrant.objects.filter(
        revoked_at__isnull=True, remaining__gt=0, expires_at__lte=timezone.now()
    ).values_list("pk", flat=True)
    for pk in list(due):
        with transaction.atomic():
            g = CreditGrant.objects.select_for_update().get(pk=pk)
            if g.remaining <= 0:
                continue
            taken = g.remaining
            g.remaining = ZERO
            g.save(update_fields=["remaining"])
            CreditTransaction.objects.create(
                account=g.account,
                grant=g,
                kind=CreditTransaction.EXPIRED,
                amount=-taken,
            )
            count += 1
    return count


def ledger(account, limit: int = 50):
    return (
        CreditTransaction.objects.filter(account=account)
        .select_related("grant", "payment", "created_by")
        .order_by("-created_at", "-pk")[:limit]
    )


def grant_early_adopters(
    amount, *, wave=None, note="", expires_at=None, by=None, dry_run=False
) -> tuple[list, list]:
    """Give every pilot business (optionally one wave) early-adopter credit, once: a business
    that already has an early-adopter grant is skipped. Returns (granted, skipped) accounts."""
    from apps.accounts.models import PilotEnrollment

    enrollments = PilotEnrollment.objects.select_related("account").order_by(
        "account_id"
    )
    if wave:
        enrollments = enrollments.filter(wave=wave)
    already = set(
        CreditGrant.objects.filter(reason=CreditGrant.EARLY_ADOPTER).values_list(
            "account_id", flat=True
        )
    )
    granted, skipped = [], []
    for enrollment in enrollments:
        account = enrollment.account
        if account.pk in already or not account.is_active:
            skipped.append(account)
            continue
        if not dry_run:
            grant(
                account,
                amount,
                reason=CreditGrant.EARLY_ADOPTER,
                note=note,
                expires_at=expires_at,
                by=by,
            )
        granted.append(account)
    return granted, skipped
