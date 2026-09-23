"""get_current_account is memoised per request.

A view plus both account context processors each ask for the current account,
so this used to cost the same membership lookup three or more times on every
authenticated page. The memo lives on the request object, which Django rebuilds
per request - these tests pin both halves of that: it caches, and switching
workspace mid-request still wins.
"""

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory

from apps.accounts.models import Account, Membership
from apps.accounts.utils import get_current_account, set_current_account


def _request(user):
    request = RequestFactory().get("/dashboard/")
    request.user = user
    request.session = {}
    return request


def _account(user, name, role=Membership.Role.OWNER):
    acc = Account.objects.create(company_name=name, email_verified=True)
    Membership.objects.create(user=user, account=acc, role=role)
    return acc


@pytest.mark.django_db
def test_resolves_then_caches(django_assert_num_queries):
    user = User.objects.create_user("u", "u@example.com", "Sup3r-secret-pw")
    acc = _account(user, "Acme")
    request = _request(user)

    first = get_current_account(request)
    assert first == acc

    # Every later call in the same request is free.
    with django_assert_num_queries(0):
        assert get_current_account(request) == acc
        assert get_current_account(request) == acc


@pytest.mark.django_db
def test_switching_workspace_invalidates_the_memo():
    """The memo must never outlive a switch, or a user would keep seeing the
    workspace they just navigated away from."""
    user = User.objects.create_user("u", "u@example.com", "Sup3r-secret-pw")
    first = _account(user, "Acme")
    second = _account(user, "Globex", role=Membership.Role.MEMBER)

    request = _request(user)
    assert get_current_account(request) == first

    set_current_account(request, second)
    assert get_current_account(request) == second


@pytest.mark.django_db
def test_memo_is_per_request_not_shared():
    """Two requests must resolve independently - a shared cache here would be a
    cross-tenant leak."""
    a = User.objects.create_user("a", "a@example.com", "Sup3r-secret-pw")
    b = User.objects.create_user("b", "b@example.com", "Sup3r-secret-pw")
    acc_a = _account(a, "Acme")
    acc_b = _account(b, "Globex")

    assert get_current_account(_request(a)) == acc_a
    assert get_current_account(_request(b)) == acc_b


@pytest.mark.django_db
def test_anonymous_and_membershipless_users_get_none():
    from django.contrib.auth.models import AnonymousUser

    anon = _request(AnonymousUser())
    assert get_current_account(anon) is None

    lonely = User.objects.create_user("l", "l@example.com", "Sup3r-secret-pw")
    assert get_current_account(_request(lonely)) is None
