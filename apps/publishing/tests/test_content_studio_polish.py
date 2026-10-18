"""P0 Content Studio polish: filters survive navigation, the AI brief's goal reaches the
draft, composer actions follow the review setting, one status badge, media as media."""

import re
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.ai.models import AIDraft
from apps.publishing.models import (
    ApprovalLog,
    ContentPost,
    ContentPostVariant,
    PublishingSettings,
)
from apps.publishing.templatetags.publishing_filters import (
    media_kind,
    status_badge,
    variant_status_badge,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Polish Co")
    user = User.objects.create_user("polish", "polish@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, user, client


def _hrefs(html):
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]*)"', html)]


# --- 1. Filters survive navigation -------------------------------------------------


def test_library_pagination_keeps_every_filter(owner):
    account, _, client = owner
    for i in range(30):
        ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            media_type="image",
            caption=f"post {i}",
        )

    html = client.get(
        reverse("publishing:library"),
        {
            "platform": "facebook",
            "media_type": "image",
            "date_from": "2020-01-01",
            "date_to": "2099-01-01",
        },
    ).content.decode()

    next_links = [h for h in _hrefs(html) if "page=2" in h]
    assert next_links, "expected a next-page link"
    for param in (
        "platform=facebook",
        "media_type=image",
        "date_from=2020-01-01",
        "date_to=2099-01-01",
    ):
        assert param in next_links[0]


def test_calendar_week_navigation_keeps_channel_and_status(owner):
    _, _, client = owner

    html = client.get(
        reverse("publishing:calendar"),
        {"week": "2026-W42", "channel": "facebook", "status": "scheduled"},
    ).content.decode()

    for week in ("2026-W41", "2026-W43"):
        links = [h for h in _hrefs(html) if f"week={week}" in h]
        assert links, f"expected a link to {week}"
        assert "channel=facebook" in links[0]
        assert "status=scheduled" in links[0]


# --- 2. The AI brief's goal reaches the draft ---------------------------------------


def _generate(client, **extra):
    data = {"prompt": "Summer launch", "platform": "facebook", "media_type": "image"}
    data.update(extra)
    with patch("apps.ai.tasks.build_draft.delay"):
        return client.post(reverse("publishing:caption-generate"), data)


def test_caption_goal_is_stored_on_the_draft(owner):
    account, _, client = owner

    assert _generate(client, goal="sales").status_code == 200

    draft = AIDraft.objects.get(account=account)
    assert draft.context["goal"] == "Drive sales"
    assert draft.context["platform"] == "facebook"


def test_unknown_caption_goal_is_dropped(owner):
    account, _, client = owner

    _generate(client, goal="<script>")

    assert "goal" not in AIDraft.objects.get(account=account).context


# --- 3. Composer actions follow the review setting ----------------------------------


def test_composer_offers_submit_for_review_when_review_is_on(owner):
    account, _, client = owner
    PublishingSettings.objects.create(account=account, requires_review=True)

    html = client.get(reverse("publishing:post-new")).content.decode()

    assert 'value="submit_review"' in html
    assert 'value="publish_now"' not in html
    assert 'value="schedule"' not in html


def test_composer_offers_schedule_and_publish_when_review_is_off(owner):
    _, _, client = owner

    html = client.get(reverse("publishing:post-new")).content.decode()

    assert 'value="schedule"' in html
    assert 'value="publish_now"' in html
    assert 'value="submit_review"' not in html


def test_save_and_submit_for_review_submits_the_draft(owner):
    account, user, client = owner
    PublishingSettings.objects.create(account=account, requires_review=True)

    client.post(
        reverse("publishing:post-new"),
        {
            "caption": "hello",
            "media_type": "text",
            "platforms": ["facebook"],
            "action": "submit_review",
        },
    )

    post = ContentPost.objects.get(account=account)
    assert post.status == ContentPost.Status.DRAFT
    assert post.approval_status == "submitted"
    assert ApprovalLog.objects.filter(
        post=post, action=ApprovalLog.Action.SUBMITTED, performed_by=user
    ).exists()


def test_calendar_plus_link_prefills_the_schedule(owner):
    _, _, client = owner

    html = client.get(
        reverse("publishing:post-new"), {"scheduled_at": "2026-11-02T09:00"}
    ).content.decode()

    assert "2026-11-02T09:00" in html


# --- 4. One status badge -----------------------------------------------------------


@pytest.mark.parametrize("status", [s.value for s in ContentPost.Status])
def test_every_post_status_has_a_tone(status):
    html = status_badge(status, "Label")

    assert re.search(r"tone-(neutral|amber|teal|info|coral)", html)
    assert "Label" in html


def test_reconciling_variant_reads_needs_check():
    variant = ContentPostVariant(
        platform="facebook", status=ContentPostVariant.Status.RECONCILING
    )

    html = variant_status_badge(variant)

    assert "Needs check" in html
    assert "tone-amber" in html


# --- 5. Media renders as media ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://cdn.example.com/a.jpg", "image"),
        ("https://cdn.example.com/a.JPEG?sig=1", "image"),
        ("https://cdn.example.com/a.png", "image"),
        ("https://cdn.example.com/a.webp", "image"),
        ("https://cdn.example.com/a.gif", "image"),
        ("https://cdn.example.com/a.mp4", "video"),
        ("https://cdn.example.com/a.mov?x=1", "video"),
        ("https://cdn.example.com/a.pdf", "file"),
        ("", "file"),
    ],
)
def test_media_kind(url, kind):
    assert media_kind(url) == kind


def test_post_detail_renders_media_not_raw_url_links(owner):
    account, _, client = owner
    image = "https://cdn.example.com/media/photo.jpg"
    video = "https://cdn.example.com/media/clip.mp4"
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="carousel",
        media_urls=[image, video],
    )

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    assert f'<img src="{image}"' in html
    assert f'<video src="{video}' in html
    assert f">{image}</a>" not in html


def test_library_shows_a_thumbnail(owner):
    account, _, client = owner
    image = "https://cdn.example.com/media/thumb.png"
    ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image", media_urls=[image]
    )

    html = client.get(reverse("publishing:library")).content.decode()

    assert f'<img src="{image}"' in html


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("publishing:index", {}),
        ("publishing:planning", {}),
        ("publishing:calendar", {}),
        ("publishing:calendar", {"view": "list"}),
        ("publishing:library", {}),
        ("publishing:channels", {}),
        ("publishing:reconciliation-queue", {}),
    ],
)
def test_content_studio_pages_render_with_media_and_every_status(owner, name, params):
    account, _, client = owner
    from django.utils import timezone

    for i, status in enumerate(ContentPost.Status):
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=status,
            caption=f"{status} post",
            media_urls=[f"https://cdn.example.com/{i}.jpg"] if i % 2 else [],
            scheduled_at=timezone.now(),
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.RECONCILING
            if i == 0
            else ContentPostVariant.Status.DRAFT,
        )

    assert client.get(reverse(name), params).status_code == 200


# --- 6. Reconciliation is reachable and readable ----------------------------------


def test_subnav_links_to_needs_check_only_when_something_needs_checking(owner):
    account, _, client = owner
    queue = reverse("publishing:reconciliation-queue")

    assert queue not in client.get(reverse("publishing:index")).content.decode()

    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], caption="Launch day"
    )
    ContentPostVariant.objects.create(
        post=post, platform="facebook", status=ContentPostVariant.Status.RECONCILING
    )

    html = client.get(reverse("publishing:index")).content.decode()
    assert queue in html
    page = client.get(queue).content.decode()
    assert "Launch day" in page
    assert post.public_id not in page
