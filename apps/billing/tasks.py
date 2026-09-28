import logging

from django.conf import settings
from django.core.mail import send_mail
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
        send_system_email(owner.email, subject, text_body=body)
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
        "review_url": _absolute_url("/billing/plans/"),
    }

    admin_emails = list(
        User.objects.filter(is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if admin_emails:
        from apps.email.services.system_templates import render_system_email

        subject, body = render_system_email(
            "billing.manual_payment_admin",
            ctx,
            fallback_subject_template="billing/manual_payment_admin_subject.txt",
            fallback_body_template="billing/manual_payment_admin.txt",
        )
        try:
            send_mail(
                subject,
                body,
                settings.DEFAULT_FROM_EMAIL,
                admin_emails,
                fail_silently=False,
            )
        except Exception:
            logger.exception(
                "notify_admins_of_manual_payment: email failed for request=%s",
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
