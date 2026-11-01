"""Sample renders of every system email for the console preview (/manage/emails/).

Each sample goes through the same function the real send uses, with made-up data and no
database writes, so what an operator sees here is what a recipient gets.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.utils import timezone


def _ctx(**extra):
    from apps.email.services.html_email import site_email_context

    return {**site_email_context(), **extra}


def _verify():
    return render_to_string(
        "accounts/verify_email.html",
        _ctx(link="https://akilent.com/verify/sample-token/"),
    )


def _password_reset():
    return render_to_string(
        "auth/password_reset_email.html",
        _ctx(protocol="https", domain="akilent.com", uid="MQ", token="sample-token"),  # noqa: S106
    )


def _invite():
    invite = SimpleNamespace(
        account=SimpleNamespace(company_name="Mwamba Kitchen"),
        get_role_display="Agent",
        EXPIRY_DAYS=7,
    )
    return render_to_string(
        "accounts/invite_email.html",
        _ctx(
            invite=invite,
            inviter=SimpleNamespace(get_username="Sam"),
            link="https://akilent.com/accept-invitation/sample-token/",
        ),
    )


def _sign_in_code():
    from apps.accounts.email_mfa import code_email_html

    return code_email_html("482913")


def _security_notice():
    from apps.accounts.security_notices import TWO_FACTOR_DISABLED, notice_html

    when = timezone.now().strftime("%d %B %Y at %H:%M UTC")
    return notice_html(
        TWO_FACTOR_DISABLED,
        when=when,
        tail="Your password is now the only way to sign in.",
    )


def _sample_subscription():
    now = timezone.now()
    return SimpleNamespace(
        account=SimpleNamespace(company_name="Mwamba Kitchen"),
        plan=SimpleNamespace(name="Growth", slug="growth"),
        billing_period="monthly",
        current_period_end=now + timedelta(days=30),
        recurring_id="",
        recurring_provider="",
        past_due_since=now,
    )


def _sample_payment():
    return SimpleNamespace(
        amount_due=Decimal("49.00"),
        currency="USD",
        credit_applied=Decimal("0"),
        reference="AK-7Q2M9X",
        paid_at=timezone.now(),
    )


def _billing(kind):
    def render():
        from apps.billing.tasks import notice_email

        built = notice_email(_sample_subscription(), kind, _sample_payment())
        assert built is not None, kind  # every sample kind is a real notice
        return built[2]

    return render


def _manual_payment_admin():
    return render_to_string(
        "billing/manual_payment_admin.html",
        _ctx(
            account="Mwamba Kitchen",
            plan=SimpleNamespace(name="Growth", price_monthly=Decimal("49.00")),
            reference="BANK-2211",
            review_url="https://akilent.com/manage/payments/",
        ),
    )


def _team_notification():
    from apps.email.services.html_email import build_transactional_html

    text = "A customer asked about delivery times and is waiting for a reply."
    return build_transactional_html(
        text,
        "New lead needs a reply",
        heading="New lead needs a reply",
        preheader=text,
        cta_url="https://akilent.com/inbox/",
        cta_label="Open in Akilent",
        reason="You're getting this because an automation in Akilent was set up to tell your team.",
    )


# key -> (label, group, renderer)
SAMPLES = {
    "verify": ("Email verification", "Account", _verify),
    "password-reset": ("Password reset", "Account", _password_reset),
    "invite": ("Team invitation", "Account", _invite),
    "sign-in-code": ("Sign-in verification code", "Security", _sign_in_code),
    "security-notice": ("Security notification", "Security", _security_notice),
    "payment-confirmed": ("Payment confirmation", "Billing", _billing("paid")),
    "renewed": ("Plan renewed", "Billing", _billing("renewed")),
    "unpaid": ("Renewal failed", "Billing", _billing("unpaid")),
    "manual-payment": (
        "Bank transfer to review (operators)",
        "Billing",
        _manual_payment_admin,
    ),
    "team-notification": ("Team notification", "Workspace", _team_notification),
}
