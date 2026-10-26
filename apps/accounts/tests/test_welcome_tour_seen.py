"""Gap G3: the welcome tour, once finished or closed, stays closed for that person in that
workspace on every browser and device (it used to live only in localStorage)."""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.accounts.models import Account, Membership

pytestmark = pytest.mark.django_db
URL = "/onboarding/tour-seen/"


@pytest.fixture
def member():
    user = User.objects.create_user("tour", "tour@example.com", "pass")
    account = Account.objects.create(company_name="Tour Co")
    other = Account.objects.create(company_name="Other Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=user, account=other, role=Membership.Role.MEMBER)
    client = Client()
    client.force_login(user)
    return client, user, account, other


def test_closing_the_tour_is_remembered_for_this_workspace(member):
    client, user, account, other = member
    # A new workspace still has setup to do, so the tour renders.
    assert "seen: false" in client.get("/dashboard/").content.decode()

    assert client.post(URL).status_code == 204
    here = Membership.objects.get(user=user, account=account)
    assert here.welcome_tour_seen_at is not None
    assert "seen: true" in client.get("/dashboard/").content.decode()
    # Another workspace's tour (different channels, different setup) is its own.
    assert Membership.objects.get(user=user, account=other).welcome_tour_seen_at is None


def test_the_first_time_is_kept(member):
    client, user, account, _ = member
    client.post(URL)
    first = Membership.objects.get(user=user, account=account).welcome_tour_seen_at
    client.post(URL)
    assert (
        Membership.objects.get(user=user, account=account).welcome_tour_seen_at == first
    )


def test_only_a_signed_in_post_counts():
    assert Client().post(URL).status_code == 302  # to sign in
