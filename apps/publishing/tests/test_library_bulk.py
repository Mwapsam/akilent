import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Library Co")
    user = User.objects.create_user("libowner", "lib@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


def _post(account, status, caption="x"):
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], status=status, caption=caption
    )
    ContentPostVariant.objects.create(post=post, platform="facebook")
    return post


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def test_delete_drafts_removes_drafts_and_reports_the_post_count(owner):
    account, client = owner
    drafts = [_post(account, ContentPost.Status.DRAFT) for _ in range(2)]

    response = client.post(
        reverse("publishing:library-bulk"),
        {"action": "delete_drafts", "post_ids": [p.pk for p in drafts]},
    )

    assert response.status_code == 302
    assert not ContentPost.objects.filter(pk__in=[p.pk for p in drafts]).exists()
    assert "Deleted 2 draft(s)." in _messages(response)


def test_delete_drafts_reports_posts_that_were_not_deleted(owner):
    account, client = owner
    draft = _post(account, ContentPost.Status.DRAFT)
    scheduled = _post(account, ContentPost.Status.SCHEDULED)

    response = client.post(
        reverse("publishing:library-bulk"),
        {"action": "delete_drafts", "post_ids": [draft.pk, scheduled.pk]},
    )

    assert not ContentPost.objects.filter(pk=draft.pk).exists()
    assert ContentPost.objects.filter(pk=scheduled.pk).exists()
    messages = _messages(response)
    assert "Deleted 1 draft(s)." in messages
    assert any("not deleted" in m for m in messages)


def test_delete_drafts_with_only_scheduled_posts_says_nothing_was_deleted(owner):
    account, client = owner
    scheduled = _post(account, ContentPost.Status.SCHEDULED)

    response = client.post(
        reverse("publishing:library-bulk"),
        {"action": "delete_drafts", "post_ids": [scheduled.pk]},
    )

    assert ContentPost.objects.filter(pk=scheduled.pk).exists()
    messages = _messages(response)
    assert not any(m.startswith("Deleted") for m in messages)
    assert any("not deleted" in m for m in messages)


def test_library_page_script_is_marked_shell_safe(owner):
    """An unmarked inline script makes every boosted load fall back to a full reload."""
    _, client = owner

    response = client.get(reverse("publishing:library"))

    assert b"<script data-shell-safe>" in response.content


def test_cancel_scheduled_reports_posts_that_were_not_cancelled(owner):
    account, client = owner
    scheduled = _post(account, ContentPost.Status.SCHEDULED)
    draft = _post(account, ContentPost.Status.DRAFT)

    response = client.post(
        reverse("publishing:library-bulk"),
        {"action": "cancel_scheduled", "post_ids": [scheduled.pk, draft.pk]},
    )

    scheduled.refresh_from_db()
    assert scheduled.status == ContentPost.Status.CANCELLED
    messages = _messages(response)
    assert "Cancelled 1 scheduled post(s)." in messages
    assert any("not cancelled" in m for m in messages)
