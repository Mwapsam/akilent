"""Tests for brief context inheritance and preservation."""

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Account
from apps.publishing.models import (
    ContentCampaign,
    ContentPillar,
    ContentPlanItem,
    ContentPost,
)


class TestBriefContextInheritance(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", "test@example.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )

    def test_plan_item_create_post_copies_objective(self):
        """Brief objective is copied from plan item to post."""
        pillar = ContentPillar.objects.create(account=self.account, name="Test Pillar")
        campaign = ContentCampaign.objects.create(
            account=self.account, name="Test Campaign"
        )
        item = ContentPlanItem.objects.create(
            account=self.account,
            topic="Test Topic",
            objective="Build brand awareness among Gen Z",
            platforms=["instagram", "facebook"],
            media_type="image",
            pillar=pillar,
            campaign=campaign,
        )

        post = ContentPost.objects.create(
            account=self.account,
            plan_item=item,
            platforms=item.platforms,
            media_type=item.media_type,
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
            brief_objective=item.objective,
        )

        self.assertEqual(post.brief_objective, "Build brand awareness among Gen Z")
        self.assertEqual(post.plan_item, item)

    def test_plan_item_without_objective_creates_post_with_empty_brief(self):
        """Plan items without objective create posts with blank brief."""
        item = ContentPlanItem.objects.create(
            account=self.account,
            topic="Test Topic",
            platforms=["instagram"],
            media_type="image",
        )

        post = ContentPost.objects.create(
            account=self.account,
            plan_item=item,
            platforms=item.platforms,
            media_type=item.media_type,
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
            brief_objective=item.objective,
        )

        self.assertEqual(post.brief_objective, "")

    def test_brief_fields_are_optional_on_post(self):
        """Posts can be created without brief context."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        self.assertEqual(post.brief_objective, "")
        self.assertEqual(post.brief_audience, "")
        self.assertEqual(post.brief_problem, "")
        self.assertEqual(post.brief_offer, "")
        self.assertEqual(post.brief_message, "")
        self.assertEqual(post.brief_cta, "")

    def test_all_brief_fields_storable(self):
        """All six brief fields can be set and retrieved."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
            brief_objective="Increase sales by 20%",
            brief_audience="Women aged 25-35 in urban areas",
            brief_problem="Customers don't know about our new product",
            brief_offer="Free trial for first 100 signups",
            brief_message="Join thousands of satisfied users",
            brief_cta="Sign up now",
        )

        post.refresh_from_db()
        self.assertEqual(post.brief_objective, "Increase sales by 20%")
        self.assertEqual(post.brief_audience, "Women aged 25-35 in urban areas")
        self.assertEqual(
            post.brief_problem, "Customers don't know about our new product"
        )
        self.assertEqual(post.brief_offer, "Free trial for first 100 signups")
        self.assertEqual(post.brief_message, "Join thousands of satisfied users")
        self.assertEqual(post.brief_cta, "Sign up now")

    def test_existing_posts_without_brief_can_be_edited(self):
        """Editing an existing post without brief doesn't error."""
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
        )

        post.caption = "Updated caption"
        post.save()

        post.refresh_from_db()
        self.assertEqual(post.caption, "Updated caption")
        self.assertEqual(post.brief_objective, "")
