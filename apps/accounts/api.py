"""Public API for the accounts module.

This is the single interface other apps should use to access Account data.
Direct imports of apps.accounts.models are not allowed outside of accounts.
"""
from django.core.exceptions import ObjectDoesNotExist

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


def is_account_admin(user, account) -> bool:
    """Whether ``user`` is an owner or admin of ``account``."""
    from apps.accounts.models import Membership

    return Membership.objects.filter(
        user=user, account=account, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()
