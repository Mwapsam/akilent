"""Token generator for the non-blocking email-verification link.

Accounts are created active and the owner is logged in straight away, so we
can't lean on ``default_token_generator`` — its hash mixes in ``last_login``,
which changes on the very next sign-in and would break the link. This variant
is stable across logins but single-use: once ``Account.email_verified`` flips
to ``True`` the hash changes and the old link stops validating. A password
change still invalidates it too (``user.password`` stays in the hash).
"""

from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        from apps.accounts.models import Membership

        membership = (
            Membership.objects.filter(user=user, role=Membership.Role.OWNER)
            .select_related("account")
            .first()
        )
        verified_flag = ""
        if membership is not None:
            account = membership.account
            verified_flag = f"{account.email_verified}{account.email_verified_at or ''}"
        return f"{user.pk}{user.password}{user.email}{verified_flag}{timestamp}"


email_verification_token = EmailVerificationTokenGenerator()
