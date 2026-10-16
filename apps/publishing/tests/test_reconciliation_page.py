import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db


def test_reconciliation_queue_renders_and_lists_reconciling_variants():
    user = User.objects.create_user("ops", "ops@example.com", "pass")
    account = Account.objects.create(company_name="Ops Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    post = ContentPost.objects.create(account=account, platforms=["facebook"])
    ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=ContentPostVariant.Status.RECONCILING,
        last_error="Final publication timed out",
    )
    client = Client()
    client.force_login(user)

    response = client.get(reverse("publishing:reconciliation-queue"))

    assert response.status_code == 200
    assert "Final publication timed out" in response.content.decode()


def test_confirm_rejects_javascript_publication_url_and_never_renders_it():
    user = User.objects.create_user("ops2", "ops2@example.com", "pass")
    account = Account.objects.create(company_name="Ops Two")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    post = ContentPost.objects.create(account=account, platforms=["facebook"])
    variant = ContentPostVariant.objects.create(
        post=post, platform="facebook", status=ContentPostVariant.Status.RECONCILING
    )
    client = Client()
    client.force_login(user)

    client.post(
        reverse("publishing:reconciliation-confirm", args=[variant.pk]),
        {"publication_id": "fb_1", "publication_url": "javascript:alert(1)"},
    )

    variant.refresh_from_db()
    assert variant.status == ContentPostVariant.Status.RECONCILING
    assert variant.publication_url == ""
    page = client.get(reverse("publishing:reconciliation-queue")).content.decode()
    assert "javascript:" not in page
