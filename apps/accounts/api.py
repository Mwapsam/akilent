"""Public API for the accounts module.

This is the single interface other apps should use to access Account data.
Direct imports of apps.accounts.models are not allowed outside of accounts.
"""

from apps.accounts.models import Account


def get_account(pk: int) -> Account:
    """Get an account by primary key.

    Raises:
        Account.DoesNotExist: if account not found
    """
    return Account.objects.get(pk=pk)


def get_account_by_slug(slug: str) -> Account:
    """Get an account by slug.

    Raises:
        Account.DoesNotExist: if account not found
    """
    return Account.objects.get(slug=slug)


def account_exists(pk: int) -> bool:
    """Check if an account exists."""
    return Account.objects.filter(pk=pk).exists()


def list_active_accounts():
    """List all active accounts (admin use)."""
    return Account.objects.filter(is_active=True).order_by("company_name")


def business_fact(account, key: str):
    """One business-profile answer as text (``location``, ``website``, ``payment_methods``,
    ``delivery``, ``what_you_sell``), or None if the owner hasn't answered it."""
    from apps.accounts import profile

    return profile.value(account, key)


def business_facts(account) -> dict:
    """The answered business-profile facts, e.g. ``{"location": "...", "payment_methods": "..."}``."""
    from apps.accounts import profile

    return profile.as_facts(account)


def business_hours_text(account) -> str:
    """Opening hours in words ("Monday to Friday 08:00-17:00, ..."), or "" when none are set."""
    from apps.accounts import business_hours

    return business_hours.describe(account)


def count_members(account) -> int:
    """People on the team, counting pending invitations (a seat is taken once invited).

    An invitation past its EXPIRY_DAYS was never accepted and can no longer be, so it isn't a
    seat: otherwise an ignored invite would permanently block the owner from inviting anyone
    else until they noticed and revoked it by hand.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.accounts.models import Invitation, Membership

    cutoff = timezone.now() - timedelta(days=Invitation.EXPIRY_DAYS)
    return (
        Membership.objects.filter(account=account).count()
        + Invitation.objects.filter(
            account=account, accepted_at__isnull=True, created_at__gte=cutoff
        ).count()
    )


def is_account_admin(user, account) -> bool:
    """Whether ``user`` is an owner or admin of ``account``."""
    from apps.accounts.models import Membership

    return Membership.objects.filter(
        user=user,
        account=account,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()
