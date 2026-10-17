"""Reset one user's two-factor authentication after a lost device.

Server-side only, and deliberately narrow:

- Needs a support ticket reference.
- Needs a second platform operator (staff, different from the target) to approve.
- Dry run unless --confirm is given.
- Removes TOTP devices and recovery codes, ends the user's sessions, writes an
  audit entry, and emails the user.

It does not disable enforcement for anyone, and it refuses to act on the
approver's own account.
"""

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import (
    EmailLoginCode,
    EmailMfaEnrolment,
    PasskeyCredential,
    RecoveryCode,
)
from apps.core.audit import platform_record


def _end_sessions(user_pk):
    """Log the user out everywhere, so a recovered session cannot outlive the reset."""
    ended = 0
    for session in Session.objects.all().iterator():
        data = session.get_decoded()
        if str(data.get("_auth_user_id")) == str(user_pk):
            session.delete()
            ended += 1
    return ended


class Command(BaseCommand):
    help = "Reset a user's TOTP devices and recovery codes after a lost device (two-person)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", required=True, help="The locked-out user's email."
        )
        parser.add_argument("--ticket", required=True, help="Support ticket reference.")
        parser.add_argument(
            "--approved-by",
            required=True,
            help="Email of a different platform staff user who approves the reset.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Actually perform the reset. Without this, nothing changes.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        ticket = options["ticket"].strip()
        if not ticket:
            raise CommandError("A non-empty --ticket is required.")

        try:
            target = User.objects.get(email__iexact=options["email"].strip())
        except User.DoesNotExist:
            raise CommandError("No user with that email.") from None

        try:
            approver = User.objects.get(email__iexact=options["approved_by"].strip())
        except User.DoesNotExist:
            raise CommandError("The approver does not exist.") from None

        if not (approver.is_staff or approver.is_superuser):
            raise CommandError("The approver must be a platform staff user.")
        if approver.pk == target.pk:
            raise CommandError("The approver cannot be the user being reset.")

        devices = TOTPDevice.objects.filter(user=target).count()
        codes = RecoveryCode.objects.filter(user=target).count()
        passkeys = PasskeyCredential.objects.filter(user=target).count()
        email_rows = EmailMfaEnrolment.objects.filter(user=target).count()
        self.stdout.write(
            f"Target {target.email}: {devices} TOTP device(s), {codes} recovery code(s), "
            f"{passkeys} passkey(s), email codes {'on' if email_rows else 'off'}."
        )
        if not options["confirm"]:
            self.stdout.write("Dry run. Re-run with --confirm to reset.")
            return

        with transaction.atomic():
            TOTPDevice.objects.filter(user=target).delete()
            RecoveryCode.objects.filter(user=target).delete()
            PasskeyCredential.objects.filter(user=target).delete()
            EmailMfaEnrolment.objects.filter(user=target).delete()
            EmailLoginCode.objects.filter(user=target).delete()
            ended = _end_sessions(target.pk)
            platform_record(
                action="auth.mfa_operator_reset",
                actor=approver,
                resource_type="user",
                resource_id=str(target.pk),
                success=True,
                metadata={
                    "ticket": ticket,
                    "approved_by": approver.email,
                    "devices_removed": devices,
                    "recovery_codes_removed": codes,
                    "passkeys_removed": passkeys,
                    "email_mfa_removed": email_rows,
                    "sessions_ended": ended,
                },
            )

        send_mail(
            subject="Your two-factor authentication was reset",
            message=(
                "Support reset two-factor authentication on your account "
                f"(ticket {ticket}). Sign in and set it up again straight away. "
                "If you did not ask for this, contact support immediately."
            ),
            from_email=None,
            recipient_list=[target.email],
        )
        self.stdout.write(
            self.style.SUCCESS(f"Reset MFA for {target.email} (ticket {ticket}).")
        )
