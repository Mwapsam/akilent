from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing.models import Plan, Subscription


@pytest.fixture
def plan(db):
    return Plan.objects.create(
        slug="starter",
        name="Starter",
        price_monthly=Decimal("19.00"),
        max_emails_per_month=10000,
        tracking_webhooks=False,
    )


@pytest.fixture
def account(db):
    # No Trial plan exists in the test DB, so the auto_create_trial signal is a
    # no-op here — we attach subscriptions explicitly in each test.
    return Account.objects.create(company_name="Acme Inc")


@pytest.fixture
def subscription(db, account, plan):
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.PAST_DUE,
        current_period_start=timezone.now(),
    )


# --- A fake payment provider for the provider-neutral core --------------------------------------

from django.http import HttpResponseRedirect  # noqa: E402

from apps.billing.providers import (  # noqa: E402
    FAILED,
    SUCCEEDED,
    PaymentProvider,
    ProviderResult,
    SavedMethodData,
    registry,
)


class FakeProvider(PaymentProvider):
    """In-memory provider: records charges and returns whatever ``next_status`` says."""

    code = "fake"
    display_name = "Fake Pay"
    off_session_charge = True

    def __init__(self):
        self.charges = []
        self.next_status = SUCCEEDED

    def start_checkout(self, request, payment):
        return HttpResponseRedirect(f"https://pay.example/{payment.reference}")

    def charge_saved_method(self, method, payment):
        self.charges.append((method.token, payment.reference, payment.amount_due))
        return ProviderResult(
            reference=payment.reference,
            status=self.next_status,
            provider_ref=f"ch_{len(self.charges)}",
            amount=payment.amount_due,
            currency=payment.currency,
            failure_reason="Card declined" if self.next_status == FAILED else "",
        )

    def paid(self, payment, *, card="tok_1"):
        return ProviderResult(
            reference=payment.reference,
            status=SUCCEEDED,
            provider_ref="tx_1",
            amount=payment.amount_due,
            currency=payment.currency,
            saved_method=SavedMethodData(token=card, last4="4242") if card else None,
        )


@pytest.fixture
def fake_provider(db):
    from apps.core.models import SiteSettings

    provider = FakeProvider()
    registry.register(provider)
    site = SiteSettings.load()
    site.billing_provider = provider.code
    site.save()
    yield provider
    registry.unregister(provider.code)
