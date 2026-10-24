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


def _stop_tokens(sub) -> list[str]:
    return sub.recurring_stop_id.split()


@transaction.atomic
def retire(sub) -> None:
    """``sub`` stops being renewed by its recurring subscription (it expired here): list that for
    stopping at the provider. The caller saves ``sub``."""
    if not sub.recurring_id:
        return
    token = f"{sub.recurring_provider}:{sub.recurring_id}"
    sub.recurring_stop_id = " ".join(dict.fromkeys([*_stop_tokens(sub), token]))
    sub.recurring_id = ""
    account_id = sub.account_id
    from .tasks import stop_recurring

    transaction.on_commit(lambda: stop_recurring.delay(account_id))


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


def end_now(sub) -> None:
    """The customer cancelled in Akilent: stop the provider's charges before saying they won't
    be charged again. Raises ProviderError when the provider couldn't confirm. The provider
    stays recorded, so a later resume can say renewal is now by pay link."""
    if not sub.recurring_id:
        return
    provider = registry.get(sub.recurring_provider)
    if provider is None:
        raise ProviderError(f"no provider {sub.recurring_provider!r}")
    provider.stop_recurring(sub.recurring_id)
    sub.recurring_id = ""
    sub.save(update_fields=["recurring_id", "updated_at"])


@transaction.atomic
def provider_ended(provider_code: str, recurring_id: str, *, by_customer: bool) -> None:
    """The provider says a recurring subscription ended (cancelled, suspended or expired there).
    If it was still renewing a plan, the plan now renews by pay link; when the customer cancelled
    it themselves, the plan ends with the paid period instead."""
    token = f"{provider_code}:{recurring_id}"
    for sub in Subscription.objects.select_for_update().filter(
        recurring_stop_id__contains=token
    ):
        sub.recurring_stop_id = " ".join(t for t in _stop_tokens(sub) if t != token)
        sub.save(update_fields=["recurring_stop_id", "updated_at"])
    sub = (
        Subscription.objects.select_for_update()
        .filter(recurring_provider=provider_code, recurring_id=recurring_id)
        .first()
    )
    if sub is None:
        return
    sub.recurring_id = ""
    fields = ["recurring_id", "updated_at"]
    if by_customer and sub.status == Subscription.ACTIVE:
        sub.cancel_at_period_end = True
        fields.append("cancel_at_period_end")
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
