from datetime import timedelta

from django.utils import timezone

from .models import Subscription
from .pricing import period_days


def activate_subscription(
    account,
    plan,
    payment_method: str,
    *,
    billing_period: str = Subscription.MONTHLY,
    fw_customer_email=None,
    stripe_customer_id=None,
    stripe_subscription_id=None,
) -> Subscription:
    """Activate (or renew) an account's subscription to ``plan``.

    Shared by every payment method — Flutterwave's checkout callback and
    webhook, Stripe's webhook, and manual-payment admin approval — so
    activation semantics (period length, status, cancellation reset) stay
    identical regardless of which gateway triggered them.
    """
    now = timezone.now()
    defaults = {
        "plan": plan,
        "status": Subscription.ACTIVE,
        "billing_period": billing_period,
        "current_period_start": now,
        "current_period_end": now + timedelta(days=period_days(billing_period)),
        "payment_method": payment_method,
        "trial_ends_at": None,
        "cancelled_at": None,
    }
    if fw_customer_email:
        defaults["fw_customer_email"] = fw_customer_email
    if stripe_customer_id:
        defaults["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id:
        defaults["stripe_subscription_id"] = stripe_subscription_id

    sub, _ = Subscription.objects.update_or_create(account=account, defaults=defaults)
    return sub
