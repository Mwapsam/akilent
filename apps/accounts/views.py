import json
import logging
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers, patch_cache_control
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts import onboarding as ob
from apps.accounts.forms import SignupForm
from apps.accounts.models import (
    Account,
    EmailMfaEnrolment,
    Membership,
    PasskeyCredential,
)
from apps.accounts.utils import get_current_account, set_current_account
from apps.seo.analytics import queue_ga_event

logger = logging.getLogger(__name__)

_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 15 * 60  # 15 minutes


def _login_rate_key(request, username: str) -> str:
    ip = request.META.get("REMOTE_ADDR", "unknown")
    return f"login_attempts:{ip}:{username.lower()[:254]}"


class _RestoreSiteBrandingMixin:
    """Restore the SiteSettings-based ``site`` template variable.

    LoginView/LogoutView.get_context_data() unconditionally overwrite ``site``
    with django.contrib.sites' get_current_site() (a RequestSite here, since
    that framework isn't installed), clobbering the SiteSettings object our
    own site_context processor already put there — so ``{{ site.app_name }}``
    silently resolves to nothing and templates fall back to "Akilent".
    """

    def get_context_data(self, **kwargs):
        from apps.core.models import SiteSettings

        context = super().get_context_data(**kwargs)  # type: ignore[misc]
        context["site"] = SiteSettings.load()
        return context


_2FA_SESSION_KEY = "_2fa_user_pk"
_2FA_SESSION_BACKEND_KEY = "_2fa_backend"
_2FA_SESSION_NEXT_KEY = "_2fa_next"
_2FA_CHANNEL_KEY = "_2fa_channel"  # "app" or "email" for the prompt
_2FA_EMAIL_SENT_KEY = "_2fa_email_sent"  # a sign-in code was emailed for this sign-in


_2FA_STEPUP_KEY = (
    "_2fa_stepup"  # True when the session is signed in but not yet verified
)


def _code_sent_message(request, result):
    """Tell the user what happened to a code request. Limits are shown as "sent recently"."""
    from apps.accounts import email_mfa

    if result == email_mfa.SENT:
        messages.info(request, "A code is on its way to your email.")
    elif result == email_mfa.RECENT:
        messages.info(
            request,
            "A code was sent just now. Check your email, or wait a minute before asking again.",
        )
    elif result == email_mfa.CAPPED:
        messages.error(
            request,
            "Too many codes have been sent to this account. Try again in an hour, or use another sign-in method.",
        )
    else:
        messages.error(
            request,
            "We couldn't send a code just now. Try again in a few minutes, or contact support.",
        )


def begin_second_factor(request, user, backend, next_url="", stepup=False):
    """Record a pending second factor. The one place the challenge state is written.

    ``stepup`` marks a session that is already signed in but not yet verified, which
    is the enforcement middleware's case. An emailed code cannot verify such a session,
    so email is not offered for a step-up. Off-site ``next`` URLs are dropped here.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    if next_url and not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        next_url = ""
    if not stepup and not request.user.is_authenticated:
        # Rotate the key so a session fixed before sign-in stops working, and clear only
        # the state that belongs to sign-in and recovery. Other session data is kept.
        from apps.accounts import passkeys

        request.session.cycle_key()
        for key in (
            _2FA_SESSION_KEY,
            _2FA_SESSION_BACKEND_KEY,
            _2FA_SESSION_NEXT_KEY,
            _2FA_STEPUP_KEY,
            _2FA_CHANNEL_KEY,
            _2FA_EMAIL_SENT_KEY,
            RECOVERY_DISPLAY_KEY,
            STRONG_VERIFIED_KEY,
            PASSKEY_REAUTH_KEY,
            passkeys.REG_KEY,
            passkeys.AUTH_KEY,
        ):
            request.session.pop(key, None)
    request.session[_2FA_SESSION_KEY] = user.pk
    request.session[_2FA_SESSION_BACKEND_KEY] = backend
    request.session[_2FA_SESSION_NEXT_KEY] = next_url
    request.session[_2FA_STEPUP_KEY] = stepup
    request.session.pop(_2FA_CHANNEL_KEY, None)


class LoginView(_RestoreSiteBrandingMixin, auth_views.LoginView):
    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            key = _login_rate_key(request, request.POST.get("username", ""))
            if (cache.get(key) or 0) >= _LOGIN_MAX_ATTEMPTS:
                messages.error(
                    request,
                    "Too many failed login attempts. Please wait 15 minutes before trying again.",
                )
                return self.get(request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def form_invalid(self, form):
        username = self.request.POST.get("username", "")
        key = _login_rate_key(self.request, username)
        attempts = (cache.get(key) or 0) + 1
        cache.set(key, attempts, _LOGIN_LOCKOUT_SECONDS)
        from apps.core.audit import platform_record

        platform_record(
            action="auth.login_failed",
            ip_address=self.request.META.get("REMOTE_ADDR"),
            success=False,
            metadata={"username": username[:254], "attempts": attempts},
        )
        return super().form_invalid(form)

    def form_valid(self, form):

        user = form.get_user()
        if _challenge_required(user):
            # login() happens only after the code check.
            raw_next = (
                self.request.POST.get("next") or self.request.GET.get("next") or ""
            )
            begin_second_factor(self.request, user, user.backend, raw_next)
            return redirect(reverse("2fa-verify"))
        return super().form_valid(form)


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

    html_email_template_name = "auth/password_reset_email.html"

    def get_form_class(self):
        from apps.accounts.forms import PasswordResetForm

        return PasswordResetForm

    def form_valid(self, form):
        from apps.core.models import SiteSettings
        from apps.email.services.html_email import site_email_context

        self.extra_email_context = {
            "site_name": SiteSettings.load().app_name or "Akilent",
            **site_email_context(),
        }
        return super().form_valid(form)


def _send_verification_email(request, user):
    """Queue an email with a tokened link to confirm the owner's address.

    Returns whether it was queued.

    Sent via Celery rather than inline so a slow/unreachable mail server
    retries in the background instead of 500ing the request — the account
    (User/Account/Membership) is already committed by the time this runs.
    Accounts are created active, so this link only flips ``Account.email_verified``
    (see apps.accounts.tokens); it is not a gate on signing in.
    """
    from apps.accounts.tasks import send_verification_email
    from apps.accounts.tokens import email_verification_token
    from apps.core.models import SiteSettings

    site_name = SiteSettings.load().app_name or "Akilent"
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)
    path = reverse("verify_email", kwargs={"uidb64": uid, "token": token})
    link = request.build_absolute_uri(path)

    try:
        send_verification_email.delay(user.pk, site_name, link)
    except Exception:
        # The broker being down must not turn a committed signup into a 500. Callers
        # tell the user it wasn't sent, so they know to try "Resend link" later.
        logger.exception("could not queue the verification email for user %s", user.pk)
        return False
    return True


def _mark_email_verified(user):
    """Flip ``Account.email_verified`` for the owner's account(s)."""
    from apps.accounts.models import Account

    accounts = Account.objects.filter(
        memberships__user=user, memberships__role=Membership.Role.OWNER
    )
    accounts.filter(email_verified=False).update(
        email_verified=True, email_verified_at=timezone.now()
    )


def _is_free_plan(card) -> bool:
    """Whether a wizard plan card (see _build_signup_wizard_config) costs nothing."""
    return card is not None and card["priceMonthly"] <= 0


def _default_trial_card(by_slug):
    """The wizard card for the plan a plain signup starts on (billing_api
    .default_signup_trial), when it's on offer and free. ``None`` otherwise, and the
    wizard falls back to showing every package."""
    from apps.billing import api as billing_api

    plan, _days = billing_api.default_signup_trial()
    card = by_slug.get(plan.slug) if plan is not None else None
    return card if _is_free_plan(card) else None


def _build_signup_wizard_config(request, *, form_data=None, errors=None):
    from django.middleware.csrf import get_token

    from apps.accounts.forms import signup_offer
    from apps.billing import api as billing_api
    from apps.billing.models import Plan, Subscription
    from apps.billing.money import SYMBOLS, ZERO_DECIMAL, billing_currency
    from apps.billing.pricing import monthly_equivalent, period_options, price_for

    offered, channel_choices = signup_offer()
    offered_keys = [value for value, _, _ in channel_choices]
    # Every card compares the same rows: what differs between the plans on offer.
    comparison = billing_api.signup_comparison(offered)
    periods = [value for value, _ in Subscription.BILLING_PERIOD_CHOICES]
    plans: list[dict[str, Any]] = sorted(
        (
            {
                "slug": p.slug,
                "name": p.name,
                # The channels the plan gives right now (see forms.plan_channels).
                "channels": sorted(channels),
                "priceMonthly": float(p.price_monthly),
                # The price of one whole term (discount included), as checkout
                # works it out before any account credit is taken off.
                "periodPrices": {
                    period: float(price_for(p, period)) for period in periods
                },
                # Each term per month, for the card's headline price only (rounded for
                # longer terms; the card shows the exact total beside it).
                "periodMonthly": {
                    period: float(monthly_equivalent(p, period)) for period in periods
                },
                "trialDays": p.trial_days,
                "popular": p.slug == Plan.PROFESSIONAL,
                "compare": comparison["cards"][p.slug],
            }
            for p, channels in offered
        ),
        key=lambda p: p["priceMonthly"],
    )

    # Figure out where the wizard should open. Steps are named, not numbered: a free or
    # trial signup skips the Package step ("trial" entry), so the numbers would shift.
    #   channels -> package -> account -> review     ("plans" entry)
    #   channels -> account -> review                ("trial" entry)
    by_slug = {p["slug"]: p for p in plans}
    data = form_data or {}
    picks = [c for c in data.get("selected_channels", []) if c in offered_keys]
    plan_slug = data.get("plan", "") if data.get("plan", "") in by_slug else ""
    period = data.get("billing_period") or request.GET.get("period", "")
    if period not in periods:
        period = Subscription.MONTHLY
    if form_data is not None:
        # A submit bounced back: keep the entry it was made through. The page works out
        # which step shows the first error.
        entry = "trial" if data.get("entry") == "trial" else "plans"
        if entry == "trial" and not _is_free_plan(by_slug.get(plan_slug)):
            entry = "plans"
    else:
        req_plan = request.GET.get("plan", "").strip()
        # ?channels=whatsapp,instagram
        req_channels = [
            c.strip() for c in request.GET.get("channels", "").split(",") if c.strip()
        ]
        req_channels = [c for c in req_channels if c in offered_keys]
        matched = by_slug.get(req_plan) if req_plan else None
        if matched and not _is_free_plan(matched):
            # A paid plan from the pricing cards: the channels asked for that it gives,
            # else everything it gives, then straight on to the details.
            picks = [c for c in req_channels if c in matched["channels"]] or list(
                matched["channels"]
            )
            plan_slug, entry = matched["slug"], "plans"
        elif req_channels and not matched:
            # Channel links from the for-pages: pick a package for them.
            picks, entry = req_channels, "plans"
        else:
            # "Start free trial", a free plan's card, or a plain /signup/: the trial (or
            # the free plan asked for), and the customer picks from what it gives.
            trial = matched or _default_trial_card(by_slug)
            if trial is None:
                entry = "plans"
                picks = req_channels
            else:
                plan_slug, entry = trial["slug"], "trial"
                picks = [c for c in req_channels if c in trial["channels"]]
    steps = ["channels", "account", "review"]
    if entry == "plans":
        steps.insert(1, "package")
    # The furthest step the query string gets the customer to; ?step= can ask for an
    # earlier one, never a later one (a shared link with ?step=account and nothing
    # picked opens on Channels).
    if not picks:
        reachable = "channels"
    elif not plan_slug:
        reachable = "package"
    else:
        reachable = "account"
    start_step = reachable
    asked = request.GET.get("step", "")
    if asked in steps and steps.index(asked) < steps.index(reachable):
        start_step = asked

    from apps.billing.views import checkout_options
    from apps.core.models import SiteSettings

    currency = billing_currency()
    payments_enabled = SiteSettings.load().payments_enabled
    return {
        "csrfToken": get_token(request),
        # How a paid plan can be paid for, so the confirm dialog says what comes next.
        "paymentMethods": checkout_options() if payments_enabled else [],
        # Prices are shown in the billing currency checkout charges in.
        "currency": {
            "symbol": SYMBOLS.get(currency, f"{currency} "),
            "zeroDecimal": currency in ZERO_DECIMAL,
        },
        "plans": plans,
        # What every plan has, said once under the cards instead of on each one.
        "included": comparison["included"],
        "channelChoices": [
            {"value": v, "label": label, "description": desc}
            for v, label, desc in channel_choices
        ],
        "periods": period_options(),
        "preselect": {
            "channels": picks,
            "plan": plan_slug,
            "period": period,
            "entry": entry,
            "steps": steps,
            "startStep": start_step,
            # A link that chose the plan or channels wins over progress saved in the
            # browser from an earlier visit.
            "fromQuery": form_data is None
            and bool(request.GET.get("plan") or request.GET.get("channels")),
        },
        "formData": form_data or {},
        "errors": errors or {},
    }


@never_cache
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
        messages.error(
            request,
            "Public sign-ups are currently disabled. Contact us if you need access.",
        )
        return redirect(f"{reverse('landing')}#pricing")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            try:
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

                    assert (
                        form.chosen_plan is not None
                    )  # clean() set it for a valid form
                    account = Account.objects.create(
                        company_name=cd["company_name"],
                        # Onboarding follows the channels they picked, which clean()
                        # checked the plan gives.
                        selected_channels=sorted(cd["selected_channels"]),
                        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
                        billing_email=cd["email"],
                    )
                    Membership.objects.create(
                        user=user, account=account, role=Membership.Role.OWNER
                    )
                    # The Plan the form checked, so the subscription and the channels
                    # can't come from two different lookups.
                    _apply_selected_plan(
                        account, form.chosen_plan, cd["billing_period"]
                    )
                    _set_default_modules(account)
            except IntegrityError:
                # Two submits raced past clean_email (or two addresses share the
                # 150-character username): same answer as the form check gives.
                # Anything else is a real fault and must not be passed off as that.
                if not User.objects.filter(
                    Q(email__iexact=cd["email"]) | Q(username=cd["email"][:150])
                ).exists():
                    raise
                logger.warning("signup: lost a duplicate-account race", exc_info=True)
                form.add_error("email", "An account with this email already exists.")
            else:
                login(request, user, backend="apps.accounts.backends.EmailBackend")
                set_current_account(request, account)
                # The code (and, as a fallback, the link) that confirms their address.
                verification_sent = _send_verify_code(user) == "sent"
                # Sent by the next page the browser loads (dashboard or checkout).
                queue_ga_event(request, "signup_created", plan=form.chosen_plan.slug)
                if cd.get("product_updates"):
                    try:
                        from apps.platform_mail.services.consent import (
                            set_user_preference,
                        )

                        set_user_preference(
                            user, True, source="signup", request=request
                        )
                    except Exception:
                        # A consent write must never fail a signup that has committed.
                        logger.exception(
                            "signup: product-updates opt-in not saved for %s", user.pk
                        )
                logger.info(
                    "signup: created account %s (%s) for user %s",
                    account.pk,
                    ",".join(account.selected_channels),
                    user.pk,
                )

                from apps.billing.models import Subscription

                subscription = getattr(account, "subscription", None)
                if subscription and subscription.status == Subscription.INCOMPLETE:
                    if not verification_sent:
                        messages.warning(
                            request,
                            "We couldn't send your confirmation code just now. You can "
                            "ask for a new one from the setup page once you've paid.",
                        )
                    messages.success(
                        request,
                        f"Almost there — complete payment to activate your {subscription.plan.name} plan.",
                    )
                    from apps.billing.views import PAY_AUTOSTART

                    request.session[PAY_AUTOSTART] = True
                    return redirect(after_signup_url(account))

                if not verification_sent:
                    messages.warning(
                        request,
                        "Your account is ready, but we couldn't send your confirmation "
                        "code just now. Press “Send a new code” in a few minutes.",
                    )
                # Free and trial: confirm the address with the code, or skip to setup.
                return redirect("signup-verify")

        config = _build_signup_wizard_config(
            request,
            # Account-step fields are echoed back so the JS-side `fields` object
            # (bound via x-model, used to gate "Continue" and populate
            # Review) doesn't blank out what the user already typed.
            # Passwords are deliberately never round-tripped.
            form_data={
                "selected_channels": request.POST.getlist("selected_channels"),
                "plan": request.POST.get("plan", ""),
                "billing_period": request.POST.get("billing_period", ""),
                "entry": request.POST.get("entry", ""),
                "first_name": request.POST.get("first_name", ""),
                "last_name": request.POST.get("last_name", ""),
                "email": request.POST.get("email", ""),
                "company_name": request.POST.get("company_name", ""),
            },
            errors=form.errors.get_json_data(escape_html=True),
        )
        return render(
            request,
            "accounts/signup.html",
            {"form": form, "wizard_config": config},
            status=400,
        )

    form = SignupForm()
    config = _build_signup_wizard_config(request)
    return render(
        request, "accounts/signup.html", {"form": form, "wizard_config": config}
    )


def after_signup_url(account) -> str:
    """Where a new account goes next: the payment page while its paid plan is unpaid,
    else its first setup step. Shared with the CSRF failure view, which resumes a
    signup the Back button re-sent. Never checkout itself: see billing.views.pay."""
    from apps.billing.models import Subscription

    subscription = getattr(account, "subscription", None)
    if subscription and subscription.status == Subscription.INCOMPLETE:
        return reverse("billing:pay")
    return ob.first_setup_url(account)


def _set_default_modules(account):
    """Decide which optional tools a brand-new account starts with.

    Orders and payments are deliberately off for a new account: the product a
    business meets on day one is about conversations — reply faster, remember
    people, follow up — and an Orders tab they have no use for is a question
    they have to answer before they can start. They can turn it on any time in
    Settings -> Optional tools.

    Only *new* accounts are touched. An existing account has no stored switch
    and therefore keeps Orders on (no switch = on) — nothing is taken away from
    anyone already using it.
    """
    from apps.billing.api import set_owner_switch

    try:
        set_owner_switch(account, "orders", False)
    except Exception:
        logger.exception("could not set default modules for account %s", account.pk)


def _apply_selected_plan(account, plan, billing_period: str):
    """Honor the plan chosen on the landing page's pricing cards, if any.

    The trial Subscription is auto-created by the billing post_save signal;
    this just swaps its plan so a "Choose Professional" click doesn't
    silently land the user on the default/trial plan instead.

    A plan priced above $0 is never activated here — it's marked
    INCOMPLETE (no trial grace period, regardless of the plan's trial_days)
    and signup() redirects to checkout with the active payment provider
    before the account is usable. Its ``billing_period`` (the term chosen in the
    wizard, already validated by SignupForm) is what that checkout charges for; a
    free plan has no term to keep.
    """
    if plan is None:
        return
    from apps.billing.models import Subscription

    subscription = getattr(account, "subscription", None)
    if subscription is None:
        return
    subscription.plan = plan
    fields = ["plan", "trial_ends_at", "status"]
    if plan.price_monthly > Decimal("0"):
        subscription.status = Subscription.INCOMPLETE
        subscription.trial_ends_at = None
        if billing_period not in dict(Subscription.BILLING_PERIOD_CHOICES):
            raise ValueError(f"Unknown billing period: {billing_period!r}")
        subscription.billing_period = billing_period
        fields.append("billing_period")
    elif plan.trial_days:
        subscription.trial_ends_at = timezone.now() + timedelta(days=plan.trial_days)
        subscription.status = Subscription.TRIALING
    else:
        subscription.trial_ends_at = None
        subscription.status = Subscription.ACTIVE
    subscription.save(update_fields=fields)


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

    # The token is always checked. An already-verified account used to skip the
    # check and log in, which let anyone who knew the pk sign in without a token.
    token_ok = user is not None and email_verification_token.check_token(user, token)

    if user is not None and token_ok:
        _mark_email_verified(user)
        if (
            not request.user.is_authenticated
            and user.is_active
            and _challenge_required(user)
        ):
            # Enrolled users get the same second-factor step as password login.
            begin_second_factor(request, user, "apps.accounts.backends.EmailBackend")
            return redirect(reverse("2fa-verify"))
        if not request.user.is_authenticated and user.is_active:
            login(request, user, backend="apps.accounts.backends.EmailBackend")
            membership = (
                Membership.objects.filter(user=user).select_related("account").first()
            )
            if membership is not None:
                set_current_account(request, membership.account)
        logger.info("verify_email: confirmed email for user %s", user.pk)
        if request.user.is_authenticated:
            messages.success(request, "Your email address is verified. Thanks!")
            return redirect("onboarding")
        return redirect("login")

    return render(request, "accounts/verify_email_invalid.html", status=400)


def _send_verify_code(user) -> str:
    """Email a code that confirms ``user``'s address, with the link as a fallback
    (email_mfa.VERIFY). Returns email_mfa's result: SENT, RECENT, CAPPED or UNAVAILABLE."""
    from apps.accounts import email_mfa

    return email_mfa.send_code(user, email_mfa.VERIFY)


_VERIFY_SEND_MESSAGES = {
    "sent": ("success", "We've sent a new code to {email}."),
    "recent": (
        "info",
        "A code went out moments ago. Give it a minute, then try again.",
    ),
    "capped": (
        "error",
        "You've asked for several codes in the last hour. Use the newest one, or the link "
        "in the email.",
    ),
    "unavailable": (
        "error",
        "We couldn't send a code just now. Please try again in a few minutes.",
    ),
}


@login_required
def signup_verify(request):
    """Confirm the owner's email address with the 6-digit code from signup.

    Shown straight after a free or trial signup (a paid one goes to checkout first and gets
    here from the setup checklist). Never a gate: "Skip for now" goes on to setup, and the
    link in the same email still works. Until it's confirmed, campaigns can't be sent
    (apps.accounts.verification). Limits are email_mfa's, in their own pool, so wrong
    guesses here never lock anyone out of signing in.
    """
    from apps.accounts import email_mfa

    account = (
        Account.objects.filter(
            memberships__user=request.user, memberships__role=Membership.Role.OWNER
        )
        .order_by("pk")
        .first()
    )
    if account is None or account.email_verified:
        return redirect(after_signup_url(account) if account else "dashboard")

    error = ""
    email_error = ""
    if request.method == "POST":
        action = request.POST.get("action", "verify")
        if action == "verify":
            code = "".join(ch for ch in request.POST.get("code", "") if ch.isdigit())
            if email_mfa.guess_limit_reached(request.user, email_mfa.VERIFY):
                error = (
                    "Too many tries for now. Use the link in the email, or request a new "
                    "code in an hour."
                )
            elif len(code) == 6 and email_mfa.check_code(
                request.user, email_mfa.VERIFY, code
            ):
                _mark_email_verified(request.user)
                queue_ga_event(request, "email_verified")
                messages.success(request, "Your email address is confirmed. Thanks!")
                return redirect(after_signup_url(account))
            else:
                error = (
                    "That code isn't right, or it has expired. Check the newest email."
                )
        elif action == "resend":
            level, text = _VERIFY_SEND_MESSAGES[_send_verify_code(request.user)]
            getattr(messages, level)(request, text.format(email=request.user.email))
            return redirect("signup-verify")
        elif action == "change_email":
            email_error = _change_signup_email(request, account)
            if not email_error:
                return redirect("signup-verify")
        elif action == "skip":
            queue_ga_event(request, "email_verify_skipped")
            return redirect(after_signup_url(account))

    return render(
        request,
        "accounts/verify_email_code.html",
        {
            "email": request.user.email,
            "error": error,
            "email_error": email_error,
            "changing": bool(email_error),
            "posted_email": request.POST.get("email", "") if email_error else "",
        },
        status=400 if (error or email_error) else 200,
    )


def _change_signup_email(request, account) -> str:
    """Move a new, still unverified owner to another address: checked like signup's, codes
    for the old address voided (the old link stops working by itself, as the token hashes
    the email), and a code sent to the new one. Returns an error to show, or ""."""
    from django import forms as dj_forms
    from django.core.exceptions import ValidationError

    from apps.accounts.models import EmailLoginCode

    raw = request.POST.get("email", "").strip()
    try:
        email = dj_forms.EmailField(max_length=254).clean(raw)
    except ValidationError:
        return "Enter a valid email address, like name@company.com."
    user = request.user
    if email.lower() == user.email.lower():
        return "That's the address we already have."
    if (
        User.objects.filter(Q(email__iexact=email) | Q(username=email[:150]))
        .exclude(pk=user.pk)
        .exists()
    ):
        return "An account with this email already exists."
    try:
        with transaction.atomic():
            user.email = email
            user.username = email[:150]
            user.save(update_fields=["email", "username"])
            Account.objects.filter(pk=account.pk).update(billing_email=email)
            EmailLoginCode.objects.filter(
                user=user,
                purpose=EmailLoginCode.Purpose.VERIFY,
                used_at__isnull=True,
            ).update(used_at=timezone.now())
    except IntegrityError:
        return "An account with this email already exists."
    logger.info("signup_verify: user %s changed their signup email", user.pk)
    result = _send_verify_code(user)
    if result == "recent":
        messages.success(
            request,
            f"Your email is now {email}. Wait a minute, then press “Send a new code”.",
        )
    else:
        level, text = _VERIFY_SEND_MESSAGES[result]
        getattr(messages, level)(request, text.format(email=email))
    return ""


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
            if _send_verification_email(request, request.user):
                logger.info(
                    "resend_verification: resent link to user %s", request.user.pk
                )
                messages.success(
                    request, "Sent — check your inbox for the confirmation link."
                )
            else:
                messages.error(
                    request,
                    "We couldn't send the email just now. Please try again in a few minutes.",
                )
            return redirect("onboarding")
        return render(
            request, "accounts/resend_verification.html", {"email": request.user.email}
        )

    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            account = _owner_account(user)
            if account is not None and account.email_verified and user.is_active:
                messages.info(
                    request, "That account is already verified — sign in below."
                )
                return redirect("login")
            _send_verification_email(request, user)
            logger.info("resend_verification: resent link to user %s", user.pk)
        return render(request, "accounts/verify_email_sent.html", {"email": email})

    return render(
        request,
        "accounts/resend_verification.html",
        {
            "email": request.GET.get("email", ""),
        },
    )


def marketing_channels() -> dict:
    """What public pages may say about each channel: "live" (switched on: sold by what it
    does) or "" (off: not mentioned). Content Studio follows its publishing channels."""
    from apps.core.channels import enabled_channels

    enabled = enabled_channels()
    out: dict[str, str | bool] = {
        k: "live" if k in enabled else ""
        for k in ("whatsapp", "instagram", "facebook", "email")
    }
    out["content"] = "live" if "live" in (out["instagram"], out["facebook"]) else ""
    # Post results need a chat link, which opens WhatsApp.
    out["results"] = out["content"] == "live" and out["whatsapp"] == "live"
    return out


def use_case(request, slug):
    """A public use-case page (templates/public/for/), only for channels that are on."""
    from django.http import Http404

    from apps.billing import api as billing_api

    ch = marketing_channels()
    if slug in ("missed-enquiries", "shared-inbox"):
        if ch["whatsapp"] != "live":
            raise Http404
        picks = ["whatsapp"] + (["instagram"] if ch["instagram"] == "live" else [])
    elif slug == "content-studio":
        if not ch["content"]:
            raise Http404
        picks = [c for c in ("instagram", "facebook") if ch[c] == "live"]
    else:
        raise Http404
    _, trial_days = billing_api.default_signup_trial()
    return render(
        request,
        f"public/for/{slug}.html",
        {
            "ch": ch,
            "trial_days": trial_days,
            "signup_query": f"?channels={','.join(picks)}" if picks else "",
        },
    )


DEMO_REQUESTS_PER_HOUR = 5  # per client address


def _demo_notify(req) -> None:
    """Email a new demo request to sales: SALES_NOTIFY_EMAIL, else the site's support address.
    The request is already saved, so a missing address or a failed send only logs."""
    from apps.core.models import SiteSettings
    from apps.email.services.send import send_system_email

    to = settings.SALES_NOTIFY_EMAIL or SiteSettings.load().support_email
    if not to:
        logger.warning("Demo request %s saved; no SALES_NOTIFY_EMAIL to notify", req.pk)
        return
    channels = ", ".join(dict(req.CHANNELS).get(c, c) for c in req.channels) or "-"
    body = (
        f"Name: {req.name}\nWork email: {req.email}\nCompany: {req.company}\n"
        f"Team size: {req.get_team_size_display()}\nChannels: {channels}\n\n"
        f"{req.message or '-'}\n"
    )
    try:
        send_system_email(
            to, f"Demo request: {req.company}", text_body=body, sender_label="Sales"
        )
    except Exception:
        logger.exception("Demo request %s saved; notification email failed", req.pk)


@never_cache
def demo(request):
    """Book a demo / contact sales: one real form that saves the request and emails sales."""
    from apps.accounts.forms import DemoRequestForm
    from apps.logs.observability import _client_ip

    form = DemoRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if form.is_bot():
            return redirect(f"{reverse('demo')}?sent=1")
        key = f"demo-requests:{_client_ip(request) or 'unknown'}"
        count = cache.get(key, 0)
        if count >= DEMO_REQUESTS_PER_HOUR:
            form.add_error(
                None, "Too many requests from here. Please try again in an hour."
            )
        else:
            cache.set(key, count + 1, timeout=3600)
            req = form.save(commit=False)
            req.source = (request.META.get("HTTP_REFERER") or "")[:200]
            req.save()
            _demo_notify(req)
            return redirect(f"{reverse('demo')}?sent=1")
    return render(
        request,
        "public/demo.html",
        {
            "form": form,
            "sent": request.GET.get("sent") == "1",
            # "Contact sales" (pricing's Enterprise card) opens the same form under its own name.
            "sales": request.GET.get("topic") == "sales",
        },
    )


LANDING_EDGE_TTL = 600  # seconds Cloudflare may serve the logged-out landing page


def landing(request):
    """Public marketing landing page."""
    from apps.accounts.forms import signup_offer
    from apps.billing import api as billing_api
    from apps.core.models import Testimonial

    # The page needs nothing from the workspace but "Open dashboard": the nav and onboarding
    # context processors skip it (accounts.context_processors._public_page).
    request.public_page = True
    # Only plans signup can sell right now, so a "Choose" button never opens on nothing.
    offered, _ = signup_offer()
    plans = [p for p, _ in offered]
    # Each card is the plan's plan_card (what the signup wizard lists) plus what the landing
    # card leads with: its tagline, the three limits that differ and the annual price.
    data = billing_api.plan_data(
        plans
    )  # three queries for every plan, not three per plan
    pricing = billing_api.pricing_table(offered, data=data)
    cards = [
        {**billing_api.plan_card(p, channels=channels, data=data), **extra}
        for (p, channels), extra in zip(offered, pricing["cards"], strict=True)
    ]
    # "Start free trial" opens signup on the plan a plain /signup/ starts on, by name, so the
    # trial is chosen up front and the click is attributed to it (data-cta-plan).
    trial_plan, trial_days = billing_api.default_signup_trial()
    # len() counts pending messages without marking them read, so the page still shows them.
    personal = request.user.is_authenticated or len(messages.get_messages(request)) > 0
    testimonials = list(Testimonial.shown())
    resp = render(
        request,
        "accounts/landing.html",
        {
            "plans": plans,
            "cards": cards,
            "pricing": pricing,
            "trial_days": trial_days,
            "trial_slug": trial_plan.slug if trial_plan else "",
            "ch": marketing_channels(),
            "testimonials": testimonials,
            # Proof slots: logos and the case-study card come from the same permissioned
            # testimonials, so nothing shows until a real customer has agreed.
            "logos": [t for t in testimonials if t.logo and t.business_name],
            "case_study": next((t for t in testimonials if t.case_study), None),
        },
    )
    # RFC 8288 / RFC 9727 §3 — advertise machine-readable APIs for agent discovery.
    resp["Link"] = (
        '</.well-known/api-catalog>; rel="api-catalog", '
        '</api/schema>; rel="service-desc"; type="application/vnd.oai.openapi+json;version=3.0", '
        '</docs/>; rel="service-doc", '
        '</api/schema>; rel="describedby"; type="application/vnd.oai.openapi+json;version=3.0"'
    )
    # The logged-out page is the same for everyone, so Cloudflare may keep it for ten minutes
    # (its cache rule also skips any request carrying the sessionid or messages cookie, and
    # deploy.yml purges "/"). A signed-in page ("Open dashboard") or one showing a message
    # is personal and is never stored.
    if personal:
        add_never_cache_headers(resp)  # no-store, private
    else:
        patch_cache_control(resp, public=True, max_age=0, s_maxage=LANDING_EDGE_TTL)
    return resp


@login_required
@require_POST
def welcome_tour_seen(request):
    """The welcome tour was finished or closed: don't open it again for this person here."""
    from django.http import HttpResponse

    from apps.accounts.models import Membership
    from apps.accounts.utils import viewing_as

    account = get_current_account(request)
    if account is not None and viewing_as(request) is None:
        Membership.objects.filter(
            user=request.user, account=account, welcome_tour_seen_at__isnull=True
        ).update(welcome_tour_seen_at=timezone.now())
    return HttpResponse(status=204)


@login_required
@require_POST
def next_action_dismiss(request, key):
    """ "Not now" on a next-step suggestion: hide it for this workspace."""
    from django.utils.http import url_has_allowed_host_and_scheme

    from apps.accounts.utils import viewing_as

    account = get_current_account(request)
    if account is not None and viewing_as(request) is None:
        ob.dismiss_next_action(account, key)
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        next_url = reverse("dashboard")
    return redirect(next_url)


@login_required
def onboarding(request):
    from apps.accounts import onboarding as ob

    account = get_current_account(request)
    if account is None:
        return redirect("signup")
    if ob.awaiting_first_payment(account):
        return redirect("billing:pay")  # setup comes after payment

    # Keep the persisted resume-state in step with reality on each visit.
    ob.advance_onboarding(account)
    state = ob.get_state(account, request=request)
    return render(
        request,
        "accounts/onboarding.html",
        {
            "account": account,
            **state,
        },
    )


@login_required
def dashboard(request):
    account = get_current_account(request)
    if account is None:
        from apps.core.utils import is_operator

        if is_operator(request.user):
            return redirect("core:home")  # an operator without a workspace of their own
        return render(request, "accounts/dashboard.html", {"account": None})

    from django.utils import timezone

    from apps.accounts import onboarding as ob

    if ob.awaiting_first_payment(account):
        return redirect("billing:pay")  # setup comes after payment

    hour = timezone.localtime().hour
    greeting = (
        "Good morning"
        if hour < 12
        else "Good afternoon"
        if hour < 18
        else "Good evening"
    )

    state = ob.get_state(account, essentials_only=True, request=request)

    from apps.accounts import welcome

    membership = Membership.objects.filter(user=request.user, account=account).first()
    welcome_form = (
        welcome.WelcomeProfileForm(account=account)
        if welcome.show_card(account, membership)
        else None
    )

    # Only the work queue is rendered on the first byte - it answers the
    # dashboard's primary question and is cheap (three counts). Everything
    # statistical, plus the domain/number/subscription lookups behind it, is
    # fetched by dashboard_panels once the page is on screen, so none of it
    # blocks first paint.
    return render(
        request,
        "accounts/dashboard.html",
        {
            "account": account,
            "onboarding_complete": state["complete"],
            "onboarding_done": state["required_done"],
            "onboarding_total": state["required_total"],
            "onboarding_next": state["next_step"],
            "greeting": greeting,
            "welcome_form": welcome_form,
            **_work_queue(account),
        },
    )


@login_required
@require_POST
def welcome_profile(request):
    """The dashboard's optional "Tell us about your business" card: save the answers, or
    "No thanks" to hide it for good. Never required; nothing waits on it."""
    from apps.accounts import welcome
    from apps.accounts.utils import viewing_as

    account = get_current_account(request)
    membership = (
        Membership.objects.filter(user=request.user, account=account).first()
        if account is not None
        else None
    )
    if viewing_as(request) is None and welcome.show_card(account, membership):
        if request.POST.get("action") == "dismiss":
            welcome.dismiss(account)
        else:
            form = welcome.WelcomeProfileForm(request.POST, account=account)
            if form.is_valid() and form.save():
                messages.success(
                    request, "Thanks, that helps us tailor Akilent for you."
                )
            welcome.dismiss(account)  # answered: don't ask again
    return redirect("dashboard")


@login_required
def dashboard_work_queue(request):
    """The work-queue tiles on their own, polled by HTMX to stay current.

    Returns the same fragment the full page renders inline, so a swap is a
    straight replacement and the counts can never drift from the first paint.
    """
    account = get_current_account(request)
    if account is None:
        return HttpResponseForbidden("no account")
    return render(request, "accounts/_dashboard_work_queue.html", _work_queue(account))


@login_required
def dashboard_panels(request):
    """Everything below the work queue, loaded once the page is on screen.

    Keeps _email_stats / _upcoming_sends / _attention_items - and the joins
    behind them - off the critical path for the first byte.
    """
    account = get_current_account(request)
    if account is None:
        return HttpResponseForbidden("no account")

    from typing import Any

    from apps.core.channels import is_enabled

    numbers: list[Any] = []
    if is_enabled("whatsapp"):
        from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

        numbers = list(
            WhatsAppBusinessNumber.objects.filter(account=account).order_by(
                "phone_number_id"
            )
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
    stats = _email_stats(account, subscription)

    from apps.accounts import onboarding as ob
    from apps.billing import api as billing_api
    from apps.conversations import api as conversations_api

    # Once the essentials are done, point at the next thing worth setting up. Computed
    # here, after first paint, and only then: the full checklist is a dozen-odd queries,
    # the essentials check a handful.
    # Sharing the request lets the full checklist reuse the essentials' work.
    next_recommended = None
    if ob.get_state(account, essentials_only=True, request=request)["complete"]:
        next_recommended = ob.get_state(account, request=request)["next_recommended"]

    return render(
        request,
        "accounts/_dashboard_panels.html",
        {
            "account": account,
            "subscription": subscription,
            "usage_warnings": billing_api.usage_warnings(account),
            "starting_point": conversations_api.starting_point(account),
            "next_recommended": next_recommended,
            "attention_items": _attention_items(
                account, stats, numbers, email_domains, subscription
            ),
            **stats,
            **_upcoming_sends(account),
            **_chatbot_summary(account),
        },
    )


def _work_queue(account):
    """ "What should I do right now?" — the dashboard's primary question.

    Built from the same derived conversation state the inbox uses
    (apps.conversations.state), never from MessageLog or a stored flag, so this
    can never drift from what "Needs attention" shows. New leads is the one
    non-conversation number, kept because it answers the same question
    ("who is waiting on me") from the sales side.
    """
    from django.utils import timezone

    from apps.conversations import followups
    from apps.conversations.state import missed, needs_attention

    now = timezone.now()
    waiting_count = needs_attention(account, now).count()
    missed_count = missed(account, now).count()
    # Business-wide like the other tiles: customers with a follow-up due for anyone, the
    # Whole team tab's own number (followups.due_for_team), so the tile and the tab agree.
    followups_due_count = followups.due_for_team(account, now=now)

    new_leads_count = 0
    try:
        from apps.crm.models import Lead

        new_leads_count = Lead.objects.filter(
            account=account, status=Lead.Status.NEW
        ).count()
    except Exception:
        pass

    return {
        "customers_waiting_count": waiting_count,
        "missed_count": missed_count,
        "new_leads_count": new_leads_count,
        "followups_due_count": followups_due_count,
    }


@login_required
def channels(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    from apps.core.channels import is_enabled

    numbers = []
    if is_enabled("whatsapp"):
        from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

        numbers = list(
            WhatsAppBusinessNumber.objects.filter(account=account).order_by(
                "phone_number_id"
            )
        )

    email_domains = []
    if is_enabled("email"):
        try:
            from apps.email.models import EmailDomain

            email_domains = list(
                EmailDomain.objects.filter(account=account).order_by("domain")
            )
        except Exception:
            pass

    instagram_accounts = []
    if is_enabled("instagram"):
        try:
            from apps.instagram.models.account import InstagramBusinessAccount

            instagram_accounts = list(
                InstagramBusinessAccount.objects.filter(account=account)
            )
        except Exception:
            pass

    facebook_state = "none"
    if is_enabled("facebook"):
        from apps.facebook.services.pages import page_state

        facebook_state = page_state(account)[1]

    return render(
        request,
        "accounts/channels.html",
        {
            "account": account,
            "facebook_state": facebook_state,
            "email_domains": email_domains,
            "email_connected": any(d.status == "verified" for d in email_domains),
            "whatsapp_numbers": numbers,
            "whatsapp_connected": bool(numbers),
            "instagram_accounts": instagram_accounts,
            "instagram_connected": any(iba.is_ready for iba in instagram_accounts),
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
            label = (
                payload.get("subject") or payload.get("name") or job.get_kind_display()
            )
        rows.append({"when": job.fire_at, "label": label, "channel": channel})

    return {"scheduled_count": qs.count(), "upcoming_sends": rows}


def _attention_items(account, stats, numbers, email_domains, subscription):
    """Actionable "needs attention" rows — only those that currently apply."""
    from apps.core.channels import is_enabled

    items = []
    email_on = is_enabled("email")
    verified = stats.get("domains_verified") or 0
    total_domains = len(email_domains)
    if email_on and total_domains == 0:
        items.append(
            {"text": "Add and verify a sending domain", "url": "/email/domains/"}
        )
    elif email_on and verified < total_domains:
        pending = total_domains - verified
        items.append(
            {
                "text": f"{pending} domain{'s' if pending != 1 else ''} awaiting DNS verification",
                "url": "/email/domains/",
            }
        )

    if is_enabled("whatsapp") and not list(numbers):
        items.append({"text": "Connect a WhatsApp number", "url": "/whatsapp/numbers/"})

    usage_pct = stats.get("usage_pct")
    if email_on and usage_pct is not None and usage_pct >= 80:
        items.append(
            {
                "text": f"You've used {usage_pct}% of this month's email quota",
                "url": "/billing/plans/",
            }
        )

    if (stats.get("failed_month") or 0) > 0:
        items.append(
            {
                "text": f"{stats['failed_month']} message{'s' if stats['failed_month'] != 1 else ''} failed this month",
                "url": "/logs/messages/",
            }
        )

    if subscription and getattr(subscription, "status", "") == "past_due":
        items.append(
            {"text": "Your subscription payment is past due", "url": "/billing/plans/"}
        )

    return items


def _email_stats(account, subscription):
    """Real-data dashboard aggregates for an account (no open/click tracking)."""
    from apps.core.channels import is_enabled

    if not is_enabled("email"):
        # Same keys and types as below, zero-valued, without the queries.
        return {
            "sent_month": 0,
            "failed_month": 0,
            "success_rate_display": "—",
            "failed_sub": "0 failed this month",
            "domains_verified": 0,
            "domains_sub": "of 0 total",
            "emails_used": 0,
            "email_quota": None,
            "usage_pct": None,
            "recent_sends": [],
        }

    from django.db.models import Count, Q
    from django.utils import timezone

    from apps.email.models import EmailDomain, EmailMessage

    month_start = timezone.now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

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

    from apps.billing import api as billing_api

    emails_used = month["sent"] + month["failed"] + month["queued"]
    email_quota = billing_api.limit(account, "emails_month") if subscription else None
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


def _chatbot_summary(account) -> dict:
    """Chatbot stats for the dashboard panel: bot list + 30-day session counts."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.chatbot.models import ChatbotConfig
    from apps.chatbot.models.session import ChatSession

    bots = list(ChatbotConfig.objects.filter(account=account).order_by("-created_at"))
    if not bots:
        return {"chatbots": [], "chatbot_sessions_month": 0}

    since = timezone.now() - timedelta(days=30)
    sessions_month = ChatSession.objects.filter(
        chatbot__account=account, started_at__gte=since
    ).count()

    active_bots = [b for b in bots if b.is_active]
    return {
        "chatbots": bots,
        "chatbot_sessions_month": sessions_month,
        "first_active_bot": active_bots[0] if active_bots else None,
    }


# ---------------------------------------------------------------------------
# Two-Factor Authentication
# ---------------------------------------------------------------------------


def _finish_second_factor(request, user, device=None):
    """Log the user in after a second factor passes. Returns where to send them.

    ``device`` is the django-otp device that passed, if there was one. Email codes
    pass None, because they have no OTP device to mark.
    """
    from django.contrib.auth import login as auth_login

    from apps.core.audit import platform_record

    backend = request.session.pop(
        _2FA_SESSION_BACKEND_KEY, "apps.accounts.backends.EmailBackend"
    )
    next_url = (
        request.session.pop(_2FA_SESSION_NEXT_KEY, "") or settings.LOGIN_REDIRECT_URL
    )
    request.session.pop(_2FA_SESSION_KEY, None)
    request.session.pop(_2FA_CHANNEL_KEY, None)
    request.session.pop(_2FA_EMAIL_SENT_KEY, None)
    request.session.pop(_2FA_STEPUP_KEY, None)
    user.backend = backend
    auth_login(request, user)
    # Mark the session as OTP-verified only after login() has rotated it.
    if device is not None:
        _mark_strong_verified(request, device, user)
    platform_record(
        action="auth.2fa_success",
        actor=user,
        ip_address=request.META.get("REMOTE_ADDR"),
    )
    return next_url


def _challenge_required(user):
    """A password login needs a second step if the user has an app, email codes or a passkey.

    Email codes count here even for accounts that must use an app. The code proves the
    login; protected pages still need a verified app or passkey.
    """
    from apps.accounts import email_mfa
    from apps.accounts.mfa import has_second_factor, has_totp
    from apps.accounts.passkeys import has_passkeys

    # Email codes are a sign-in option only for accounts with no app or passkey.
    email_counts = email_mfa.is_enrolled(user) and not has_second_factor(user)
    return has_totp(user) or email_counts or has_passkeys(user)


def _wait_text(info):
    """How long to wait after a lockout, in words."""
    if not info or "locked_until" not in info:
        return "Too many attempts. Try again later."
    seconds = math.ceil((info["locked_until"] - timezone.now()).total_seconds())
    if seconds <= 60:
        return f"Too many attempts. Try again in {max(seconds, 1)} seconds."
    if seconds <= 3600:
        return f"Too many attempts. Try again in {math.ceil(seconds / 60)} minutes."
    return "Too many attempts. Try again later."


def _check_second_factor(user, device, raw, allow_recovery=True):
    """Check an app code or unused recovery code under the device lock.

    Returns (device, None) on success, (None, wait_message) while the lockout is
    active, and (None, None) for a wrong code. The lockout is read inside the same
    lock as the check, so the message and the decision always agree.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts import recovery, totp_storage

    with transaction.atomic():
        locked = TOTPDevice.objects.select_for_update().get(pk=device.pk)
        allowed, info = locked.verify_is_allowed()
        if not allowed:
            return None, _wait_text(info)
        if recovery.looks_like_code(raw):
            if not allow_recovery:
                return None, None
            if recovery.redeem(user, raw):
                locked.throttle_reset(commit=True)
                return locked, None
            locked.throttle_increment(commit=True)
            return None, None
        # verify_token() handles replay (last_t) and increments the lockout on failure.
        # Secret access goes through the storage service, which re-seals the key column.
        return totp_storage.verify_and_seal(locked, raw), None


RECOVERY_DISPLAY_KEY = "recovery_display"
RECOVERY_DISPLAY_SECONDS = 10 * 60


def _mark_strong_verified(request, device, user):
    """Record that a strong factor just passed in this session. Used by sign-in and setup."""
    from django_otp import login as otp_login

    otp_login(request, device)
    request.session[STRONG_VERIFIED_KEY] = {
        "at": timezone.now().isoformat(),
        "user": user.pk,
    }


def _store_recovery_display(request, codes):
    """Keep a freshly generated set in the session, encrypted, for a short display window."""
    from apps.accounts.fields import seal_text

    payload = json.dumps({"at": timezone.now().isoformat(), "codes": codes})
    request.session[RECOVERY_DISPLAY_KEY] = seal_text(payload)


def _load_recovery_display(request):
    """The codes still on display, or None.

    None covers expired, missing and unreadable state alike. Unreadable state is
    cleared and never regenerated here (fail closed): the user must make a new set.
    """
    from apps.accounts import recovery
    from apps.accounts.fields import open_text

    token = request.session.get(RECOVERY_DISPLAY_KEY)
    if not token:
        return None
    raw = open_text(token)
    if raw is None:
        request.session.pop(RECOVERY_DISPLAY_KEY, None)
        return None
    try:
        data = json.loads(raw)
        issued = datetime.fromisoformat(data["at"])
        codes = data["codes"]
    except (ValueError, KeyError, TypeError):
        request.session.pop(RECOVERY_DISPLAY_KEY, None)
        return None
    well_formed = (
        isinstance(codes, list)
        and len(codes) == recovery.CODE_COUNT
        and all(isinstance(c, str) for c in codes)
    )
    if not well_formed or timezone.now() - issued > timedelta(
        seconds=RECOVERY_DISPLAY_SECONDS
    ):
        request.session.pop(RECOVERY_DISPLAY_KEY, None)
        return None
    return codes


def _2fa_verify(request):
    """Step 2 of sign-in. Offers the authenticator app, emailed codes, or a passkey.

    The app and emailed codes are separate channels. An emailed code is never
    checked as an app code, so a mistyped emailed code cannot use up the app's
    throttle. An unknown or unavailable action is refused without any token check.
    Each POST redirects back, so a refresh does not repeat the action.
    """
    from apps.accounts import email_mfa
    from apps.accounts.passkeys import has_passkeys
    from apps.core.audit import platform_record

    user, device = _pending_second_factor(request)
    if user is None:
        return redirect(settings.LOGIN_URL)
    user_pk = user.pk

    stepup = bool(request.session.get(_2FA_STEPUP_KEY))
    # Email codes prove a login only. They are never offered for a step-up.
    from apps.accounts.mfa import has_second_factor

    # Email codes are a sign-in option only for accounts with no app or passkey.
    email_available = (
        email_mfa.is_enrolled(user) and not stepup and not has_second_factor(user)
    )
    channel = request.session.get(_2FA_CHANNEL_KEY, "app") if email_available else "app"
    if device is None and email_available:
        channel = "email"

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "send_email":
            if email_available:
                _code_sent_message(request, email_mfa.send_code(user, "login"))
                request.session[_2FA_CHANNEL_KEY] = "email"
                request.session[_2FA_EMAIL_SENT_KEY] = True
            else:
                messages.error(
                    request, "Email codes are not available for this sign-in."
                )
            return redirect(reverse("2fa-verify"))
        if action == "use_app":
            if device is not None:
                request.session[_2FA_CHANNEL_KEY] = "app"
            return redirect(reverse("2fa-verify"))
        if action is not None:
            # Unknown or unavailable action: no token check and no throttle change.
            return redirect(reverse("2fa-verify"))

        token = request.POST.get("token", "").replace(" ", "")
        if channel == "email":
            passed = email_mfa.check_code(user, "login", token)
            factor_device = (
                None  # an emailed code proves the login; there is no OTP device
            )
        else:
            from apps.accounts import recovery

            if device is not None and recovery.looks_like_code(token):
                messages.error(
                    request,
                    'That looks like a recovery code. Use "Lost your device?" to enter it.',
                )
                return redirect(reverse("2fa-verify"))
            factor_device, wait = (
                _check_second_factor(user, device, token) if device else (None, None)
            )
            if wait:
                platform_record(
                    action="auth.2fa_failed",
                    ip_address=request.META.get("REMOTE_ADDR"),
                    success=False,
                    metadata={"user_pk": user_pk, "channel": "app", "locked": True},
                )
                messages.error(request, wait)
                return redirect(reverse("2fa-verify"))
            passed = factor_device is not None
        if passed:
            return redirect(_finish_second_factor(request, user, factor_device))

        platform_record(
            action="auth.2fa_failed",
            ip_address=request.META.get("REMOTE_ADDR"),
            success=False,
            metadata={"user_pk": user_pk, "channel": channel},
        )
        if channel == "email" and email_mfa.guess_limit_reached(user):
            messages.error(
                request,
                "Too many attempts. Try again in an hour, or use a passkey if you have one.",
            )
        else:
            messages.error(
                request, "That code didn't work. Check the code and try again."
            )
        return redirect(reverse("2fa-verify"))

    if channel == "email" and not request.session.get(_2FA_EMAIL_SENT_KEY):
        # Email-only accounts: send the code when the step opens, as password reset does.
        request.session[_2FA_EMAIL_SENT_KEY] = True
        _code_sent_message(request, email_mfa.send_code(user, "login"))

    return render(
        request,
        "auth/2fa_verify.html",
        {
            "channel": channel,
            "email_available": email_available,
            "has_app": device is not None,
            "recovery_available": device is not None and channel == "app",
            "passkey_available": has_passkeys(user),
        },
    )


def recovery_sign_in(request):
    """Step 2 with a recovery code. Its own page, so the code field never takes these.

    Shows nothing about the account until the code is accepted, not even how many
    codes are left.
    """
    from apps.accounts import recovery
    from apps.core.audit import platform_record

    user, device = _pending_second_factor(request)
    if user is None or device is None:
        # Recovery codes belong to an app. Without one, go back to the sign-in step.
        return redirect(reverse("2fa-verify"))

    if request.method == "POST":
        raw = request.POST.get("code", "")
        if not recovery.looks_like_code(raw):
            messages.error(
                request,
                "Recovery codes have 16 letters and numbers. Check the code and try again.",
            )
            return redirect(reverse("2fa-recovery"))
        passed, wait = _check_second_factor(user, device, raw)
        if wait:
            platform_record(
                action="auth.2fa_failed",
                ip_address=request.META.get("REMOTE_ADDR"),
                success=False,
                metadata={"user_pk": user.pk, "channel": "recovery", "locked": True},
            )
            messages.error(request, wait)
            return redirect(reverse("2fa-recovery"))
        if passed is not None:
            return redirect(_finish_second_factor(request, user, passed))
        platform_record(
            action="auth.2fa_failed",
            ip_address=request.META.get("REMOTE_ADDR"),
            success=False,
            metadata={"user_pk": user.pk, "channel": "recovery"},
        )
        messages.error(
            request,
            "That recovery code didn't work. Each code works once. Check it and try again.",
        )
        return redirect(reverse("2fa-recovery"))

    return render(request, "auth/2fa_recovery.html")


@login_required
@never_cache
def recovery_codes_page(request):
    """Show the codes from the setup or regenerate step, for one display window."""
    from apps.accounts.mfa import has_totp

    codes = _load_recovery_display(request)
    return render(
        request,
        "accounts/2fa_recovery_codes.html",
        {
            "codes": codes,
            "account": get_current_account(request),
            "can_regenerate": has_totp(request.user),
        },
    )


@login_required
@require_POST
def recovery_codes_confirm(request):
    """The user confirms they saved the codes. Removes the temporary copy."""
    if request.POST.get("saved") != "1":
        messages.error(request, "Tick the box to confirm you saved your codes.")
        return redirect(reverse("settings-recovery-codes"))
    request.session.pop(RECOVERY_DISPLAY_KEY, None)
    messages.success(request, "Recovery codes saved. Keep them somewhere safe.")
    return redirect("settings-security")


@login_required
@require_POST
def recovery_codes_regenerate(request):
    """Replace all recovery codes. Needs the password and a current app code or unused
    recovery code, as set out in docs/security/recovery-policy.md.

    The old codes stop working in the same transaction that creates the new set.
    """
    from apps.accounts import recovery
    from apps.accounts.mfa import has_totp
    from apps.accounts.security_notices import RECOVERY_REGENERATED, notify_after_commit
    from apps.core.audit import platform_record

    user = request.user
    # Recovery codes are only for accounts with an authenticator app. Email codes
    # never authorise a new set, so an email-only account cannot be given codes.
    if not has_totp(user):
        messages.error(
            request, "Turn on an authenticator app before making recovery codes."
        )
        return redirect("settings-security")
    ok, wait = _reauth_outcome(
        user, request.POST.get("password", ""), request.POST.get("token", "")
    )
    if not ok:
        platform_record(
            action="auth.recovery_codes_regen_failed",
            actor=user,
            ip_address=request.META.get("REMOTE_ADDR"),
            success=False,
            metadata={"locked": bool(wait)},
        )
        messages.error(request, wait or "Check your password and code, then try again.")
        return redirect("2fa-setup")

    with transaction.atomic():
        codes = recovery.generate_codes(user)
        platform_record(
            action="auth.recovery_codes_regenerated",
            actor=user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        notify_after_commit(user, RECOVERY_REGENERATED)
    _store_recovery_display(request, codes)
    return redirect("settings-recovery-codes")


@login_required
def _2fa_setup(request):
    """Enable or disable TOTP 2FA from the security settings page."""
    import base64
    import io

    import segno
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.core.audit import platform_record

    account = get_current_account(request)
    user = request.user
    existing = TOTPDevice.objects.filter(user=user, confirmed=True).first()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "disable" and existing:
            # Re-authenticate: a password and a current factor, so a stolen
            # session alone cannot switch 2FA off.
            password = request.POST.get("password", "")
            ok, wait = _reauth_outcome(user, password, request.POST.get("token", ""))
            if not ok:
                platform_record(
                    action="auth.2fa_disable_failed",
                    account=account,
                    actor=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                    success=False,
                    metadata={"locked": bool(wait)},
                )
                messages.error(
                    request,
                    wait
                    or "Enter your password and a current code to turn off two-factor authentication.",
                )
                return redirect("2fa-setup")
            from apps.accounts.models import RecoveryCode
            from apps.accounts.security_notices import (
                TWO_FACTOR_DISABLED,
                notify_after_commit,
            )

            # The change, its audit record and the notice are one unit: the email is
            # sent only if this transaction commits.
            with transaction.atomic():
                TOTPDevice.objects.filter(user=user).delete()
                RecoveryCode.objects.filter(user=user).delete()
                platform_record(
                    action="auth.2fa_disabled",
                    account=account,
                    actor=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                notify_after_commit(user, TWO_FACTOR_DISABLED)
            request.session.pop(RECOVERY_DISPLAY_KEY, None)
            messages.success(request, "Two-factor authentication disabled.")
            return redirect("settings-security")

        if action == "enable":
            from apps.accounts import recovery

            token = request.POST.get("token", "").replace(" ", "")
            # Retrieve the unconfirmed device created during GET.
            from apps.accounts import totp_storage

            pending = TOTPDevice.objects.filter(user=user, confirmed=False).first()
            device = totp_storage.verify_and_seal(pending, token) if pending else None
            if device:
                device.confirmed = True
                device.save(update_fields=["confirmed"])
                _mark_strong_verified(request, device, user)
                platform_record(
                    action="auth.2fa_enabled",
                    account=account,
                    actor=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                # Kept encrypted in the session for a short window; never logged.
                codes = recovery.generate_codes(user)
                _store_recovery_display(request, codes)
                return redirect("settings-recovery-codes")
            messages.error(
                request,
                "That code didn't match. Check that your phone's time is set automatically, "
                "then enter the newest code.",
            )
            # Fall through to re-render setup with the same (unconfirmed) device.

    if existing:
        return render(
            request, "accounts/settings_2fa.html", {"enabled": True, "account": account}
        )

    # Create (or reuse) an unconfirmed device and show the QR code.
    from apps.accounts import totp_storage

    pending = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if pending is None:
        pending = totp_storage.create_device(user, name="default", confirmed=False)
    device = totp_storage.load(pending)
    if device is None:
        messages.error(request, "Two-factor set-up is unavailable. Contact support.")
        return redirect("settings-security")
    uri = device.config_url  # otpauth://totp/...

    # Render QR as an inline SVG (no file I/O, no Pillow required).
    qr = segno.make(uri, error="M")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=4, border=2)
    qr_svg = base64.b64encode(buf.getvalue()).decode()

    return render(
        request,
        "accounts/settings_2fa.html",
        {
            "enabled": False,
            "account": account,
            "qr_svg": qr_svg,
            "secret": base64.b32encode(device.bin_key).decode().rstrip("="),
        },
    )


@login_required
def email_mfa_settings(request):
    """Turn email sign-in codes on or off.

    Email codes are a sign-in option only for accounts with no authenticator app or
    passkey, so they can never weaken an account that has one. Turning them on or off
    needs the password and an emailed code.
    """
    from apps.accounts import email_mfa
    from apps.accounts.mfa import has_second_factor
    from apps.core.audit import platform_record

    user = request.user
    account = get_current_account(request)
    enrolled = email_mfa.is_enrolled(user)
    strong_factor = has_second_factor(user)
    too_strong = "Email codes are for accounts without an authenticator app or passkey."

    if request.method == "POST":
        action = request.POST.get("action")
        code = request.POST.get("token", "")
        password = request.POST.get("password", "")
        if action == "send_enrol" and not enrolled:
            if strong_factor:
                messages.error(request, too_strong)
            else:
                _code_sent_message(request, email_mfa.send_code(user, "enrol"))
        elif action == "confirm_enrol" and not enrolled:
            if strong_factor:
                messages.error(request, too_strong)
            elif user.check_password(password) and email_mfa.check_code(
                user, "enrol", code
            ):
                EmailMfaEnrolment.objects.get_or_create(user=user)
                platform_record(
                    action="auth.email_mfa_enabled",
                    account=account,
                    actor=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, "Email sign-in codes are on.")
                return redirect("settings-email-mfa")
            else:
                messages.error(
                    request, "Check your password and the code, then try again."
                )
        elif action == "send_disable" and enrolled:
            _code_sent_message(request, email_mfa.send_code(user, "disable"))
        elif action == "confirm_disable" and enrolled:
            if user.check_password(password) and email_mfa.check_code(
                user, "disable", code
            ):
                EmailMfaEnrolment.objects.filter(user=user).delete()
                platform_record(
                    action="auth.email_mfa_disabled",
                    account=account,
                    actor=user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, "Email sign-in codes are off.")
                return redirect("settings-email-mfa")
            messages.error(request, "Check your password and the code, then try again.")
        return redirect("settings-email-mfa")

    return render(
        request,
        "accounts/settings_email_mfa.html",
        {"enrolled": enrolled, "strong_factor": strong_factor, "account": account},
    )


PASSKEY_REAUTH_KEY = "passkey_reauth_at"
PASSKEY_REAUTH_SECONDS = 300
# Set when an app, passkey or security key passes. Email codes never set it.
STRONG_VERIFIED_KEY = "strong_verified_at"
STRONG_VERIFIED_SECONDS = 15 * 60


def _pending_second_factor(request, with_device=True):
    """(user, confirmed app device) for the second step in progress, or (None, None).

    The one place that reads the pending state. A pointer to a user who no longer
    exists is cleared here, so callers never repeat that cleanup.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    pk = request.session.get(_2FA_SESSION_KEY)
    if not pk:
        return None, None
    user = User.objects.filter(pk=pk).first()
    if user is None:
        request.session.pop(_2FA_SESSION_KEY, None)
        return None, None
    device = None
    if with_device:
        device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    return user, device


def _pending_user(request):
    """The user who passed their password and is now completing a second factor."""
    user, _ = _pending_second_factor(request, with_device=False)
    return user


def _json_body(request):
    """Parse a JSON object from the request. Returns None if it is not one."""
    try:
        body = json.loads(request.body or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _reauth_is_fresh(request):
    """True if the user re-authenticated for a passkey change in the last few minutes."""
    stamp = request.session.get(PASSKEY_REAUTH_KEY)
    if stamp is None:
        return False
    return timezone.now() - datetime.fromisoformat(stamp) <= timedelta(
        seconds=PASSKEY_REAUTH_SECONDS
    )


def _recently_verified(request):
    """True if this session passed a strong factor recently and is still OTP-verified."""
    stamp = request.session.get(STRONG_VERIFIED_KEY)
    is_verified = getattr(request.user, "is_verified", None)
    if not stamp or is_verified is None or not is_verified():
        return False
    if stamp.get("user") != request.user.pk:
        return False
    return timezone.now() - datetime.fromisoformat(stamp["at"]) <= timedelta(
        seconds=STRONG_VERIFIED_SECONDS
    )


def _passkey_change_allowed(request):
    """A recent strong sign-in, or a recent password and code, allows a passkey change."""
    return _reauth_is_fresh(request) or _recently_verified(request)


REAUTH_PASSWORD_LIMIT = 5
REAUTH_PASSWORD_WINDOW_SECONDS = 15 * 60


def _password_guess(user, password):
    """Check a password against the per-account limit. Returns (ok, wait_message).

    Every attempt is counted before its password is checked, using an atomic
    increment, so parallel requests cannot all pass the limit before any of them
    counts. A correct password clears the count. Once the limit is reached the
    password is not checked at all.
    """
    key = f"reauth-password-failures:{user.pk}"
    cache.add(key, 0, REAUTH_PASSWORD_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        attempts = 1
        cache.set(key, 1, REAUTH_PASSWORD_WINDOW_SECONDS)
    if attempts > REAUTH_PASSWORD_LIMIT:
        return False, "Too many failed attempts. Try again in 15 minutes."
    if not user.check_password(password):
        return False, None
    cache.delete(key)
    return True, None


def _reauth_outcome(user, password, token, allow_recovery=True):
    """Password, then a current second factor, for a sensitive change.

    Returns (ok, wait_message). ``allow_recovery`` is False for passkey changes, which
    the recovery-code policy does not cover. The factor is the authenticator app if
    one is enrolled, otherwise an emailed code.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts import email_mfa

    ok, wait = _password_guess(user, password)
    if not ok:
        return False, wait
    token = token.replace(" ", "")
    totp = TOTPDevice.objects.filter(user=user, confirmed=True).first()
    if totp is not None:
        factor, wait = _check_second_factor(
            user, totp, token, allow_recovery=allow_recovery
        )
        return factor is not None, wait
    return email_mfa.check_code(user, "reauth", token), None


def _app_code_refusal(device, token):
    """Explain a refused app code. A code already used to sign in cannot be used again.

    Authenticator codes are single use per 30-second step, so the code shown when the user
    signed in is refused here. Saying so stops people retrying the same code.
    """
    from django_otp.oath import TOTP
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts import totp_storage

    fresh = TOTPDevice.objects.get(pk=device.pk)
    totp_storage.load(fresh)
    try:
        now_totp = TOTP(fresh.bin_key, fresh.step, fresh.t0, fresh.digits, fresh.drift)
        current_step, current_code = now_totp.t(), now_totp.token()
    finally:
        totp_storage.reseal(fresh)
    if fresh.last_t == current_step and token == f"{current_code:0{fresh.digits}d}":
        return "That code was just used to sign in. Wait for the next code in your app, then try again."
    return "That app code didn't work. Check it and try again."


def _verify_for_passkey_enrolment(user, method, password, token):
    """Confirm it's the account owner, using the one method the user chose.

    ``password``: only for accounts not required to use MFA. Where MFA is required, a
    passkey would otherwise satisfy MFA with the password alone.
    ``app``: a current authenticator app code. Recovery codes are not accepted here.
    ``email``: the code emailed by passkey_reauth_send.
    Returns (ok, message).
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from apps.accounts import email_mfa
    from apps.accounts.mfa import mfa_required

    token = token.replace(" ", "")
    if method == "password":
        if mfa_required(user):
            return (
                False,
                "Your account must use an authenticator app or passkey. Choose another option.",
            )
        ok, wait = _password_guess(user, password)
        return ok, wait or "That password didn't match. Try again."
    if method == "app":
        totp = TOTPDevice.objects.filter(user=user, confirmed=True).first()
        if totp is None:
            return False, "Set up an authenticator app first, or choose another option."
        factor, wait = _check_second_factor(user, totp, token, allow_recovery=False)
        if factor is not None:
            return True, None
        return False, wait or _app_code_refusal(totp, token)
    if method == "email":
        if mfa_required(user):
            # Email codes never count as a strong factor where MFA is required.
            return (
                False,
                "Your account must use an authenticator app or passkey. Choose another option.",
            )
        if email_mfa.check_code(user, "reauth", token):
            return True, None
        return False, "That email code didn't work. Check it and try again."
    return False, "Choose how to confirm it's you."


@login_required
@require_POST
def passkey_reauth_send(request):
    """Email a re-authentication code to users who have no authenticator app."""
    from apps.accounts import email_mfa

    _code_sent_message(request, email_mfa.send_code(request.user, "reauth"))
    return redirect("settings-passkeys")


@login_required
@require_POST
def passkey_register_options(request):
    from apps.accounts import passkeys

    body = _json_body(request)
    if body is None:
        return JsonResponse({"ok": False}, status=400)
    # A re-authentication from the last few minutes covers a retry after a cancelled
    # browser prompt, so the user is not asked for a new code.
    if not _passkey_change_allowed(request):
        # The same one-method choice as the QR path: app, email or password.
        ok, message = _verify_for_passkey_enrolment(
            request.user,
            str(body.get("method", "")),
            str(body.get("password", "")),
            str(body.get("token", "")),
        )
        if not ok:
            return JsonResponse({"ok": False, "error": message}, status=400)
        request.session[PASSKEY_REAUTH_KEY] = timezone.now().isoformat()
    return HttpResponse(
        passkeys.begin_registration(request, request.user),
        content_type="application/json",
    )


@login_required
@require_POST
def passkey_register_finish(request):
    """Verify and store a new passkey. The re-authentication stamp survives a failed try.

    It is cleared only after a passkey is stored, so one cancelled browser prompt does
    not cost the user another emailed code.
    """
    from apps.accounts import passkeys
    from apps.core.audit import platform_record

    body = _json_body(request)
    if body is None or not _passkey_change_allowed(request):
        return JsonResponse({"ok": False}, status=400)
    name = body.get("name", "")
    passkey = passkeys.finish_registration(
        request,
        request.user,
        body.get("credential"),
        name if isinstance(name, str) else "",
    )
    if passkey is None:
        return JsonResponse({"ok": False}, status=400)
    request.session.pop(PASSKEY_REAUTH_KEY, None)
    platform_record(
        action="auth.passkey_added",
        actor=request.user,
        ip_address=request.META.get("REMOTE_ADDR"),
        metadata={"passkey_pk": passkey.pk},
    )
    from apps.accounts.security_notices import PASSKEY_ADDED, notify_after_commit

    notify_after_commit(request.user, PASSKEY_ADDED)
    return JsonResponse({"ok": True})


@login_required
def passkey_settings(request):
    """List passkeys and register a new one. Removal is a separate, re-authenticated POST."""
    from apps.accounts.mfa import has_totp, mfa_required

    user = request.user
    return render(
        request,
        "accounts/settings_passkeys.html",
        {
            "passkeys": PasskeyCredential.objects.filter(user=user),
            "totp_enrolled": has_totp(user),
            "password_allowed": not mfa_required(user),
            "email_allowed": not mfa_required(user),
            "reauth_fresh": _passkey_change_allowed(request),
            "account": get_current_account(request),
        },
    )


@login_required
@require_POST
def passkey_remove(request, pk):
    """Remove a passkey. Needs the password and a current second factor."""
    from apps.core.audit import platform_record

    user = request.user
    passkey = get_object_or_404(PasskeyCredential, pk=pk, user=user)
    ok, wait = _reauth_outcome(
        user,
        request.POST.get("password", ""),
        request.POST.get("token", ""),
        allow_recovery=False,
    )
    if not ok:
        messages.error(request, wait or "Check your password and code, then try again.")
        return redirect("settings-passkeys")
    passkey.delete()
    platform_record(
        action="auth.passkey_removed",
        actor=user,
        ip_address=request.META.get("REMOTE_ADDR"),
        metadata={"passkey_pk": pk},
    )
    messages.success(request, "Passkey removed.")
    return redirect("settings-passkeys")


@require_POST
def passkey_signin_options(request):
    from apps.accounts import passkeys
    from apps.accounts.passkeys import has_passkeys

    user = _pending_user(request)
    if user is None or not has_passkeys(user):
        return JsonResponse({"ok": False}, status=400)
    return HttpResponse(
        passkeys.begin_authentication(request, user), content_type="application/json"
    )


@require_POST
def passkey_signin_finish(request):
    from apps.accounts import passkeys

    user = _pending_user(request)
    body = _json_body(request)
    if user is None or body is None:
        return JsonResponse({"ok": False}, status=400)
    passkey = passkeys.finish_authentication(request, user, body.get("credential"))
    if passkey is None:
        return JsonResponse({"ok": False}, status=400)
    return JsonResponse({"redirect": _finish_second_factor(request, user, passkey)})
