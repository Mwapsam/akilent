from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
    PublishingSettings,
)
from apps.publishing.tasks import publish_post_async

pytestmark = pytest.mark.django_db


@pytest.fixture
def team():
    account = Account.objects.create(company_name="Team Co")
    owner = User.objects.create_user("owner", "owner@example.com", "pass")
    member = User.objects.create_user("member", "member@example.com", "pass")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    # Publishing now needs a connected Page (the composer's readiness check).
    FacebookPageConnection.objects.create(
        account=account, page_id="p1", page_name="Team Co", access_token="t"
    )
    return account, owner, member


def _login(user):
    client = Client()
    client.force_login(user)
    return client


def _require_review(account):
    PublishingSettings.objects.create(account=account, requires_review=True)


def _compose(client, **extra):
    data = {
        "caption": "hello",
        "media_type": "text",
        "platforms": ["facebook"],
        "action": "publish_now",
        **extra,
    }
    return client.post(reverse("publishing:post-new"), data)


def test_review_is_off_by_default_so_members_publish_directly(team):
    account, _, member = team
    with patch.object(publish_post_async, "delay") as delay:
        _compose(_login(member))

    post = ContentPost.objects.get(account=account)
    assert post.status == ContentPost.Status.PUBLISHING
    delay.assert_called_once()


def test_publish_now_without_approval_is_saved_as_draft_when_review_is_on(team):
    account, _, member = team
    _require_review(account)

    with patch.object(publish_post_async, "delay") as delay:
        _compose(_login(member))

    post = ContentPost.objects.get(account=account)
    assert post.status == ContentPost.Status.DRAFT
    delay.assert_not_called()


def test_scheduling_without_approval_is_saved_as_draft_when_review_is_on(team):
    account, _, member = team
    _require_review(account)
    when = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

    _compose(_login(member), action="schedule", scheduled_at=when)

    assert ContentPost.objects.get(account=account).status == ContentPost.Status.DRAFT


def test_full_review_cycle_then_schedule(team):
    account, owner, member = team
    _require_review(account)
    member_client = _login(member)
    _compose(member_client, action="draft")
    post = ContentPost.objects.get(account=account)

    member_client.post(reverse("publishing:post-submit-for-review", args=[post.pk]))
    _login(owner).post(reverse("publishing:post-approve", args=[post.pk]))
    post.refresh_from_db()
    assert post.is_approval_valid()

    when = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    _compose(member_client, post_id=post.pk, action="schedule", scheduled_at=when)

    post.refresh_from_db()
    assert post.status == ContentPost.Status.SCHEDULED


def test_editing_approved_content_sends_the_post_back_for_review(team):
    account, owner, member = team
    _require_review(account)
    member_client = _login(member)
    _compose(member_client, action="draft")
    post = ContentPost.objects.get(account=account)
    member_client.post(reverse("publishing:post-submit-for-review", args=[post.pk]))
    _login(owner).post(reverse("publishing:post-approve", args=[post.pk]))

    _compose(member_client, post_id=post.pk, caption="changed after approval")

    post.refresh_from_db()
    assert post.status == ContentPost.Status.DRAFT
    assert post.approval_status == "draft"


def test_turning_review_on_also_stops_already_scheduled_posts(team):
    account, _, _ = team
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=timezone.now(),
    )
    ContentPostVariant.objects.create(post=post, platform="facebook")
    _require_review(account)

    with patch("apps.publishing.services.orchestrate_post_publication") as orchestrate:
        assert publish_post_async.run(post.pk) == "approval_required"

    orchestrate.assert_not_called()


def test_only_admins_can_change_the_review_setting(team):
    account, owner, member = team

    _login(member).post(reverse("publishing:settings"), {"requires_review": "on"})
    assert not PublishingSettings.requires_review_for(account)

    _login(owner).post(reverse("publishing:settings"), {"requires_review": "on"})
    assert PublishingSettings.requires_review_for(account)


def test_members_cannot_resolve_reconciliation(team):
    account, _, member = team
    post = ContentPost.objects.create(account=account, platforms=["facebook"])
    variant = ContentPostVariant.objects.create(
        post=post, platform="facebook", status=ContentPostVariant.Status.RECONCILING
    )

    _login(member).post(
        reverse("publishing:reconciliation-confirm", args=[variant.pk]),
        {"publication_id": "123", "evidence_notes": "seen it"},
    )

    variant.refresh_from_db()
    assert variant.status == ContentPostVariant.Status.RECONCILING


def test_members_cannot_disconnect_the_facebook_page(team):
    account, _, member = team
    conn = FacebookPageConnection.objects.get(account=account)

    _login(member).post(reverse("publishing:fb-disconnect"))

    conn.refresh_from_db()
    assert conn.status == FacebookPageConnection.Status.ACTIVE


def test_published_post_cannot_be_overwritten_from_the_composer(team):
    account, owner, _ = team
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        caption="live",
        status=ContentPost.Status.PUBLISHED,
    )

    _compose(_login(owner), post_id=post.pk, caption="rewritten")

    post.refresh_from_db()
    assert post.caption == "live"
    assert post.status == ContentPost.Status.PUBLISHED


def test_composer_save_keeps_a_brief_it_did_not_send(team):
    account, owner, _ = team
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], brief_objective="Sell more cakes"
    )

    _compose(_login(owner), post_id=post.pk, action="draft")

    post.refresh_from_db()
    assert post.brief_objective == "Sell more cakes"


def test_post_detail_shows_the_review_panel(team):
    account, owner, member = team
    _require_review(account)
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        created_by=member,
        approval_status="submitted",
    )

    owner_page = _login(owner).get(reverse("publishing:post-detail", args=[post.pk]))
    member_page = _login(member).get(reverse("publishing:post-detail", args=[post.pk]))

    assert reverse("publishing:post-approve", args=[post.pk]) in owner_page.text
    assert reverse("publishing:post-approve", args=[post.pk]) not in member_page.text
    assert "Waiting for an Owner or Admin" in member_page.text
