"""Bank transfer / offline payment: the customer sees instructions and submits a reference; an
operator approves it in the console, which records the payment as succeeded. It can't take a
renewal on its own, so it's offered alongside the active provider rather than instead of it."""

from __future__ import annotations

from django.shortcuts import render

from .base import SUCCEEDED, PaymentProvider, ProviderResult


class ManualProvider(PaymentProvider):
    code = "manual"
    display_name = "Bank transfer"
    selectable = False
    has_webhooks = False

    def start_checkout(self, request, payment):
        from apps.billing.models import PaymentMethod

        method = PaymentMethod.objects.filter(code=self.code).first()
        return render(
            request,
            "billing/manual_checkout.html",
            {
                "payment": payment,
                "plan": payment.plan,
                "account": payment.account,
                "instructions": method.instructions if method else "",
            },
        )


def approved_result(payment) -> ProviderResult:
    """What an operator's approval of a bank transfer means for the payment."""
    return ProviderResult(
        reference=payment.reference,
        status=SUCCEEDED,
        amount=payment.amount_due,
        currency=payment.currency,
    )
