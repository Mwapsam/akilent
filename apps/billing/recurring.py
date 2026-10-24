"""Provider-run recurring subscriptions (PayPal Subscriptions), seen from the billing core.

The provider keeps the renewal schedule and takes each payment; Akilent still grants every
period, and only from a completed payment (``payments.record_result``). ``Subscription``
remembers the provider subscription that renews it (``recurring_id``), and the renewal engine
waits for its charge instead of charging or sending a pay link.

Stopping one at the provider is the part that must not be lost: a replaced or ended recurring
subscription that keeps charging takes money for a plan the customer left. It is listed in
``recurring_stop_id`` until the provider confirms, and retried daily.
"""

from __future__ import annotations

import logging

from django.db import transaction

from .models import Subscription
from .providers import ProviderError, registry

logger = logging.getLogger(__name__)


# Set on a payment whose recurring subscription was stopped (cancelled) before the payment
# completed: the period it paid for is still granted, but nothing is linked and the plan stays
# cancelled at the period end.
RECURRING_STOPPED = "recurring_stopped"


def _stop_tokens(sub) -> list[str]:
    return sub.recurring_stop_id.split()


def queue_stop(sub, provider_code: str, recurring_id: str) -> None:
    """List a recurring subscription for stopping at its provider, and try once the transaction
    commits. ``sub`` is locked by the caller."""
    token = f"{provider_code}:{recurring_id}"
    tokens = _stop_tokens(sub)
    if token not in tokens:
        sub.recurring_stop_id = " ".join([*tokens, token])
        sub.save(update_fields=["recurring_stop_id", "updated_at"])
    from .tasks import stop_recurring

    account_id = sub.account_id
    transaction.on_commit(lambda: stop_recurring.delay(account_id))


def retire(sub) -> None:
    """``sub`` stops being renewed by its recurring subscription (it ended here). ``sub`` is
    locked and saved by the caller."""
    if sub.recurring_id:
        queue_stop(sub, sub.recurring_provider, sub.recurring_id)
    sub.recurring_id = ""
    sub.recurring_provider = ""


def stop_pending(account_id: int) -> int:
    """Stop, at their provider, the recurring subscriptions this account no longer uses. One the
    provider couldn't confirm stays listed for the next try. Returns how many were stopped."""
    sub = Subscription.objects.filter(account_id=account_id).first()
    if sub is None:
        return 0
    stopped = 0
    for token in _stop_tokens(sub):
        code, _, recurring_id = token.partition(":")
        provider = registry.get(code)
        if provider is None:
            logger.error("recurring: no provider %r to stop %s", code, recurring_id)
            continue
        try:
            provider.stop_recurring(recurring_id)
        except ProviderError as exc:
            logger.warning(
                "recurring: couldn't stop %s for account=%s yet: %s",
                token,
                account_id,
                exc,
            )
            continue
        _forget(sub.pk, token)
        stopped += 1
    return stopped


def _forget(sub_pk, token) -> None:
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=sub_pk)
        tokens = [t for t in _stop_tokens(sub) if t != token]
        sub.recurring_stop_id = " ".join(tokens)
        sub.save(update_fields=["recurring_stop_id", "updated_at"])


def end_now(sub) -> bool:
    """The customer cancelled in Akilent: stop the provider's charges before saying they won't
    be charged again. That includes a recurring subscription approved but not linked yet (its
    first payment is still settling); if that payment completes, its period is granted and the
    plan still ends with it. Raises ProviderError when a provider couldn't confirm. Returns
    whether automatic payments were stopped; the provider stays recorded so a resume can say
    renewal is now by pay link."""
    stopped = ""
    if sub.recurring_id:
        provider = registry.get(sub.recurring_provider)
        if provider is None:
            raise ProviderError(f"no provider {sub.recurring_provider!r}")
        provider.stop_recurring(sub.recurring_id)
        stopped = provider.code
        sub.recurring_id = ""
        sub.save(update_fields=["recurring_id", "updated_at"])
    for provider in registry.all_providers():
        for payment in provider.unlinked_recurring(sub.account):
            provider.stop_recurring(payment.provider_operation_id)
            payment.raw = {**(payment.raw or {}), RECURRING_STOPPED: True}
            payment.save(update_fields=["raw", "updated_at"])
            stopped = provider.code
    if stopped:
        sub.recurring_provider = stopped
        sub.save(update_fields=["recurring_provider", "updated_at"])
    return bool(stopped)


def stopped_automatic_payments(sub) -> bool:
    """A cancel stopped this plan's automatic payments (see end_now)."""
    return bool(sub.recurring_provider and not sub.recurring_id)


@transaction.atomic
def provider_ended(provider_code: str, recurring_id: str, *, by_customer: bool) -> None:
    """The provider says a recurring subscription ended (cancelled, suspended or expired there).
    If it was still renewing a plan, the plan now renews by pay link; when the customer cancelled
    it themselves, the plan ends with the paid period instead."""
    token = f"{provider_code}:{recurring_id}"
    for listed in Subscription.objects.select_for_update().filter(
        recurring_stop_id__contains=token
    ):
        listed.recurring_stop_id = " ".join(
            t for t in _stop_tokens(listed) if t != token
        )
        listed.save(update_fields=["recurring_stop_id", "updated_at"])
    sub = (
        Subscription.objects.select_for_update()
        .filter(recurring_provider=provider_code, recurring_id=recurring_id)
        .first()
    )
    if sub is None:
        return
    sub.recurring_id = ""
    fields = ["recurring_id", "recurring_provider", "updated_at"]
    if by_customer and sub.status == Subscription.ACTIVE:
        # Kept: the customer stopped the automatic payments themselves (a resume says so).
        sub.cancel_at_period_end = True
        fields.append("cancel_at_period_end")
    else:
        sub.recurring_provider = ""
    sub.save(update_fields=fields)
    logger.info(
        "recurring: %s ended at the provider for account=%s (by customer: %s)",
        token,
        sub.account_id,
        by_customer,
    )


def sync_all() -> int:
    """Daily: retry pending stops, and ask each provider about the recurring subscriptions it
    renews, so a missed webhook can't leave a paid period ungranted or a cancellation unseen."""
    count = 0
    for sub in Subscription.objects.exclude(recurring_id="", recurring_stop_id=""):
        if sub.recurring_stop_id:
            stop_pending(sub.account_id)
        if not sub.recurring_id:
            continue
        provider = registry.get(sub.recurring_provider)
        if provider is None:
            continue
        try:
            provider.sync_recurring(sub)
            count += 1
        except ProviderError as exc:
            logger.warning("recurring: sync of %s failed: %s", sub.recurring_id, exc)
        except Exception:
            logger.exception("recurring: sync of %s failed", sub.recurring_id)
    return count
