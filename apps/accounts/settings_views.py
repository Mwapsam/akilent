"""Self-service account settings: profile, security (password), and team.

These give non-technical owners a front door for "user management" and
"security setup" without touching the Django admin or contacting support.
"""
import logging

from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.forms import AcceptInvitationForm, InviteForm, ProfileForm
from apps.accounts.models import Invitation, Membership
from apps.accounts.utils import get_current_account, is_ajax, set_current_account

logger = logging.getLogger(__name__)

MANAGE_ROLES = (Membership.Role.OWNER, Membership.Role.ADMIN)


def _account_and_membership(request):
    """Return ``(account, membership)`` for the request's current workspace."""
    account = get_current_account(request)
    if account is None:
        return None, None
    membership = Membership.objects.filter(user=request.user, account=account).first()
    return account, membership


def _can_manage_team(membership) -> bool:
    return membership is not None and membership.role in MANAGE_ROLES


def _send_invitation_email(request, invite):
    from apps.core.models import SiteSettings
    from apps.email.services.send import send_system_email
    from apps.email.services.system_templates import render_system_email

    site_name = SiteSettings.load().app_name or "Automator"
    link = request.build_absolute_uri(
        reverse("accept-invitation", kwargs={"token": invite.token})
    )
    ctx = {"invite": invite, "site_name": site_name, "link": link, "inviter": invite.invited_by}
    subject, body = render_system_email(
        "accounts.invite",
        ctx,
        fallback_subject_template="accounts/invite_email_subject.txt",
        fallback_body_template="accounts/invite_email.txt",
    )
    send_system_email(
        to_email=invite.email,
        subject=subject,
        text_body=body,
    )


# --- Profile ------------------------------------------------------------------

@login_required
def settings_profile(request):
    account = get_current_account(request)
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("settings-profile")
    else:
        form = ProfileForm(instance=request.user)
    return render(request, "accounts/settings_profile.html", {
        "form": form, "account": account, "active_tab": "profile",
    })


# --- Security -----------------------------------------------------------------

@login_required
def settings_security(request):
    account, membership = _account_and_membership(request)
    if (
        account is not None
        and account.security_reviewed_at is None
        and membership is not None
        and membership.role == Membership.Role.OWNER
    ):
        account.security_reviewed_at = timezone.now()
        account.save(update_fields=["security_reviewed_at"])
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            # Keep the user signed in after their password hash changes.
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed.")
            return redirect("settings-security")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, "accounts/settings_security.html", {
        "form": form, "account": account, "active_tab": "security",
    })


# --- Optional tools -----------------------------------------------------------

# (feature_name for apps.billing.api, checkbox label, one-line explanation of the goal).
# Framed as opt-OUT: apps.billing.api.module_enabled() already treats "no row" as
# enabled, so an account with neither row checked here already has both on today —
# this page must not silently turn anything off on first render (see R1.5a).
OPTIONAL_TOOLS = [
    ("crm", "Track potential sales",
     "See customers who are considering buying, and where each one stands."),
    ("commerce", "Create orders and collect payments",
     "Turn a conversation into an order, and send a payment link."),
]


@login_required
def settings_tools(request):
    from apps.billing import api as billing_api

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        for feature_name, _label, _hint in OPTIONAL_TOOLS:
            enabled = request.POST.get(feature_name) == "on"
            billing_api.set_module_enabled(account, feature_name, enabled)
        messages.success(request, "Settings saved.")
        return redirect("settings-tools")

    tools = [
        {"feature": f, "label": label, "hint": hint, "enabled": billing_api.module_enabled(account, f)}
        for f, label, hint in OPTIONAL_TOOLS
    ]
    return render(request, "accounts/settings_tools.html", {
        "account": account, "active_tab": "tools", "tools": tools,
    })


# --- Team ---------------------------------------------------------------------

@login_required
def settings_team(request):
    account, membership = _account_and_membership(request)
    if account is None:
        return redirect("dashboard")
    members = (
        Membership.objects.filter(account=account)
        .select_related("user")
        .order_by("role", "user__username")
    )
    invitations = Invitation.objects.filter(account=account, accepted_at__isnull=True)
    return render(request, "accounts/settings_team.html", {
        "account": account,
        "membership": membership,
        "can_manage": _can_manage_team(membership),
        "members": members,
        "invitations": invitations,
        "active_tab": "team",
    })


@login_required
@require_POST
def invite_create(request):
    account, membership = _account_and_membership(request)
    if account is None or not _can_manage_team(membership):
        messages.error(request, "You don't have permission to invite teammates.")
        return redirect("settings-team")

    form = InviteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid email and role.")
        return redirect("settings-team")

    email = form.cleaned_data["email"]
    role = form.cleaned_data["role"]

    if Membership.objects.filter(account=account, user__email__iexact=email).exists():
        messages.error(request, f"{email} is already a member of this workspace.")
        return redirect("settings-team")

    invite = Invitation.objects.filter(
        account=account, email=email, accepted_at__isnull=True
    ).first()
    if invite is not None:
        invite.role = role
        invite.invited_by = request.user
        invite.save(update_fields=["role", "invited_by"])
    else:
        invite = Invitation.objects.create(
            account=account, email=email, role=role, invited_by=request.user
        )

    try:
        _send_invitation_email(request, invite)
    except Exception as exc:  # email backend / SMTP failure shouldn't 500
        logger.error("invite email to %s failed: %s", email, exc)
        messages.warning(
            request,
            f"Invitation saved, but the email to {email} couldn't be sent. "
            "Share the invite link from the pending list.",
        )
        return redirect("settings-team")

    messages.success(request, f"Invitation sent to {email}.")
    return redirect("settings-team")


@login_required
@require_POST
def invite_revoke(request, pk):
    account, membership = _account_and_membership(request)
    if account is None or not _can_manage_team(membership):
        messages.error(request, "You don't have permission to do that.")
        return redirect("settings-team")
    invite = get_object_or_404(
        Invitation, pk=pk, account=account, accepted_at__isnull=True
    )
    email = invite.email
    invite.delete()
    messages.success(request, f"Invitation to {email} revoked.")
    return redirect("settings-team")


@login_required
@require_POST
def member_role(request, pk):
    account, membership = _account_and_membership(request)
    if account is None or not _can_manage_team(membership):
        messages.error(request, "You don't have permission to do that.")
        return redirect("settings-team")
    target = get_object_or_404(Membership, pk=pk, account=account)
    if target.role == Membership.Role.OWNER:
        messages.error(request, "The workspace owner's role can't be changed.")
        return redirect("settings-team")
    new_role = request.POST.get("role")
    if new_role not in (Membership.Role.MEMBER, Membership.Role.ADMIN):
        messages.error(request, "Pick a valid role.")
        return redirect("settings-team")
    target.role = new_role
    target.save(update_fields=["role"])
    messages.success(
        request, f"{target.user.get_username()} is now {target.get_role_display()}."
    )
    return redirect("settings-team")


@login_required
@require_POST
def member_remove(request, pk):
    account, membership = _account_and_membership(request)

    def fail(msg):
        if is_ajax(request):
            return JsonResponse({"error": msg}, status=403)
        messages.error(request, msg)
        return redirect("settings-team")

    if account is None or not _can_manage_team(membership):
        return fail("You don't have permission to do that.")
    target = get_object_or_404(Membership, pk=pk, account=account)
    if target.role == Membership.Role.OWNER:
        return fail("The workspace owner can't be removed.")
    if target.user == request.user:
        return fail("You can't remove yourself from the workspace.")

    username = target.user.get_username()
    target.delete()
    # Flash, then let the page reload — the message renders as a toast on the
    # next request (a JS toast would be lost when we navigate via `redirect`).
    messages.success(request, f"{username} removed from the workspace.")
    if is_ajax(request):
        return JsonResponse({"redirect": reverse("settings-team")})
    return redirect("settings-team")


# --- Accept invitation (works logged in or out) -------------------------------

def accept_invitation(request, token):
    invite = (
        Invitation.objects.select_related("account", "invited_by")
        .filter(token=token)
        .first()
    )
    if invite is None or invite.is_accepted or invite.is_expired:
        return render(request, "accounts/accept_invitation.html", {"invalid": True}, status=400)

    # Already signed in: confirm, then join the workspace as this user.
    if request.user.is_authenticated:
        if request.method == "POST":
            Membership.objects.get_or_create(
                user=request.user, account=invite.account,
                defaults={"role": invite.role},
            )
            invite.accepted_at = timezone.now()
            invite.save(update_fields=["accepted_at"])
            set_current_account(request, invite.account)
            messages.success(request, f"You've joined {invite.account.company_name}.")
            return redirect("dashboard")
        return render(request, "accounts/accept_invitation.html", {"invite": invite, "mode": "join"})

    # Logged out, but an account already exists for this email: send to sign in.
    if User.objects.filter(email__iexact=invite.email).exists():
        login_url = f"{reverse('login')}?next={reverse('accept-invitation', kwargs={'token': token})}"
        return render(request, "accounts/accept_invitation.html", {
            "invite": invite, "mode": "signin", "login_url": login_url,
        })

    # Brand-new user: register them (the invite itself proves the email).
    if request.method == "POST":
        form = AcceptInvitationForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save(commit=False)
                user.email = invite.email
                user.is_active = True
                user.save()
                Membership.objects.create(
                    user=user, account=invite.account, role=invite.role
                )
                invite.accepted_at = timezone.now()
                invite.save(update_fields=["accepted_at"])
            login(request, user, backend="apps.accounts.backends.EmailBackend")
            set_current_account(request, invite.account)
            messages.success(request, f"Welcome to {invite.account.company_name}!")
            return redirect("dashboard")
    else:
        form = AcceptInvitationForm()
    return render(request, "accounts/accept_invitation.html", {
        "invite": invite, "mode": "register", "form": form,
    })
