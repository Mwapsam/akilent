import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from apps.accounts import onboarding as ob
from apps.accounts.forms import SignupForm
from apps.accounts.models import Account, Membership
from apps.accounts.utils import get_current_account, set_current_account

logger = logging.getLogger(__name__)


class _RestoreSiteBrandingMixin:
    """Restore the SiteSettings-based ``site`` template variable.

    LoginView/LogoutView.get_context_data() unconditionally overwrite ``site``
    with django.contrib.sites' get_current_site() (a RequestSite here, since
    that framework isn't installed), clobbering the SiteSettings object our
    own site_context processor already put there — so ``{{ site.app_name }}``
    silently resolves to nothing and templates fall back to "Automator".
    """

    def get_context_data(self, **kwargs):
        from apps.core.models import SiteSettings

        context = super().get_context_data(**kwargs)
        context["site"] = SiteSettings.load()
        return context


class LoginView(_RestoreSiteBrandingMixin, auth_views.LoginView):
    pass


class LogoutView(_RestoreSiteBrandingMixin, auth_views.LogoutView):
    pass


class PasswordResetView(auth_views.PasswordResetView):
    """Password reset that emails the dashboard's configured app name via the active send provider.

    Django's default resolves ``site_name`` from django.contrib.sites (not
    installed here), which falls back to the request's raw domain instead of
    the branding configured in SiteSettings.

    Uses a custom PasswordResetForm that sends via send_system_email (SES/SMTP)
    instead of Django's hardcoded EMAIL_BACKEND, and respects suppression lists.
    """

    def get_form_class(self):
        from apps.accounts.forms import PasswordResetForm
        return PasswordResetForm

    def form_valid(self, form):
        from apps.core.models import SiteSettings

        self.extra_email_context = {"site_name": SiteSettings.load().app_name or "Automator"}
        return super().form_valid(form)


def _send_verification_email(request, user):
    """Queue an email with a tokened link to confirm the owner's address.

    Sent via Celery rather than inline so a slow/unreachable mail server
    retries in the background instead of 500ing the request — the account
    (User/Account/Membership) is already committed by the time this runs.
    Accounts are created active, so this link only flips ``Account.email_verified``
    (see apps.accounts.tokens); it is not a gate on signing in.
    """
    from apps.accounts.tasks import send_verification_email
    from apps.accounts.tokens import email_verification_token
    from apps.core.models import SiteSettings

    site_name = SiteSettings.load().app_name or "Automator"
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)
    path = reverse("verify_email", kwargs={"uidb64": uid, "token": token})
    link = request.build_absolute_uri(path)

    send_verification_email.delay(user.pk, site_name, link)


def _mark_email_verified(user):
    """Flip ``Account.email_verified`` for the owner's account(s)."""
    from apps.accounts.models import Account

    accounts = Account.objects.filter(
        memberships__user=user, memberships__role=Membership.Role.OWNER
    )
    accounts.filter(email_verified=False).update(
        email_verified=True, email_verified_at=timezone.now()
    )


_ACCOUNT_PROFILE_FIELDS = (
    "legal_name", "website", "industry", "company_size", "phone",
    "address_line1", "address_line2", "city", "state_region",
    "postal_code", "country",
)


def _plan_feature_bullets(plan) -> list:
    """Short marketing bullets for a plan card in the signup wizard.

    Mirrors the capability rows shown on templates/billing/plans.html, kept in
    Python so the wizard config is a plain JSON blob.
    """
    def _cap(value, unit):
        return f"Unlimited {unit}" if value == -1 else f"{value} {unit}"

    bullets = []
    if plan.service_type in ("whatsapp", "both"):
        bullets.append(_cap(plan.max_whatsapp_numbers, "WhatsApp number(s)"))
        bullets.append(_cap(plan.max_conversations_per_month, "conversations/mo"))
    if plan.service_type in ("email", "both"):
        bullets.append(_cap(plan.max_emails_per_month, "emails/mo"))
        if plan.email_apis:
            bullets.append("REST API + SMTP relay")
        if plan.bulk_email:
            bullets.append("Bulk / campaign sending")
        if plan.inbound_email:
            bullets.append("Inbound email processing")
        if plan.detailed_analytics:
            bullets.append("Detailed analytics & insights")
    bullets.append(_cap(plan.max_automation_rules, "automation rule(s)"))
    if plan.has_priority_support:
        bullets.append("Priority support")
    return bullets


def _build_signup_wizard_config(request, *, form_data=None, errors=None):
    from django.middleware.csrf import get_token
    from apps.billing.models import Plan
    from apps.core.models import SiteSettings

    plans = [
        {
            "slug": p.slug,
            "name": p.name,
            "serviceType": p.service_type,
            "priceMonthly": float(p.price_monthly),
            "trialDays": p.trial_days,
            "popular": p.slug == Plan.PROFESSIONAL,
            "features": _plan_feature_bullets(p),
        }
        for p in Plan.objects.filter(is_active=True).order_by("price_monthly")
    ]

    site = SiteSettings.load()
    whatsapp_enabled = bool(settings.WHATSAPP_ENABLED) and getattr(
        site, "whatsapp_enabled", True
    )

    # Figure out where the wizard should open.
    services = (form_data or {}).get("selected_services", "")
    plan_slug = (form_data or {}).get("plan", "")
    if not services and not plan_slug:
        req_plan = request.GET.get("plan", "").strip()
        req_services = request.GET.get("services", "").strip()
        matched = next((p for p in plans if p["slug"] == req_plan), None) if req_plan else None
        if matched:
            services, plan_slug = matched["serviceType"], matched["slug"]
            start_step = 2
        elif req_services in dict(Account.Services.choices):
            services, start_step = req_services, 1
        else:
            start_step = 0
    else:
        start_step = 2

    return {
        "csrfToken": get_token(request),
        "whatsappEnabled": whatsapp_enabled,
        "plans": plans,
        "serviceChoices": [
            {"value": v, "label": l} for v, l in Account.Services.choices
        ],
        "preselect": {
            "services": services,
            "plan": plan_slug,
            "startStep": start_step,
        },
        "formData": form_data or {},
        "errors": errors or {},
    }


def signup(request):
    """Service-first self-service signup wizard.

    One page, one POST: the user picks services, a package, then enters
    personal + business details. That single submit creates the User, the
    Account (with its business profile and selected services), and the owner
    Membership. The user is logged in immediately; a verification email is sent
    in the background and surfaced as an onboarding step rather than gating
    access. The trial Subscription is created by the billing post_save signal
    on Account and re-pointed to the chosen plan by ``_apply_selected_plan``.
    """
    if request.user.is_authenticated:
        return redirect("dashboard")

    from apps.core.models import SiteSettings
    if not SiteSettings.load().signups_enabled:
        messages.error(request, "Public sign-ups are currently disabled. Contact us if you need access.")
        return redirect(f"{reverse('landing')}#pricing")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            with transaction.atomic():
                user = form.save(commit=False)
                user.email = cd["email"]
                # Email is the login credential (EmailBackend); username is
                # just an internal identifier Django's User model requires.
                user.username = cd["email"][:150]
                user.first_name = cd["first_name"]
                user.last_name = cd["last_name"]
                user.is_active = True
                user.save()

                account = Account.objects.create(
                    company_name=cd["company_name"],
                    selected_services=cd["selected_services"],
                    onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
                    billing_email=cd["email"],
                    **{f: cd.get(f, "") for f in _ACCOUNT_PROFILE_FIELDS},
                )
                Membership.objects.create(
                    user=user, account=account, role=Membership.Role.OWNER
                )
                _apply_selected_plan(account, cd.get("plan"))

            login(request, user, backend="apps.accounts.backends.EmailBackend")
            set_current_account(request, account)
            _send_verification_email(request, user)
            logger.info(
                "signup: created account %s (%s) for user %s",
                account.pk, account.selected_services, user.pk,
            )
            messages.success(
                request,
                "Your account is ready. We've emailed a link to verify your address.",
            )
            return redirect(ob.first_setup_url(account))

        config = _build_signup_wizard_config(
            request,
            # Step 2 fields are echoed back so the JS-side `fields` object
            # (bound via x-model, used to gate "Continue" and populate
            # Review) doesn't blank out what the user already typed.
            # Passwords are deliberately never round-tripped.
            form_data={
                "selected_services": request.POST.get("selected_services", ""),
                "plan": request.POST.get("plan", ""),
                "first_name": request.POST.get("first_name", ""),
                "last_name": request.POST.get("last_name", ""),
                "email": request.POST.get("email", ""),
                "phone": request.POST.get("phone", ""),
                "company_name": request.POST.get("company_name", ""),
                "address_line1": request.POST.get("address_line1", ""),
                "city": request.POST.get("city", ""),
                "country": request.POST.get("country", ""),
            },
            errors=form.errors.get_json_data(escape_html=True),
        )
        return render(request, "accounts/signup.html", {"form": form, "wizard_config": config}, status=400)

    form = SignupForm()
    config = _build_signup_wizard_config(request)
    return render(request, "accounts/signup.html", {"form": form, "wizard_config": config})


def _apply_selected_plan(account, plan_slug):
    """Honor the plan chosen on the landing page's pricing cards, if any.

    The trial Subscription is auto-created by the billing post_save signal;
    this just swaps its plan so a "Choose Professional" click doesn't
    silently land the user on the default/trial plan instead.
    """
    if not plan_slug:
        return
    from apps.billing.models import Plan

    plan = Plan.objects.filter(slug=plan_slug, is_active=True).first()
    if plan is None:
        return
    subscription = getattr(account, "subscription", None)
    if subscription is None:
        return
    subscription.plan = plan
    if plan.trial_days:
        subscription.trial_ends_at = timezone.now() + timedelta(days=plan.trial_days)
        subscription.status = subscription.__class__.TRIALING
    else:
        subscription.trial_ends_at = None
        subscription.status = subscription.__class__.ACTIVE
    subscription.save(update_fields=["plan", "trial_ends_at", "status"])


def verify_email(request, uidb64, token):
    """Confirm the owner's email address from the emailed link.

    Accounts are created active and the owner is already signed in, so this
    just marks ``Account.email_verified`` and clears that onboarding step. It
    works whether or not the visitor happens to be logged in (e.g. opening the
    link on another device).
    """
    from apps.accounts.tokens import email_verification_token

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    already_verified = user is not None and not Account.objects.filter(
        memberships__user=user,
        memberships__role=Membership.Role.OWNER,
        email_verified=False,
    ).exists()

    if user is not None and (already_verified or email_verification_token.check_token(user, token)):
        _mark_email_verified(user)
        if not request.user.is_authenticated and user.is_active:
            login(request, user, backend="apps.accounts.backends.EmailBackend")
            membership = Membership.objects.filter(user=user).select_related("account").first()
            if membership is not None:
                set_current_account(request, membership.account)
        logger.info("verify_email: confirmed email for user %s", user.pk)
        if request.user.is_authenticated:
            messages.success(request, "Your email address is verified. Thanks!")
            return redirect("onboarding")
        return redirect("login")

    return render(request, "accounts/verify_email_invalid.html", status=400)


def resend_verification(request):
    """Send a fresh confirmation link if the first was lost or expired.

    Avoids confirming/denying account existence to a third party by always
    showing the same "check your email" outcome. A signed-in owner whose email
    is still unverified can trigger it straight from the onboarding step.
    """
    def _owner_account(user):
        return (
            Account.objects.filter(
                memberships__user=user, memberships__role=Membership.Role.OWNER
            )
            .order_by("pk")
            .first()
        )

    if request.user.is_authenticated:
        account = _owner_account(request.user)
        if account is not None and account.email_verified:
            messages.info(request, "Your email address is already verified.")
            return redirect("onboarding")
        if request.method == "POST":
            _send_verification_email(request, request.user)
            logger.info("resend_verification: resent link to user %s", request.user.pk)
            messages.success(request, "Sent — check your inbox for the confirmation link.")
            return redirect("onboarding")
        return render(request, "accounts/resend_verification.html", {"email": request.user.email})

    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            account = _owner_account(user)
            if account is not None and account.email_verified and user.is_active:
                messages.info(request, "That account is already verified — sign in below.")
                return redirect("login")
            _send_verification_email(request, user)
            logger.info("resend_verification: resent link to user %s", user.pk)
        return render(request, "accounts/verify_email_sent.html", {"email": email})

    return render(request, "accounts/resend_verification.html", {
        "email": request.GET.get("email", ""),
    })


def landing(request):
    """Public marketing landing page."""
    from apps.billing.models import Plan

    plans = Plan.objects.filter(is_active=True).order_by("price_monthly")
    return render(request, "accounts/landing.html", {"plans": plans})


@login_required
def onboarding(request):
    from apps.accounts import onboarding as ob

    account = get_current_account(request)
    if account is None:
        return redirect("signup")

    # Keep the persisted resume-state in step with reality on each visit.
    ob.advance_onboarding(account)
    state = ob.get_state(account)
    return render(request, "accounts/onboarding.html", {
        "account": account,
        **state,
    })


@login_required
def dashboard(request):
    account = get_current_account(request)
    if account is None:
        # Authenticated user with no tenant (e.g. a staff-only admin user).
        return render(request, "accounts/dashboard.html", {"account": None})

    from django.conf import settings

    numbers = []
    if settings.WHATSAPP_ENABLED:
        from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

        numbers = WhatsAppBusinessNumber.objects.filter(account=account).order_by(
            "phone_number_id"
        )

    email_domains = []
    try:
        from apps.email.models import EmailDomain

        email_domains = list(
            EmailDomain.objects.filter(account=account).order_by("domain")
        )
    except Exception:
        pass

    subscription = getattr(account, "subscription", None)

    from django.utils import timezone

    from apps.accounts import onboarding as ob

    hour = timezone.localtime().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"

    state = ob.get_state(account)
    stats = _email_stats(account, subscription)
    scheduling = _upcoming_sends(account)
    attention = _attention_items(
        account, stats, numbers, email_domains, subscription
    )

    return render(
        request,
        "accounts/dashboard.html",
        {
            "account": account,
            "numbers": numbers,
            "email_domains": email_domains,
            "subscription": subscription,
            "onboarding_complete": state["complete"],
            "onboarding_done": state["required_done"],
            "onboarding_total": state["required_total"],
            "onboarding_next": state["next_step"],
            "attention_items": attention,
            "greeting": greeting,
            **stats,
            **scheduling,
        },
    )


def _upcoming_sends(account, limit=5):
    """Next scheduled jobs across all channels — for the dashboard panel + KPI."""
    from django.utils import timezone

    try:
        from apps.scheduler.models import ScheduledJob
    except Exception:
        return {"scheduled_count": 0, "upcoming_sends": []}

    qs = ScheduledJob.objects.filter(
        account=account,
        status=ScheduledJob.Status.SCHEDULED,
        fire_at__gte=timezone.now(),
    ).order_by("fire_at")

    rows = []
    for job in qs[:limit]:
        channel = "whatsapp" if job.kind == ScheduledJob.Kind.WHATSAPP else "email"
        label = ""
        if job.target_campaign_id and getattr(job, "target_campaign", None):
            label = getattr(job.target_campaign, "name", "") or ""
        if not label:
            payload = job.template_payload or {}
            label = payload.get("subject") or payload.get("name") or job.get_kind_display()
        rows.append({"when": job.fire_at, "label": label, "channel": channel})

    return {"scheduled_count": qs.count(), "upcoming_sends": rows}


def _attention_items(account, stats, numbers, email_domains, subscription):
    """Actionable "needs attention" rows — only those that currently apply."""
    from django.conf import settings

    items = []
    verified = stats.get("domains_verified") or 0
    total_domains = len(email_domains)
    if total_domains == 0:
        items.append({"text": "Add and verify a sending domain", "url": "/email/domains/"})
    elif verified < total_domains:
        pending = total_domains - verified
        items.append({
            "text": f"{pending} domain{'s' if pending != 1 else ''} awaiting DNS verification",
            "url": "/email/domains/",
        })

    if settings.WHATSAPP_ENABLED and not list(numbers):
        items.append({"text": "Connect a WhatsApp number", "url": "/whatsapp/numbers/"})

    usage_pct = stats.get("usage_pct")
    if usage_pct is not None and usage_pct >= 80:
        items.append({
            "text": f"You've used {usage_pct}% of this month's email quota",
            "url": "/billing/plans/",
        })

    if (stats.get("failed_month") or 0) > 0:
        items.append({
            "text": f"{stats['failed_month']} message{'s' if stats['failed_month'] != 1 else ''} failed this month",
            "url": "/logs/messages/",
        })

    if subscription and getattr(subscription, "status", "") == "past_due":
        items.append({"text": "Your subscription payment is past due", "url": "/billing/plans/"})

    return items


def _email_stats(account, subscription):
    """Real-data dashboard aggregates for an account (no open/click tracking)."""
    from django.db.models import Count, Q
    from django.utils import timezone

    from apps.email.models import EmailDomain, EmailMessage

    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    msgs = EmailMessage.objects.filter(account=account)
    month = msgs.filter(created_at__gte=month_start).aggregate(
        sent=Count("id", filter=Q(status=EmailMessage.Status.SENT)),
        failed=Count("id", filter=Q(status=EmailMessage.Status.FAILED)),
        queued=Count("id", filter=Q(status=EmailMessage.Status.QUEUED)),
    )
    attempted = month["sent"] + month["failed"]
    success_rate = round(month["sent"] / attempted * 100) if attempted else None

    domains = EmailDomain.objects.filter(account=account).aggregate(
        total=Count("id"),
        verified=Count("id", filter=Q(status=EmailDomain.Status.VERIFIED)),
    )

    emails_used = month["sent"] + month["failed"] + month["queued"]
    email_quota = getattr(getattr(subscription, "plan", None), "max_emails_per_month", None)
    usage_pct = None
    if email_quota and email_quota > 0:
        usage_pct = min(round(emails_used / email_quota * 100), 100)

    return {
        "sent_month": month["sent"],
        "failed_month": month["failed"],
        "success_rate_display": f"{success_rate}%" if success_rate is not None else "—",
        "failed_sub": f"{month['failed']} failed this month",
        "domains_verified": domains["verified"],
        "domains_sub": f"of {domains['total']} total",
        "emails_used": emails_used,
        "email_quota": email_quota,
        "usage_pct": usage_pct,
        "recent_sends": list(msgs.order_by("-created_at")[:6]),
    }
