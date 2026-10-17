"""Role-based two-factor enforcement.

Off by default (settings.MFA_ENFORCEMENT_ENABLED). When on, the middleware
below runs on every authenticated request, so password login, email-verify
links, invitations, password reset and already-open sessions all hit the same
check.

What counts as a second factor for enforcement: a confirmed authenticator app
or a passkey. Email codes never count. An email code proves a login, not a
strong factor, so an email-only account is still sent to set up an app or
passkey before it can reach protected pages.

A required user who is already verified in this session passes straight through.
A required user with no strong factor is sent to set-up. A required user with a
strong factor who is not verified is sent to the code prompt.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import BACKEND_SESSION_KEY
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

# Reachable without any second factor: sign-in, sign-out, the factor prompts and
# account recovery.
ALLOWED_URL_NAMES = frozenset(
    {
        "login",
        "logout",
        "2fa-verify",
        "2fa-recovery",
        "verify_email",
        "resend_verification",
        "password_reset",
        "password_reset_done",
        "password_reset_confirm",
        "password_reset_complete",
        "passkey-signin-options",
        "passkey-signin-finish",
    }
)

# Enrolment pages. Reachable only while the user has no strong factor. Once they have
# one, these need a verified session like any other page. This stops a session that
# passed only an email code from adding its own app.
ENROL_URL_NAMES = frozenset(
    {
        "2fa-setup",
        "settings-passkeys",
        "settings-email-mfa",
        "passkey-register-options",
        "passkey-register-finish",
        "passkey-reauth-send",
    }
)


def _url_name(request):
    try:
        return resolve(request.path_info).url_name
    except Resolver404:
        return None


def mfa_required(user):
    """True when this user must have a strong second factor under the current settings."""
    if not getattr(settings, "MFA_ENFORCEMENT_ENABLED", False):
        return False
    if user.is_staff or user.is_superuser:
        return bool(getattr(settings, "MFA_ENFORCE_STAFF", False))
    from apps.accounts.models import Membership

    enforced = tuple(getattr(settings, "MFA_ENFORCED_ROLES", ()))
    if not enforced:
        return False
    return Membership.objects.filter(user=user, role__in=enforced).exists()


def has_totp(user):
    """A confirmed authenticator app."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def has_second_factor(user):
    """A strong factor for enforcement: a confirmed authenticator app or a passkey."""
    from apps.accounts.passkeys import has_passkeys

    return has_totp(user) or has_passkeys(user)


class MfaEnforcementMiddleware:
    """Send users who must have MFA through set-up or the code prompt.

    Must sit after django_otp's OTPMiddleware (so is_verified() works) and after
    the messages middleware (so the set-up notice can be shown).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        redirect_response = self._redirect_if_needed(request)
        if redirect_response is not None:
            return redirect_response
        return self.get_response(request)

    def _redirect_if_needed(self, request):
        if not getattr(settings, "MFA_ENFORCEMENT_ENABLED", False):
            return None
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if not mfa_required(user):
            return None
        # A verified session already proved a strong factor this session.
        if user.is_verified():
            return None
        # Middleware runs before URL resolution, so resolve the path here, or the
        # allowlists never match and the set-up page redirects to itself.
        name = _url_name(request)
        if name in ALLOWED_URL_NAMES:
            return None
        has_factor = has_second_factor(user)
        if name in ENROL_URL_NAMES and not has_factor:
            return None

        if not has_factor:
            messages.warning(
                request,
                "Set up an authenticator app or passkey to continue using your account.",
            )
            return redirect("2fa-setup")

        # The code prompt completes this session, so it needs the pending login state.
        from apps.accounts.views import begin_second_factor

        begin_second_factor(
            request,
            user,
            request.session.get(
                BACKEND_SESSION_KEY, "apps.accounts.backends.EmailBackend"
            ),
            request.get_full_path(),
            stepup=True,
        )
        return redirect("2fa-verify")
