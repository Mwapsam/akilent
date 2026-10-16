import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPlanItem, ContentPost

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_for_owner():
    user = User.objects.create_user("maker", "maker@example.com", "pass")
    account = Account.objects.create(company_name="Maker Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client, account


def test_publish_now_with_no_platform_leaves_no_active_post(client_for_owner):
    client, account = client_for_owner

    client.post(
        reverse("publishing:post-new"),
        {"caption": "hello", "media_type": "image", "action": "publish_now"},
    )

    assert not ContentPost.objects.filter(
        account=account, status=ContentPost.Status.PUBLISHING
    ).exists()
    assert ContentPost.objects.filter(account=account).count() == 0


def test_plan_item_with_no_platform_redirects_instead_of_500(client_for_owner):
    client, account = client_for_owner
    item = ContentPlanItem.objects.create(
        account=account, topic="t", platforms=[], media_type="image"
    )

    response = client.post(reverse("publishing:plan-create-post", args=[item.pk]))

    assert response.status_code == 302
    assert ContentPost.objects.filter(account=account).count() == 0
