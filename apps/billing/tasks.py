import logging
import re

from django.conf import settings
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)


def _absolute_url(path: str) -> str:
    base_domain = getattr(settings, "BASE_DOMAIN", "") or settings.ALLOWED_HOSTS[0]
    scheme = "http" if settings.DEBUG else "https"
    return f"{scheme}://{base_domain}{path}"


@shared_task
def expire_trials() -> int:
    """Trials past their end become EXPIRED, and the owner is told. Features already lock at
    trial_ends_at whether or not this has run (billing.api._read_state); this records it and
    sends the email. Each row is moved only if it is still a trial that has ended, so an
    operator extending the trial at the same moment wins."""
    from apps.billing.models import Subscription

    now = timezone.now()
    count = 0
    for pk, account_id in Subscription.objects.filter(
        status=Subscription.TRIALING, trial_ends_at__lte=now
    ).values_list("pk", "account_id"):
        moved = Subscription.objects.filter(
            pk=pk, status=Subscription.TRIALING, trial_ends_at__lte=now
        ).update(status=Subscription.EXPIRED, updated_at=now)
        if moved:
            count += 1
            send_billing_notice.delay(account_id, "trial_ended", None)
    if count:
        logger.info("expire_trials: expired %s trial subscriptions", count)
    return count


@shared_task
def billing_health_check() -> list[str]:
    """Daily: anything in billing that should have moved on and hasn't (a stopped renewal
    job, a stuck payment...). Operators are emailed and Slack is posted only when something
    is found."""
    from apps.billing import health

    found = health.problems()
    if found:
        health.alert(found)
    return found


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

        html_body = build_transactional_html(
            _strip_link_lines(body),
            subject,
            eyebrow="Usage",
            heading=subject,
            details=[
                ("Used", f"{n:,} of {allowed:,} {lim.unit}"),
                ("Resets on", f"{resets:%d %B %Y}"),
            ],
            cta_url=_absolute_url("/billing/plans/"),
            cta_label="See usage and plans",
        )
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
def stop_recurring(account_id: int) -> int:
    """Stop, at the provider, recurring subscriptions an account no longer uses (replaced by a
    plan change, or ended here). One that can't be stopped now is retried by sync_recurring."""
    from apps.billing import recurring

    return recurring.stop_pending(account_id)


@shared_task
def sync_recurring() -> int:
    """Daily: compare each provider-run recurring subscription with the provider (payments or
    an ending a webhook missed), and retry stops that didn't go through."""
    from apps.billing import recurring

    return recurring.sync_all()


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
    from apps.billing.renewals import grace_period, renews_automatically

    deadline = (
        f"{timezone.localtime(sub.past_due_since + grace_period()):%d %B %Y}"
        if sub.past_due_since
        else ""
    )
    if kind == "upcoming":
        price = price_for(plan, sub.billing_period)
        covered = min(credit.balance(account), price)
        lines = [f"Your {plan.name} plan for {account.company_name} renews on {ends}."]
        recurring = registry.get(sub.recurring_provider) if sub.recurring_id else None
        if recurring is not None:
            # The provider charges the price the customer subscribed at; credit can't reduce it.
            amount = recurring.recurring_amount(sub)
            what = format_money(*amount) if amount else "the renewal"
            lines.append(
                f"{recurring.display_name} will take {what} automatically; "
                "you don't need to do anything. Manage billing: "
                f"{_absolute_url('/billing/plans/')}"
            )
            return f"Your {plan.name} plan renews on {ends}", "\n\n".join(lines)
        if price - covered <= 0:
            lines.append(
                f"Your account credit covers it ({format_money(price)}), so there is "
                "nothing to pay."
            )
            return f"Your {plan.name} plan renews on {ends}", "\n\n".join(lines)
        if covered > 0:
            lines.append(
                f"{format_money(covered)} of your account credit will be used, "
                f"so {format_money(price - covered)} is due."
            )
        else:
            lines.append(f"The amount due is {format_money(price)}.")
        if renews_automatically(sub):
            lines.append(
                "It will be charged automatically; you don't need to do anything."
            )
        else:
            # Nothing charges this plan by itself: give the way to pay now, before it's due.
            lines.append(
                "Pay before the renewal date to keep everything running. Paying early "
                f"doesn't lose any days: the new period starts on {ends}.\n\n"
                f"Pay{with_provider}: {pay_link}"
            )
        return f"Your {plan.name} plan renews on {ends}", "\n\n".join(lines)
    if kind in ("due", "due_reminder"):
        # The renewal date passed and nothing was charged (this plan renews by pay link).
        price = price_for(plan, sub.billing_period)
        keep = f" until {deadline}" if deadline else ""
        return (
            f"Your {plan.name} renewal is due",
            f"The {plan.name} plan for {account.company_name} was due to renew on {ends}. "
            f"The amount due is {format_money(price)}.\n\n"
            f"Your features stay on{keep}. Pay{with_provider} here to keep everything "
            f"running: {pay_link}",
        )
    if kind == "finish_payment":
        from apps.billing import payments as billing_payments

        quote = billing_payments.quote(account, plan, sub.billing_period)
        term = sub.get_billing_period_display().lower()
        lines = [
            f"You created {account.company_name}'s Akilent account on the {plan.name} plan "
            f"({term}), but the payment wasn't finished, so the plan isn't on yet.",
            f"The amount due is {format_money(quote['due'])}."
            + (
                f" ({format_money(quote['credit'])} of your account credit is already "
                "taken off.)"
                if quote["credit"]
                else ""
            ),
            "If you've already paid, you don't need to do anything: the plan switches on "
            "as soon as the payment is confirmed.",
            f"Finish paying: {_absolute_url('/billing/pay/')}",
        ]
        return f"Finish setting up your {plan.name} plan", "\n\n".join(lines)
    if kind == "trial_ending":
        trial_end = (
            f"{timezone.localtime(sub.trial_ends_at):%d %B %Y}"
            if sub.trial_ends_at
            else ""
        )
        return (
            f"Your free trial ends on {trial_end}",
            f"The free trial for {account.company_name} ends on {trial_end}. After that, "
            "the features that come with a plan switch off. Nothing is deleted.\n\n"
            f"Choose a plan to keep everything running: {_absolute_url('/billing/plans/')}",
        )
    if kind == "trial_ended":
        return (
            "Your free trial has ended",
            f"The free trial for {account.company_name} has ended, so the features that "
            "come with a plan are off. Nothing has been deleted.\n\n"
            f"Choose a plan to switch them back on: {_absolute_url('/billing/plans/')}",
        )
    if kind == "payment_review":
        paid = ""
        if payment is not None:
            paid = (
                f" for {format_money(payment.amount_due, payment.currency)}"
                f" (reference {payment.reference})"
            )
        return (
            "We're waiting for your payment to be confirmed",
            f"Your payment{paid} has reached the payment provider, which is still checking "
            "it. This can take a few days. You don't need to do anything, and please don't "
            f"pay again: {plan.name} switches on by itself as soon as it's confirmed, and "
            f"we'll email you.\n\nBilling: {_absolute_url('/billing/plans/')}",
        )
    if kind == "auto_pay_stopped":
        return (
            f"Automatic payments for your {plan.name} plan have stopped",
            f"The payment provider stopped the automatic payments for "
            f"{account.company_name}'s {plan.name} plan. Your plan and the time you've "
            f"paid for don't change.\n\nYour next renewal is on {ends}. We'll email you a "
            f"link to pay before then, or you can set it up now: {pay_link}",
        )
    if kind == "paid":
        paid = ""
        if payment is not None:
            paid = f"Paid: {format_money(payment.amount_due, payment.currency)}"
            if payment.credit_applied:
                paid += f" (plus {format_money(payment.credit_applied, payment.currency)} credit)"
            paid += f". Reference: {payment.reference}.\n\n"
        return (
            f"Payment confirmed — you're on {plan.name}",
            f"Thanks — we've received your payment. {account.company_name} is on the "
            f"{plan.name} plan until {ends}.\n\n{paid}"
            f"Billing: {_absolute_url('/billing/plans/')}",
        )
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
        until = f" until {deadline}" if deadline else ""
        recurring = registry.get(sub.recurring_provider) if sub.recurring_id else None
        if recurring is not None:
            # The provider retries the payment itself; a pay link as well could pay it twice.
            return (
                f"Action needed: your {plan.name} renewal didn't go through",
                f"{recurring.display_name} couldn't take the renewal payment for "
                f"{account.company_name}'s {plan.name} plan. It will try again "
                f"automatically. Please check the card or balance on your "
                f"{recurring.display_name} account.\n\nYour features stay on{until}. "
                f"Billing: {_absolute_url('/billing/plans/')}",
            )
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


# A closing "Label: URL" sentence (the whole paragraph, or after a sentence that ends in
# . ! ?). The label can't contain sentence punctuation, so only that sentence is matched.
_TRAILING_LINK_RE = re.compile(
    r"(?:^|(?<=[.!?]))\s*[^.!?\n:]{1,60}:\s*https?://\S+\s*$"
)


def _strip_link_lines(body: str) -> str:
    """Drop closing "Label: URL" sentences and "Paid: ..." paragraphs from the text for the
    HTML version, which shows them as a button and a details table. Anything said before
    the link in the same paragraph is kept."""
    keep = []
    for p in body.split("\n\n"):
        p = p.strip()
        if p.startswith("Paid: "):
            continue
        p = _TRAILING_LINK_RE.sub("", p).strip()
        if p:
            keep.append(p)
    return "\n\n".join(keep)


def _notice_parts(sub, kind, payment) -> dict:
    """Heading, details and button for the HTML version of a ``_notice`` email."""
    from apps.billing.money import format_money
    from apps.billing.providers import registry

    plan = sub.plan
    billing_url = _absolute_url("/billing/plans/")
    pay_link = _absolute_url(
        f"/billing/checkout/?plan={plan.slug}&period={sub.billing_period}"
    )
    ends = (
        f"{timezone.localtime(sub.current_period_end):%d %B %Y}"
        if sub.current_period_end
        else ""
    )
    recurring = registry.get(sub.recurring_provider) if sub.recurring_id else None
    parts: dict = {
        "eyebrow": "Billing",
        "cta_url": billing_url,
        "cta_label": "View billing",
    }

    def money_rows():
        if payment is None:
            return []
        rows = [("Amount paid", format_money(payment.amount_due, payment.currency))]
        if payment.credit_applied:
            rows.append(
                (
                    "Credit applied",
                    format_money(payment.credit_applied, payment.currency),
                )
            )
        return rows

    if kind == "paid":
        details = [("Plan", plan.name), *money_rows()]
        if payment is not None:
            details.append(("Reference", payment.reference))
            if payment.paid_at:
                details.append(
                    ("Paid on", f"{timezone.localtime(payment.paid_at):%d %B %Y}")
                )
        if ends:
            details.append(("Active until", ends))
        parts.update(
            heading="Payment confirmed",
            preheader=f"Thanks for your payment. You're on {plan.name} until {ends}.",
            details=details,
            small_print="Keep this email for your records.",
        )
    elif kind == "renewed":
        parts.update(
            heading="Your plan has renewed",
            preheader=f"{plan.name} is active until {ends}.",
            details=[("Plan", plan.name), *money_rows(), ("Active until", ends)],
        )
    elif kind == "upcoming":
        from apps.billing.renewals import renews_automatically

        parts.update(
            heading="Your renewal is coming up",
            preheader=f"{plan.name} renews on {ends}.",
            details=[("Plan", plan.name), ("Renews on", ends)],
        )
        if not renews_automatically(sub):
            parts.update(cta_url=pay_link, cta_label="Pay now")
    elif kind in ("due", "due_reminder"):
        from apps.billing.renewals import grace_period

        details = [("Plan", plan.name), ("Was due", ends)]
        if sub.past_due_since:
            keep = timezone.localtime(sub.past_due_since + grace_period())
            details.append(("Pay by", f"{keep:%d %B %Y}"))
        parts.update(
            heading="Your renewal is due",
            details=details,
            notice="Your features stay on for now. Nothing has been deleted.",
            notice_tone="warning",
            cta_url=pay_link,
            cta_label="Renew now",
        )
    elif kind == "finish_payment":
        parts.update(
            heading="Your plan is waiting for payment",
            preheader=f"Finish paying to switch on {plan.name}.",
            details=[("Plan", plan.name), ("Billed", sub.get_billing_period_display())],
            cta_url=_absolute_url("/billing/pay/"),
            cta_label="Finish paying",
        )
    elif kind == "trial_ending":
        parts.update(
            heading="Your free trial ends soon",
            cta_label="Choose a plan",
        )
    elif kind == "trial_ended":
        parts.update(
            heading="Your free trial has ended",
            notice="Nothing has been deleted. Choose a plan to switch your features back on.",
            cta_label="Choose a plan",
        )
    elif kind == "payment_review":
        details = [("Plan", plan.name), *money_rows()]
        if payment is not None:
            details.append(("Reference", payment.reference))
        parts.update(
            heading="Your payment is being confirmed",
            details=details,
            notice="Please don't pay again. We'll email you once it's confirmed.",
            notice_tone="info",
        )
    elif kind == "auto_pay_stopped":
        parts.update(
            heading="Automatic payments have stopped",
            details=[("Plan", plan.name), ("Next renewal", ends)],
            notice="Your plan and paid time don't change.",
            notice_tone="warning",
            cta_url=pay_link,
            cta_label="Set up payment",
        )
    elif kind in ("unpaid", "reminder"):
        parts.update(
            heading="Your renewal payment didn't go through",
            notice="Your features stay on for now. Nothing has been deleted.",
            notice_tone="warning",
        )
        if recurring is None:
            parts.update(cta_url=pay_link, cta_label="Renew now")
    elif kind == "provider_changed":
        parts.update(
            heading="How you pay is changing",
            cta_url=pay_link,
            cta_label="Add payment details",
        )
    elif kind == "expired":
        parts.update(
            heading="Your plan has ended",
            notice="Nothing has been deleted. Renew to switch your features back on.",
            cta_url=pay_link,
            cta_label="Renew plan",
        )
    elif kind == "cancelled":
        parts.update(
            heading="Your plan has ended",
            notice="Nothing has been deleted.",
            cta_label="See plans",
        )
    return parts


def notice_email(sub, kind, payment) -> tuple[str, str, str] | None:
    """(subject, text_body, html_body) for a billing notice, or None for an unknown kind."""
    from apps.email.services.html_email import build_transactional_html

    built = _notice(sub, kind, payment)
    if built is None:
        return None
    subject, body = built
    html_body = build_transactional_html(
        _strip_link_lines(body), subject, **_notice_parts(sub, kind, payment)
    )
    return subject, body, html_body


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
    try:
        built = notice_email(sub, kind, payment)
        if built is None:
            return
        subject, body, html_body = built
        send_system_email(
            to,
            subject,
            text_body=body,
            html_body=html_body,
            sender_label="Billing",
        )
    except Exception:
        logger.exception(
            "send_billing_notice: %s email failed for account %s", kind, account_id
        )
