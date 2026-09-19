"""Onboarding checklist state.

Single source of truth for "where is this workspace in its setup?", shared by
the onboarding page, the dashboard banner, and the always-available floating
widget (via the context processor). Steps reflect the real customer journey:
add a domain → verify DNS → start using email → invite the team.
"""
from django.conf import settings


def _wants_whatsapp(account) -> bool:
    return bool(settings.WHATSAPP_ENABLED) and account.selected_services in (
        account.Services.WHATSAPP,
        account.Services.BOTH,
    )


def _wants_email(account) -> bool:
    # Email steps also cover the fallback case where a WhatsApp-only tenant
    # landed on an instance where WhatsApp is switched off — otherwise the
    # checklist would have no required steps at all.
    return account.selected_services in (
        account.Services.EMAIL,
        account.Services.BOTH,
    ) or not _wants_whatsapp(account)


def _has_ready_whatsapp_number(account) -> bool:
    """A number registered on the Cloud API with working credentials.

    Merely having a number row doesn't mean WhatsApp works, so this gates the
    resumable onboarding state (``WhatsAppBusinessNumber.is_ready``).
    """
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    return any(
        n.is_ready for n in WhatsAppBusinessNumber.objects.filter(account=account)
    )


def _has_sending_domain(account) -> bool:
    from apps.email.models import EmailDomain

    return EmailDomain.objects.filter(account=account).exists()


WHATSAPP_SETUP_URL = "/whatsapp/numbers/"
DOMAIN_SETUP_URL = "/email/domains/"


def advance_onboarding(account) -> str:
    """Recompute the resumable onboarding state from live data, persist it, and
    return the URL of the next service-specific step ('' once complete).

    The pre-account steps (service selection → plan → account information) live
    entirely in the signup wizard; the first persisted state is ACCOUNT_CREATED.
    From there the sequence is, in order, whichever of these still apply:

        WhatsApp connected (Meta Embedded Signup)  →  a sending domain added

    For "Email & WhatsApp" that means Meta onboarding first, then domain setup,
    matching the documented flow. DNS verification / API-key creation stay as
    (still-required) checklist items in ``get_state`` rather than blocking the
    state machine, so slow DNS never strands a user mid-onboarding.
    """
    from apps.accounts.models import Account

    if account.onboarding_state == Account.Onboarding.COMPLETED:
        return ""

    if _wants_whatsapp(account) and not _has_ready_whatsapp_number(account):
        target, url = Account.Onboarding.WHATSAPP_SETUP, WHATSAPP_SETUP_URL
    elif _wants_email(account) and not _has_sending_domain(account):
        target, url = Account.Onboarding.DOMAIN_SETUP, DOMAIN_SETUP_URL
    else:
        target, url = Account.Onboarding.COMPLETED, ""

    if account.onboarding_state != target:
        account.onboarding_state = target
        account.save(update_fields=["onboarding_state"])
    return url


def resume_url(account) -> str:
    """Read-only counterpart to ``advance_onboarding`` — the URL an in-progress
    onboarding should return to, or '' when there's nothing outstanding."""
    from apps.accounts.models import Account

    if account.onboarding_state == Account.Onboarding.COMPLETED:
        return ""
    if _wants_whatsapp(account) and not _has_ready_whatsapp_number(account):
        return WHATSAPP_SETUP_URL
    if _wants_email(account) and not _has_sending_domain(account):
        return DOMAIN_SETUP_URL
    return ""


def first_setup_url(account) -> str:
    """Where to send a freshly created account for its first setup step."""
    return advance_onboarding(account) or "/onboarding/"


def _whatsapp_step(account) -> dict:
    """Checklist step that follows the WhatsApp setup console.

    Done once a number is registered *and* a test message was accepted by Meta
    (outbound proven). A reply/webhook is recommended but never blocks this.
    """
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
    from apps.whatsapp.setup import build_setup_console

    numbers = list(
        WhatsAppBusinessNumber.objects.filter(account=account).order_by("phone_number_id")
    )
    console = build_setup_console(
        numbers,
        embedded_enabled=bool(settings.WHATSAPP_APP_ID and settings.WHATSAPP_CONFIG_ID),
        inbound_seen=False,
    )
    current = console.current
    done = bool(numbers) and console.required_complete
    if not numbers:
        cta = "Connect WhatsApp"
    elif current:
        cta = (current.action or {}).get("label") or "Continue setup"
    else:
        cta = None
    return {
        "key": "whatsapp", "title": "Set up WhatsApp",
        "desc": (
            current.hint if current
            else "Your number is registered and sending. Reply to the test message to confirm inbound."
        ),
        "done": done,
        "url": WHATSAPP_SETUP_URL, "cta": cta,
        "icon": "chat", "optional": False,
    }


def get_state(account) -> dict:
    from apps.accounts.models import Invitation, Membership
    from apps.email.models import EmailApiKey, EmailDomain

    has_domain = EmailDomain.objects.filter(account=account).exists()
    has_verified = EmailDomain.objects.filter(
        account=account, status=EmailDomain.Status.VERIFIED
    ).exists()
    # Deep-link straight to the unverified domain's DNS-check panel instead of
    # just the bare list, so the "verify" step doesn't dump the user back at
    # square one.
    pending_domain = (
        EmailDomain.objects.filter(account=account)
        .exclude(status=EmailDomain.Status.VERIFIED)
        .order_by("pk")
        .first()
    )
    verify_url = f"/email/domains/#domain-card-{pending_domain.pk}" if pending_domain else "/email/domains/"
    has_key = EmailApiKey.objects.filter(account=account, is_active=True).exists()
    has_team = (
        Membership.objects.filter(account=account).count() > 1
        or Invitation.objects.filter(account=account, accepted_at__isnull=True).exists()
    )

    steps = [
        {
            "key": "account", "title": "Create your account",
            "desc": "Your workspace is ready to go.",
            "done": True, "url": None, "cta": None,
            "icon": "check-circle", "optional": False,
        },
        {
            "key": "verify_email", "title": "Verify your email address",
            "desc": "Confirm your email so we can send you delivery reports and alerts.",
            "done": account.email_verified, "url": "/resend-verification/", "cta": "Resend link",
            "icon": "mail", "optional": False,
        },
    ]

    if _wants_whatsapp(account):
        steps.append(_whatsapp_step(account))

    if _wants_email(account):
        steps += [
            {
                "key": "domain", "title": "Add a sending domain",
                "desc": "Add the domain you'll send email from, e.g. mail.yourcompany.com.",
                "done": has_domain, "url": "/email/domains/", "cta": "Add domain",
                "icon": "globe", "optional": False,
            },
            {
                "key": "verify", "title": "Verify your domain",
                "desc": "Add the DNS records and run the DNS check to switch sending on.",
                "done": has_verified, "url": verify_url, "cta": "Verify DNS",
                "icon": "check-circle", "optional": False,
            },
            {
                "key": "use", "title": "Set up your email API",
                "desc": "Generate an API key to start sending email from your app.",
                "done": has_key, "url": "/email/api/", "cta": "Get API key",
                "icon": "code", "optional": False,
            },
        ]

    steps.append({
        "key": "team", "title": "Invite your team",
        "desc": "Bring colleagues into your workspace so they can help manage email.",
        "done": has_team, "url": "/settings/team/", "cta": "Invite teammate",
        "icon": "user", "optional": True,
    })
    steps.append({
        "key": "security", "title": "Review your security settings",
        "desc": "Check your password and active sessions, and confirm your account details are correct.",
        "done": bool(account.security_reviewed_at), "url": "/settings/security/", "cta": "Review",
        "icon": "key", "optional": True,
    })

    required = [s for s in steps if not s["optional"]]
    required_done = sum(1 for s in required if s["done"])
    complete = required_done == len(required)
    # Only essentials drive "next up", so finishing them surfaces the
    # completion state even if an optional step (e.g. inviting the team) remains.
    next_step = next((s for s in steps if not s["done"] and not s["optional"]), None)
    return {
        "steps": steps,
        "complete": complete,
        "required_done": required_done,
        "required_total": len(required),
        "done_count": sum(1 for s in steps if s["done"]),
        "total": len(steps),
        "next_step": next_step,
        "pct": round(required_done / len(required) * 100) if required else 100,
    }
