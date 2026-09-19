import logging
import uuid

import stripe
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from ..models import Subscription
from ..pricing import price_for, stripe_interval
from .base import PaymentGateway

logger = logging.getLogger(__name__)


class StripeGateway(PaymentGateway):
    code = "stripe"
    label = "Card (Stripe)"

    def start_checkout(self, request, account, plan):
        period = request.GET.get("period", Subscription.MONTHLY)
        if period not in dict(Subscription.BILLING_PERIOD_CHOICES):
            period = Subscription.MONTHLY

        amount = price_for(plan, period)
        interval, interval_count = stripe_interval(period)
        currency = getattr(settings, "STRIPE_CURRENCY", "USD")

        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            session = stripe.checkout.Session.create(
                mode="subscription",
                line_items=[
                    {
                        "price_data": {
                            "currency": currency,
                            "product_data": {"name": plan.name},
                            "unit_amount": int(amount * 100),
                            "recurring": {
                                "interval": interval,
                                "interval_count": interval_count,
                            },
                        },
                        "quantity": 1,
                    }
                ],
                customer_email=request.user.email or None,
                success_url=request.build_absolute_uri(reverse("billing:stripe-success"))
                + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=request.build_absolute_uri("/billing/plans/"),
                client_reference_id=f"sub_{account.pk}_{plan.slug}_{uuid.uuid4().hex[:8]}",
                metadata={
                    "account_id": str(account.pk),
                    "plan_slug": plan.slug,
                    "billing_period": period,
                },
            )
        except stripe.error.StripeError as exc:
            logger.error("checkout: Stripe error for account=%s plan=%s: %s", account.pk, plan.slug, exc)
            messages.error(request, f"Payment initialization failed: {exc}")
            return redirect("/billing/plans/")

        return redirect(session.url)
