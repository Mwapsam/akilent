"""Publishing app tests — models, views, tasks, and security boundaries."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import (
    ContentCampaign,
    ContentCaption,
    ContentPillar,
    ContentPlanItem,
    ContentPost,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_account(slug="test-co"):
    return Account.objects.create(company_name="Test Co", slug=slug)


def make_user(username="tester"):
    return User.objects.create_user(username=username, password="password")  # noqa: S106


def make_post(account, **kwargs):
    defaults = dict(
        platforms=["instagram"],
        media_type="image",
        caption="Default caption",
        status=ContentPost.Status.DRAFT,
    )
    defaults.update(kwargs)
    return ContentPost.objects.create(account=account, **defaults)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class CaptionForTest(TestCase):
    def setUp(self):
        self.account = make_account()

    def test_returns_shared_caption_when_no_override(self):
        post = make_post(self.account, caption="Shared caption", platform_captions={})
        assert post.caption_for("instagram") == "Shared caption"
        assert post.caption_for("facebook") == "Shared caption"

    def test_returns_platform_override_when_set(self):
        post = make_post(
            self.account,
            caption="Shared",
            platform_captions={"instagram": "IG override"},
        )
        assert post.caption_for("instagram") == "IG override"
        assert post.caption_for("facebook") == "Shared"

    def test_empty_string_override_falls_back_to_shared(self):
        post = make_post(
            self.account,
            caption="Shared",
            platform_captions={"instagram": ""},
        )
        assert post.caption_for("instagram") == "Shared"

    def test_none_platform_captions_falls_back(self):
        post = make_post(self.account, caption="Shared")
        post.platform_captions = None
        assert post.caption_for("instagram") == "Shared"

    def test_unknown_platform_falls_back(self):
        post = make_post(
            self.account, caption="Shared", platform_captions={"facebook": "FB"}
        )
        assert post.caption_for("tiktok") == "Shared"


class ContentPlanItemCleanTest(TestCase):
    def setUp(self):
        self.account = make_account("a1")
        self.other_account = make_account("a2")
        self.pillar = ContentPillar.objects.create(account=self.account, name="P1")
        self.other_pillar = ContentPillar.objects.create(
            account=self.other_account, name="P2"
        )
        self.campaign = ContentCampaign.objects.create(account=self.account, name="C1")
        self.other_campaign = ContentCampaign.objects.create(
            account=self.other_account, name="C2"
        )

    def test_pillar_from_same_account_passes(self):
        item = ContentPlanItem(account=self.account, topic="Test", pillar=self.pillar)
        item.clean()  # no exception

    def test_pillar_from_other_account_raises(self):
        from django.core.exceptions import ValidationError

        item = ContentPlanItem(
            account=self.account, topic="Test", pillar=self.other_pillar
        )
        with self.assertRaises(ValidationError):  # noqa: PT027
            item.clean()

    def test_campaign_from_other_account_raises(self):
        from django.core.exceptions import ValidationError

        item = ContentPlanItem(
            account=self.account, topic="Test", campaign=self.other_campaign
        )
        with self.assertRaises(ValidationError):  # noqa: PT027
            item.clean()


class RefreshPlanStatusTest(TestCase):
    def setUp(self):
        self.account = make_account()
        self.item = ContentPlanItem.objects.create(
            account=self.account,
            topic="Test",
            plan_status=ContentPlanItem.PlanStatus.IN_PRODUCTION,
        )

    def test_all_published_sets_plan_status_published(self):
        make_post(
            self.account, plan_item=self.item, status=ContentPost.Status.PUBLISHED
        )
        make_post(
            self.account, plan_item=self.item, status=ContentPost.Status.PUBLISHED
        )
        self.item.refresh_plan_status()
        self.item.refresh_from_db()
        assert self.item.plan_status == ContentPlanItem.PlanStatus.PUBLISHED

    def test_any_draft_keeps_in_production(self):
        make_post(
            self.account, plan_item=self.item, status=ContentPost.Status.PUBLISHED
        )
        make_post(self.account, plan_item=self.item, status=ContentPost.Status.DRAFT)
        self.item.refresh_plan_status()
        self.item.refresh_from_db()
        assert self.item.plan_status == ContentPlanItem.PlanStatus.IN_PRODUCTION

    def test_no_posts_does_not_change_status(self):
        self.item.plan_status = ContentPlanItem.PlanStatus.PLANNED
        self.item.save()
        self.item.refresh_plan_status()
        self.item.refresh_from_db()
        assert self.item.plan_status == ContentPlanItem.PlanStatus.PLANNED

    def test_failed_post_does_not_trigger_published(self):
        make_post(self.account, plan_item=self.item, status=ContentPost.Status.FAILED)
        self.item.refresh_plan_status()
        self.item.refresh_from_db()
        assert self.item.plan_status == ContentPlanItem.PlanStatus.IN_PRODUCTION


class ContentCaptionNullablePostTest(TestCase):
    def test_caption_without_post_saves(self):
        make_account()
        caption = ContentCaption.objects.create(
            post=None, prompt="Write about summer", body="Great summer vibes!"
        )
        assert caption.pk is not None
        assert caption.post_id is None


# ---------------------------------------------------------------------------
# Task tests
# ---------------------------------------------------------------------------


class CancellationGuardTest(TestCase):
    def setUp(self):
        self.account = make_account()

    def test_cancelled_post_is_skipped_by_task(self):
        post = make_post(self.account, status=ContentPost.Status.CANCELLED)

        from apps.publishing.tasks import publish_post_async

        with patch("apps.publishing.services.publish_post") as mock_publish:
            result = publish_post_async(post.pk)

        assert result == "skipped"
        mock_publish.assert_not_called()

    def test_draft_post_is_skipped_by_task(self):
        post = make_post(self.account, status=ContentPost.Status.DRAFT)

        from apps.publishing.tasks import publish_post_async

        with patch("apps.publishing.services.publish_post") as mock_publish:
            result = publish_post_async(post.pk)

        assert result == "skipped"
        mock_publish.assert_not_called()

    def test_publishing_post_proceeds(self):
        post = make_post(self.account, status=ContentPost.Status.PUBLISHING)

        from apps.publishing.tasks import publish_post_async

        with patch("apps.publishing.services.publish_post") as mock_publish:
            result = publish_post_async(post.pk)

        assert result == "published"
        mock_publish.assert_called_once_with(post)

    def test_nonexistent_post_returns_not_found(self):
        from apps.publishing.tasks import publish_post_async

        with patch("apps.publishing.services.publish_post") as mock_publish:
            result = publish_post_async(99999999)

        assert result == "not_found"
        mock_publish.assert_not_called()

    def test_failed_post_marks_status_after_max_retries(self):
        post = make_post(self.account, status=ContentPost.Status.PUBLISHING)

        from apps.publishing.tasks import publish_post_async

        mock_task = MagicMock()
        mock_task.request.retries = 3
        mock_task.max_retries = 3

        with patch(
            "apps.publishing.services.publish_post",
            side_effect=RuntimeError("API down"),
        ):
            # Call the underlying task run() directly to avoid Celery's local proxy
            publish_post_async.run.__func__(mock_task, post.pk)

        post.refresh_from_db()
        assert post.status == ContentPost.Status.FAILED
        assert "API down" in post.error


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


class ViewTestBase(TestCase):
    def setUp(self):
        self.account = make_account()
        self.user = make_user()
        self.client.force_login(self.user)
        # Patch get_current_account to return our test account
        self.account_patcher = patch(
            "apps.publishing.views.get_current_account",
            return_value=self.account,
        )
        self.account_patcher.start()

    def tearDown(self):
        self.account_patcher.stop()


class PublishingIndexViewTest(ViewTestBase):
    def test_index_renders(self):
        response = self.client.get("/publishing/")
        assert response.status_code == 200

    def test_index_counts_only_this_months_published(self):
        now = timezone.now()
        this_month = now.replace(day=1)
        last_month = (this_month - datetime.timedelta(days=1)).replace(day=1)

        make_post(self.account, status=ContentPost.Status.PUBLISHED, published_at=now)
        make_post(
            self.account, status=ContentPost.Status.PUBLISHED, published_at=last_month
        )

        response = self.client.get("/publishing/")
        assert response.status_code == 200
        counts = response.context["counts"]
        assert counts["published_this_month"] == 1

    def test_index_requires_login(self):
        self.client.logout()
        response = self.client.get("/publishing/")
        assert response.status_code == 302


class LibraryViewTest(ViewTestBase):
    def test_library_renders(self):
        response = self.client.get("/publishing/library/")
        assert response.status_code == 200

    def test_library_search_filters_by_caption(self):
        make_post(
            self.account, caption="Summer sale launch", status=ContentPost.Status.DRAFT
        )
        make_post(
            self.account, caption="Winter collection", status=ContentPost.Status.DRAFT
        )

        response = self.client.get("/publishing/library/?q=summer")
        assert response.status_code == 200
        page = response.context["page_obj"]
        assert all("summer" in p.caption.lower() for p in page)

    def test_library_status_filter(self):
        make_post(self.account, status=ContentPost.Status.DRAFT)
        make_post(
            self.account,
            status=ContentPost.Status.PUBLISHED,
            published_at=timezone.now(),
        )

        response = self.client.get("/publishing/library/?status=draft")
        page = response.context["page_obj"]
        assert all(p.status == ContentPost.Status.DRAFT for p in page)

    def test_library_excludes_other_accounts(self):
        other = make_account("other-co")
        make_post(other, caption="Other account post")
        make_post(self.account, caption="My post")

        response = self.client.get("/publishing/library/")
        page = response.context["page_obj"]
        assert all(p.account_id == self.account.pk for p in page)


class BulkActionSecurityTest(ViewTestBase):
    def test_bulk_delete_rejects_cross_account_pks(self):
        other = make_account("other")
        other_post = make_post(other, status=ContentPost.Status.DRAFT)
        own_post = make_post(self.account, status=ContentPost.Status.DRAFT)

        response = self.client.post(
            "/publishing/library/bulk/",
            {"action": "delete_drafts", "post_ids": [other_post.pk, own_post.pk]},
        )
        # Should redirect with error, not delete anything
        assert response.status_code == 302
        assert ContentPost.objects.filter(pk=other_post.pk).exists()
        assert ContentPost.objects.filter(pk=own_post.pk).exists()

    def test_bulk_delete_drafts_removes_only_drafts(self):
        draft = make_post(self.account, status=ContentPost.Status.DRAFT)
        scheduled = make_post(
            self.account,
            status=ContentPost.Status.SCHEDULED,
            scheduled_at=timezone.now() + datetime.timedelta(hours=1),
        )

        self.client.post(
            "/publishing/library/bulk/",
            {"action": "delete_drafts", "post_ids": [draft.pk, scheduled.pk]},
        )
        assert not ContentPost.objects.filter(pk=draft.pk).exists()
        assert ContentPost.objects.filter(pk=scheduled.pk).exists()

    def test_bulk_cancel_scheduled_changes_status(self):
        post = make_post(
            self.account,
            status=ContentPost.Status.SCHEDULED,
            scheduled_at=timezone.now() + datetime.timedelta(hours=1),
        )
        self.client.post(
            "/publishing/library/bulk/",
            {"action": "cancel_scheduled", "post_ids": [post.pk]},
        )
        post.refresh_from_db()
        assert post.status == ContentPost.Status.CANCELLED


class PostStatusTransitionViewTest(ViewTestBase):
    def test_cancel_scheduled_post(self):
        post = make_post(
            self.account,
            status=ContentPost.Status.SCHEDULED,
            scheduled_at=timezone.now() + datetime.timedelta(hours=1),
        )
        response = self.client.post(f"/publishing/posts/{post.pk}/cancel/")
        assert response.status_code == 302
        post.refresh_from_db()
        assert post.status == ContentPost.Status.CANCELLED

    def test_cancel_non_scheduled_post_rejected(self):
        post = make_post(self.account, status=ContentPost.Status.DRAFT)
        self.client.post(f"/publishing/posts/{post.pk}/cancel/")
        post.refresh_from_db()
        assert post.status == ContentPost.Status.DRAFT

    def test_retry_failed_post(self):
        post = make_post(
            self.account,
            status=ContentPost.Status.FAILED,
            error="Connection timeout",
        )
        response = self.client.post(f"/publishing/posts/{post.pk}/retry/")
        assert response.status_code == 302
        post.refresh_from_db()
        assert post.status == ContentPost.Status.SCHEDULED
        assert post.error == ""
        assert post.scheduled_at is not None

    def test_retry_non_failed_post_rejected(self):
        post = make_post(self.account, status=ContentPost.Status.DRAFT)
        self.client.post(f"/publishing/posts/{post.pk}/retry/")
        post.refresh_from_db()
        assert post.status == ContentPost.Status.DRAFT

    def test_reopen_cancelled_post(self):
        post = make_post(
            self.account,
            status=ContentPost.Status.CANCELLED,
            scheduled_at=timezone.now() + datetime.timedelta(hours=1),
        )
        response = self.client.post(f"/publishing/posts/{post.pk}/reopen/")
        assert response.status_code == 302
        post.refresh_from_db()
        assert post.status == ContentPost.Status.DRAFT
        assert post.scheduled_at is None

    def test_reopen_non_cancelled_rejected(self):
        post = make_post(self.account, status=ContentPost.Status.DRAFT)
        self.client.post(f"/publishing/posts/{post.pk}/reopen/")
        post.refresh_from_db()
        assert post.status == ContentPost.Status.DRAFT

    def test_cannot_act_on_other_accounts_post(self):
        other = make_account("other-account")
        post = make_post(
            other,
            status=ContentPost.Status.SCHEDULED,
            scheduled_at=timezone.now() + datetime.timedelta(hours=1),
        )
        response = self.client.post(f"/publishing/posts/{post.pk}/cancel/")
        assert response.status_code == 404
        post.refresh_from_db()
        assert post.status == ContentPost.Status.SCHEDULED


class PlanItemCreatePostTest(ViewTestBase):
    def test_creates_linked_draft_with_correct_fields(self):
        item = ContentPlanItem.objects.create(
            account=self.account,
            topic="Summer product launch",
            platforms=["instagram", "facebook"],
            media_type="image",
        )
        response = self.client.post(f"/publishing/planning/{item.pk}/create-post/")
        assert response.status_code == 302

        post = ContentPost.objects.filter(account=self.account, plan_item=item).first()
        assert post is not None
        assert post.status == ContentPost.Status.DRAFT
        assert post.platforms == ["instagram", "facebook"]
        assert post.media_type == "image"

        item.refresh_from_db()
        assert item.plan_status == ContentPlanItem.PlanStatus.IN_PRODUCTION

    def test_plan_item_create_post_scoped_to_account(self):
        other = make_account("other-2")
        item = ContentPlanItem.objects.create(
            account=other, topic="Other account idea", platforms=["instagram"]
        )
        response = self.client.post(f"/publishing/planning/{item.pk}/create-post/")
        assert response.status_code == 404
        assert not ContentPost.objects.filter(plan_item=item).exists()


class PlanItemDeleteTest(ViewTestBase):
    def test_deletes_idea_status_item(self):
        item = ContentPlanItem.objects.create(
            account=self.account,
            topic="Old idea",
            plan_status=ContentPlanItem.PlanStatus.IDEA,
        )
        self.client.post(f"/publishing/planning/{item.pk}/delete/")
        assert not ContentPlanItem.objects.filter(pk=item.pk).exists()

    def test_rejects_in_production_delete(self):
        item = ContentPlanItem.objects.create(
            account=self.account,
            topic="Active item",
            plan_status=ContentPlanItem.PlanStatus.IN_PRODUCTION,
        )
        self.client.post(f"/publishing/planning/{item.pk}/delete/")
        assert ContentPlanItem.objects.filter(pk=item.pk).exists()


class PostNewViewTest(ViewTestBase):
    def test_save_draft_creates_post(self):
        response = self.client.post(
            "/publishing/new/",
            {
                "caption": "A new post",
                "platforms": ["instagram"],
                "media_type": "image",
                "hashtags": "",
                "media_urls": "",
                "scheduled_at": "",
                "action": "draft",
            },
        )
        assert response.status_code == 302
        post = ContentPost.objects.filter(
            account=self.account, caption="A new post"
        ).first()
        assert post is not None
        assert post.status == ContentPost.Status.DRAFT

    def test_platform_caption_overrides_saved(self):
        self.client.post(
            "/publishing/new/",
            {
                "caption": "Shared caption",
                "platforms": ["instagram", "facebook"],
                "media_type": "image",
                "caption_instagram": "IG specific",
                "hashtags": "",
                "media_urls": "",
                "scheduled_at": "",
                "action": "draft",
            },
        )
        post = (
            ContentPost.objects.filter(account=self.account)
            .order_by("-created_at")
            .first()
        )
        assert post is not None
        assert post.platform_captions.get("instagram") == "IG specific"
        assert "facebook" not in post.platform_captions

    def test_existing_draft_loaded_by_post_id_param(self):
        post = make_post(self.account, caption="Existing draft")
        response = self.client.get(f"/publishing/new/?post_id={post.pk}")
        assert response.status_code == 200
        assert response.context["existing_post"].pk == post.pk

    def test_post_id_param_from_other_account_ignored(self):
        other = make_account("other-3")
        other_post = make_post(other, caption="Other")
        response = self.client.get(f"/publishing/new/?post_id={other_post.pk}")
        assert response.context["existing_post"] is None

    def test_schedule_action_sets_scheduled_status(self):
        future = (timezone.now() + datetime.timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        self.client.post(
            "/publishing/new/",
            {
                "caption": "Scheduled post",
                "platforms": ["instagram"],
                "media_type": "image",
                "hashtags": "",
                "media_urls": "",
                "scheduled_at": future,
                "action": "schedule",
            },
        )
        post = ContentPost.objects.filter(
            account=self.account, caption="Scheduled post"
        ).first()
        assert post is not None
        assert post.status == ContentPost.Status.SCHEDULED
        assert post.scheduled_at is not None


class PostDetailViewTest(ViewTestBase):
    def test_detail_renders_own_post(self):
        post = make_post(self.account, caption="My post")
        response = self.client.get(f"/publishing/posts/{post.pk}/")
        assert response.status_code == 200

    def test_detail_404_for_other_account_post(self):
        other = make_account("other-4")
        post = make_post(other, caption="Not mine")
        response = self.client.get(f"/publishing/posts/{post.pk}/")
        assert response.status_code == 404


class PlanningOverviewViewTest(ViewTestBase):
    def test_planning_renders(self):
        response = self.client.get("/publishing/planning/")
        assert response.status_code == 200

    def test_planning_counts_by_status(self):
        ContentPlanItem.objects.create(
            account=self.account,
            topic="Idea 1",
            plan_status=ContentPlanItem.PlanStatus.IDEA,
        )
        ContentPlanItem.objects.create(
            account=self.account,
            topic="Idea 2",
            plan_status=ContentPlanItem.PlanStatus.PLANNED,
        )
        response = self.client.get("/publishing/planning/")
        counts = response.context["counts"]
        assert counts["idea"] == 1
        assert counts["planned"] == 1

    def test_planning_excludes_other_account_items(self):
        other = make_account("other-5")
        ContentPlanItem.objects.create(account=other, topic="Other idea")
        ContentPlanItem.objects.create(account=self.account, topic="My idea")
        response = self.client.get("/publishing/planning/")
        backlog = response.context["backlog"]
        assert all(i.account_id == self.account.pk for i in backlog)


class CalendarViewTest(ViewTestBase):
    def test_calendar_renders_default_week(self):
        response = self.client.get("/publishing/calendar/")
        assert response.status_code == 200
        assert "week_days" in response.context

    def test_calendar_renders_specific_week(self):
        response = self.client.get("/publishing/calendar/?week=2026-W41")
        assert response.status_code == 200


class ChannelsViewTest(ViewTestBase):
    def test_channels_renders(self):
        response = self.client.get("/publishing/channels/")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Month boundary test (Dec → Jan rollover)
# ---------------------------------------------------------------------------


class MonthBoundaryTest(TestCase):
    """published_at from the previous year must not count as this month."""

    def setUp(self):
        self.account = make_account()
        self.user = make_user("boundary-user")
        self.client.force_login(self.user)
        self.patcher = patch(
            "apps.publishing.views.get_current_account",
            return_value=self.account,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_only_current_month_posts_counted(self):
        now = timezone.now()
        last_year_same_month = now.replace(year=now.year - 1)

        make_post(self.account, status=ContentPost.Status.PUBLISHED, published_at=now)
        make_post(
            self.account,
            status=ContentPost.Status.PUBLISHED,
            published_at=last_year_same_month,
        )

        response = self.client.get("/publishing/")
        counts = response.context["counts"]
        assert counts["published_this_month"] == 1
