"""Role-based two-factor enforcement.

Off by default (settings.MFA_ENFORCEMENT_ENABLED). When on, the middleware
below runs on every authenticated request, so password login, email-verify
links, invitations, password reset and already-open sessions all hit the same
check. A user who must have MFA but has no confirmed TOTP device is sent to
set one up; a user with a device who is not OTP-verified in this session is
sent to the code prompt.

Email codes never satisfy the requirement. Only a confirmed TOTP device counts.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import BACKEND_SESSION_KEY
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

# Pages a user can reach without a second factor, by URL name.
ALLOWED_URL_NAMES = frozenset(
    {
        "login",
        "logout",
        "2fa-verify",
        "2fa-setup",
        "verify_email",
        "resend_verification",
        "password_reset",
        "password_reset_done",
        "password_reset_confirm",
        "password_reset_complete",
    }
)


def _url_name(request):
    try:
        return resolve(request.path_info).url_name
    except Resolver404:
        return None


def mfa_required(user):
    """True when this user must have a second factor under the current settings."""
    if not getattr(settings, "MFA_ENFORCEMENT_ENABLED", False):
        return False
    if user.is_staff or user.is_superuser:
        return bool(getattr(settings, "MFA_ENFORCE_STAFF", False))
    from apps.accounts.models import Membership

    enforced = tuple(getattr(settings, "MFA_ENFORCED_ROLES", ()))
    if not enforced:
        return False
    return Membership.objects.filter(user=user, role__in=enforced).exists()


def has_second_factor(user):
    """A confirmed TOTP device. Email codes are deliberately not counted."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    return TOTPDevice.objects.filter(user=user, confirmed=True).exists()


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
        # Middleware runs before URL resolution, so request.resolver_match is not
        # set yet. Resolve the path here, or the allowlist never matches and the
        # set-up page redirects to itself.
        if _url_name(request) in ALLOWED_URL_NAMES:
            return None
        if not mfa_required(user):
            return None

        if not has_second_factor(user):
            messages.warning(
                request,
                "Set up two-factor authentication to continue using your account.",
            )
            return redirect("2fa-setup")

        if not user.is_verified():
            # The code prompt completes this session, so it needs the pending login state.
            from apps.accounts.views import begin_second_factor

            begin_second_factor(
                request,
                user,
                request.session.get(
                    BACKEND_SESSION_KEY, "apps.accounts.backends.EmailBackend"
                ),
                request.get_full_path(),
            )
            return redirect("2fa-verify")
        return None
