import logging

from django.conf import settings
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


def _absolute_url(path: str) -> str:
    base_domain = getattr(settings, "BASE_DOMAIN", "") or settings.ALLOWED_HOSTS[0]
    scheme = "http" if settings.DEBUG else "https"
    return f"{scheme}://{base_domain}{path}"


@shared_task
def expire_trials():
    from apps.billing.models import Subscription

    count = Subscription.objects.filter(
        status=Subscription.TRIALING,
        trial_ends_at__lte=timezone.now(),
    ).update(status=Subscription.EXPIRED)

    if count:
        logger.info("expire_trials: expired %s trial subscriptions", count)


@shared_task
def reset_monthly_usage():
    """
    UsageSummary rows are created on demand with period_start=first_of_month,
    so no reset is needed — new month = new row. This task is a heartbeat.
    """
    logger.info("reset_monthly_usage: heartbeat at %s", timezone.now().date())


@shared_task
def commit_stale_reservations() -> int:
    """Usage reservations a worker never settled are assumed used (cost-safe)."""
    from apps.billing.metering import commit_stale

    return commit_stale()


@shared_task
def send_limit_warning(account_id: int, key: str, percent: int) -> None:
    """Tell the owner a limit is at 80% or reached, once per limit per period."""
    from apps.accounts.models import Account
    from apps.billing import limit_catalog
    from apps.billing.metering import limit, period_end, used
    from apps.email.services.send import send_system_email

    account = Account.objects.filter(pk=account_id).first()
    if account is None:
        return
    owner = account.owner
    if owner is None or not owner.email:
        return
    lim = limit_catalog.get(key)
    allowed, n, resets = limit(account, key), used(account, key), period_end(key)
    if percent >= 100:
        subject = f"You've reached your {lim.name.lower()} limit"
        if lim.over_limit == limit_catalog.HOLD:
            what = (
                f"{account.company_name} has used all {allowed:,} {lim.unit} its plan includes. "
                "Anything more is held (not sent and not lost) until it resets or you upgrade."
            )
        else:
            what = (
                f"{account.company_name} has used all {allowed:,} {lim.unit} its plan includes. "
                "This is a heads-up only — nothing is blocked or held; customers are never affected."
            )
    else:
        subject = f"You've used {percent}% of your {lim.name.lower()}"
        what = f"{account.company_name} has used {n:,} of {allowed:,} {lim.unit}."
    body = (
        f"{what}\n\nIt resets on {resets:%d %B %Y}.\n\n"
        f"See your usage or upgrade: {_absolute_url('/billing/plans/')}\n"
    )
    try:
        from apps.email.services.html_email import build_transactional_html

        html_body = build_transactional_html(body, subject)
        send_system_email(
            owner.email,
            subject,
            text_body=body,
            html_body=html_body,
            sender_label="Billing",
        )
    except Exception:
        logger.exception(
            "send_limit_warning: couldn't email the owner of account %s", account_id
        )


@shared_task
def notify_admins_of_manual_payment(request_id: int) -> None:
    """Alert admins (email + Slack) that a bank-transfer payment needs review.

    Each channel is independent — a broken Slack webhook must not stop the
    email, and vice versa, since either one alone is enough for an admin to
    notice and act.
    """
    from django.contrib.auth.models import User

    from apps.billing.models import ManualPaymentRequest

    try:
        req = ManualPaymentRequest.objects.select_related("account", "plan").get(
            pk=request_id
        )
    except ManualPaymentRequest.DoesNotExist:
        logger.error(
            "notify_admins_of_manual_payment: request %s not found", request_id
        )
        return

    ctx = {
        "account": req.account,
        "plan": req.plan,
        "reference": req.reference,
        "review_url": _absolute_url("/manage/payments/"),
    }

    admin_emails = list(
        User.objects.filter(is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if admin_emails:
        from apps.email.services.send import send_system_email
        from apps.email.services.system_templates import render_system_email

        subject, text_body, html_body = render_system_email(
            "billing.manual_payment_admin",
            ctx,
            fallback_subject_template="billing/manual_payment_admin_subject.txt",
            fallback_body_template="billing/manual_payment_admin.txt",
            fallback_html_template="billing/manual_payment_admin.html",
        )
        for email in admin_emails:
            try:
                send_system_email(
                    email,
                    subject,
                    text_body=text_body,
                    html_body=html_body,
                    sender_label="Billing",
                )
            except Exception:
                logger.exception(
                    "notify_admins_of_manual_payment: email failed for %s request=%s",
                    email,
                    request_id,
                )
    else:
        logger.warning("notify_admins_of_manual_payment: no admin emails to notify")

    try:
        from apps.billing.slack import post_message

        post_message(
            f":bank: New bank transfer to review — *{req.account}* on *{req.plan.name}* "
            f"(ref: {req.reference or '—'})\n{ctx['review_url']}"
        )
    except Exception:
        logger.exception(
            "notify_admins_of_manual_payment: slack post failed for request=%s",
            request_id,
        )


@shared_task
def renew_due_subscriptions() -> dict:
    """The renewal engine (apps.billing.renewals): renew, charge, dun, cancel and expire."""
    from apps.billing import renewals

    return renewals.run()


@shared_task
def expire_credit_grants() -> int:
    from apps.billing import credit

    return credit.expire_due()


@shared_task
def abandon_stale_checkouts() -> int:
    """Payments nobody finished give their held credit back (and an unanswered renewal charge
    makes the subscription past due)."""
    from apps.billing import payments

    return payments.abandon_stale()


@shared_task
def notify_provider_change(old_code: str, new_code: str) -> int:
    """Tell paying customers whose saved method is with the old provider that their next
    renewal needs the new one. Their access and paid period are unaffected."""
    from apps.billing.models import SavedPaymentMethod, Subscription

    accounts = set(
        SavedPaymentMethod.objects.filter(
            provider=old_code, is_default=True
        ).values_list("account_id", flat=True)
    )
    subs = list(
        Subscription.objects.filter(
            account_id__in=accounts,
            status__in=[Subscription.ACTIVE, Subscription.PAST_DUE],
            cancel_at_period_end=False,
        ).values_list("account_id", flat=True)
    )
    for account_id in subs:
        send_billing_notice.delay(account_id, "provider_changed", None)
    return len(subs)


def _notice(sub, kind, payment) -> tuple[str, str] | None:
    from apps.billing import credit
    from apps.billing.money import format_money
    from apps.billing.pricing import price_for
    from apps.billing.providers import registry

    account = sub.account
    plan = sub.plan
    pay_link = _absolute_url(
        f"/billing/checkout/?plan={plan.slug}&period={sub.billing_period}"
    )
    ends = (
        f"{timezone.localtime(sub.current_period_end):%d %B %Y}"
        if sub.current_period_end
        else ""
    )
    provider = registry.active()
    with_provider = f" with {provider.display_name}" if provider else ""
    if kind == "upcoming":
        price = price_for(plan, sub.billing_period)
        covered = min(credit.balance(account), price)
        lines = [f"Your {plan.name} plan for {account.company_name} renews on {ends}."]
        if covered > 0:
            lines.append(
                f"{format_money(covered)} of your account credit will be used, "
                f"so {format_money(price - covered)} is due."
            )
        else:
            lines.append(f"The amount due is {format_money(price)}.")
        return f"Your {plan.name} plan renews on {ends}", "\n\n".join(lines)
    if kind == "renewed":
        paid = ""
        if payment is not None:
            paid = f"Paid: {format_money(payment.amount_due, payment.currency)}"
            if payment.credit_applied:
                paid += f" (plus {format_money(payment.credit_applied, payment.currency)} credit)"
            paid += ".\n\n"
        return (
            f"Your {plan.name} plan has renewed",
            f"{account.company_name} is on {plan.name} until {ends}.\n\n{paid}"
            f"Billing: {_absolute_url('/billing/plans/')}",
        )
    if kind in ("unpaid", "reminder"):
        from apps.billing.renewals import grace_period

        until = ""
        if sub.past_due_since:
            until = f" until {timezone.localtime(sub.past_due_since + grace_period()):%d %B %Y}"
        return (
            f"Action needed: renew your {plan.name} plan",
            f"We couldn't take the renewal payment for {account.company_name}'s {plan.name} plan."
            f"\n\nPay{with_provider} here to keep everything running: {pay_link}\n\n"
            f"Your features stay on{until}.",
        )
    if kind == "provider_changed":
        return (
            "How you pay for your plan is changing",
            f"From your next renewal ({ends}), payments for {account.company_name} go through"
            f"{with_provider or ' a new payment provider'}. Your plan and the time you've paid "
            f"for don't change.\n\nWhen it's time to renew we'll email you a link to add your "
            f"payment details. You can also do it now: {pay_link}",
        )
    if kind == "expired":
        return (
            f"Your {plan.name} plan has ended",
            f"The {plan.name} plan for {account.company_name} has ended because the renewal "
            f"wasn't paid. Nothing has been deleted.\n\nRenew{with_provider}: {pay_link}",
        )
    if kind == "cancelled":
        return (
            f"Your {plan.name} plan has ended",
            f"As requested, the {plan.name} plan for {account.company_name} has ended. "
            f"Nothing has been deleted.\n\nYou can subscribe again any time: "
            f"{_absolute_url('/billing/plans/')}",
        )
    return None


@shared_task
def send_billing_notice(
    account_id: int, kind: str, payment_id: int | None = None
) -> None:
    """Email the business about its subscription (renewal, unpaid, ended...)."""
    from apps.billing.models import BillingPayment, Subscription
    from apps.email.services.send import send_system_email

    sub = (
        Subscription.objects.select_related("plan", "account")
        .filter(account_id=account_id)
        .first()
    )
    if sub is None:
        return
    owner = sub.account.owner
    to = sub.account.billing_email or (owner.email if owner else "")
    if not to:
        return
    payment = (
        BillingPayment.objects.filter(pk=payment_id).first() if payment_id else None
    )
    built = _notice(sub, kind, payment)
    if built is None:
        return
    subject, body = built
    try:
        from apps.email.services.html_email import build_transactional_html

        send_system_email(
            to,
            subject,
            text_body=body,
            html_body=build_transactional_html(body, subject),
            sender_label="Billing",
        )
    except Exception:
        logger.exception(
            "send_billing_notice: %s email failed for account %s", kind, account_id
        )
