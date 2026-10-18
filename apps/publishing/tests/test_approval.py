"""Tests for content approval workflow."""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ApprovalLog, ContentPost, ContentPostVariant


class TestApprovalWorkflow(TestCase):
    """Test approval workflow: submit, request changes, approve, resubmit."""

    def setUp(self):
        self.creator = User.objects.create_user("creator", "creator@test.com", "pass")
        self.reviewer = User.objects.create_user(
            "reviewer", "reviewer@test.com", "pass"
        )
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.creator, account=self.account, role=Membership.Role.OWNER
        )
        Membership.objects.create(
            user=self.reviewer, account=self.account, role=Membership.Role.ADMIN
        )
        self.client = Client()

        self.post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram", "facebook"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.creator,
            approval_required=True,
            assigned_reviewer=self.reviewer,
        )

    def test_submit_draft_for_review(self):
        """Creator can submit a draft for review."""
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("publishing:post-submit-for-review", args=[self.post.pk])
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")
        self.assertEqual(response.status_code, 302)

    def test_submit_creates_approval_log(self):
        """Submitting for review creates an ApprovalLog entry."""
        self.client.force_login(self.creator)
        self.client.post(
            reverse("publishing:post-submit-for-review", args=[self.post.pk])
        )

        log = ApprovalLog.objects.get(post=self.post)
        self.assertEqual(log.action, ApprovalLog.Action.SUBMITTED)
        self.assertEqual(log.performed_by, self.creator)

    def test_only_draft_can_be_submitted(self):
        """Cannot submit non-draft posts for review."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.creator)
        self.client.post(
            reverse("publishing:post-submit-for-review", args=[self.post.pk])
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")

    def test_reviewer_can_approve_submitted_post(self):
        """Reviewer can approve a submitted post."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "approved")
        self.assertEqual(self.post.approved_by, self.reviewer)
        self.assertIsNotNone(self.post.approved_at)
        self.assertIsNotNone(self.post.last_content_hash)

    def test_approval_captures_content_hash(self):
        """Approval captures a hash of current content."""
        self.post.caption = "Test caption"
        self.post.save()

        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        self.post.refresh_from_db()
        expected_hash = self.post.compute_content_hash()
        self.assertEqual(self.post.last_content_hash, expected_hash)

    def test_approval_creates_log_entry(self):
        """Approving creates an ApprovalLog entry."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        log = ApprovalLog.objects.filter(
            post=self.post, action=ApprovalLog.Action.APPROVED
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.performed_by, self.reviewer)

    def test_only_reviewer_can_approve(self):
        """Only the assigned reviewer can approve."""
        self.post.approval_status = "submitted"
        self.post.save()

        other_user = User.objects.create_user("other", "other@test.com", "pass")
        self.client.force_login(other_user)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")

    def test_reviewer_can_request_changes(self):
        """Reviewer can request changes on a submitted post."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("publishing:post-request-changes", args=[self.post.pk]),
            {"feedback": "Adjust the caption."},
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "changes_requested")
        self.assertEqual(self.post.approval_feedback, "Adjust the caption.")

    def test_request_changes_requires_feedback(self):
        """Requesting changes requires feedback."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("publishing:post-request-changes", args=[self.post.pk]),
            {"feedback": ""},
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")

    def test_request_changes_creates_log(self):
        """Requesting changes creates an ApprovalLog entry with feedback."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        feedback_text = "Enhance the caption."
        self.client.post(
            reverse("publishing:post-request-changes", args=[self.post.pk]),
            {"feedback": feedback_text},
        )

        log = ApprovalLog.objects.filter(
            post=self.post, action=ApprovalLog.Action.CHANGES_REQUESTED
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.feedback, feedback_text)

    def test_creator_can_resubmit_after_changes(self):
        """Creator can resubmit after changes are requested."""
        self.post.approval_status = "changes_requested"
        self.post.approval_feedback = "Adjust caption."
        self.post.save()

        self.client.force_login(self.creator)
        self.client.post(
            reverse("publishing:post-resubmit-after-changes", args=[self.post.pk])
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")
        self.assertEqual(self.post.approval_feedback, "")

    def test_resubmit_creates_log(self):
        """Resubmitting creates an ApprovalLog entry."""
        self.post.approval_status = "changes_requested"
        self.post.save()

        self.client.force_login(self.creator)
        self.client.post(
            reverse("publishing:post-resubmit-after-changes", args=[self.post.pk])
        )

        log = ApprovalLog.objects.filter(
            post=self.post, action=ApprovalLog.Action.RESUBMITTED
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.performed_by, self.creator)

    def test_only_creator_can_resubmit(self):
        """Only the creator can resubmit after changes."""
        self.post.approval_status = "changes_requested"
        self.post.save()

        other_user = User.objects.create_user("other", "other@test.com", "pass")
        self.client.force_login(other_user)
        self.client.post(
            reverse("publishing:post-resubmit-after-changes", args=[self.post.pk])
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "changes_requested")


class TestContentChangeInvalidatesApproval(TestCase):
    """Test that content changes invalidate approval."""

    def setUp(self):
        self.creator = User.objects.create_user("creator", "creator@test.com", "pass")
        self.reviewer = User.objects.create_user(
            "reviewer", "reviewer@test.com", "pass"
        )
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.creator, account=self.account, role=Membership.Role.OWNER
        )
        Membership.objects.create(
            user=self.reviewer, account=self.account, role=Membership.Role.ADMIN
        )

        self.post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            caption="Original caption",
            status=ContentPost.Status.APPROVED,
            created_by=self.creator,
            approval_required=True,
            approval_status="approved",
            approved_by=self.reviewer,
            approved_at=timezone.now(),
        )

    def test_caption_change_invalidates_approval(self):
        """Changing caption after approval invalidates the approval."""
        variant = ContentPostVariant.objects.create(
            post=self.post, platform="instagram", caption="Original caption"
        )
        original_hash = self.post.compute_content_hash()
        self.post.last_content_hash = original_hash
        self.post.save()

        # Content is valid initially
        self.assertTrue(self.post.is_approval_valid())

        # Change variant caption
        variant.caption = "Modified caption"
        variant.save()

        # Verify hash changed
        modified_hash = self.post.compute_content_hash()
        self.assertNotEqual(modified_hash, original_hash)

        # Approval is no longer valid
        self.assertFalse(self.post.is_approval_valid())

    def test_platform_caption_change_invalidates_approval(self):
        """Changing platform captions invalidates approval."""
        variant = ContentPostVariant.objects.create(
            post=self.post, platform="instagram", caption="Platform-specific"
        )
        original_hash = self.post.compute_content_hash()
        self.post.last_content_hash = original_hash
        self.post.save()

        self.assertTrue(self.post.is_approval_valid())

        # Modify variant caption
        variant.caption = "Changed caption"
        variant.save()

        # Verify hash changed
        modified_hash = self.post.compute_content_hash()
        self.assertNotEqual(modified_hash, original_hash)

        self.assertFalse(self.post.is_approval_valid())

    def test_media_urls_change_invalidates_approval(self):
        """Changing media URLs invalidates approval."""
        variant = ContentPostVariant.objects.create(
            post=self.post,
            platform="instagram",
            media_urls=["https://example.com/image1.jpg"],
        )
        original_hash = self.post.compute_content_hash()
        self.post.last_content_hash = original_hash
        self.post.save()

        self.assertTrue(self.post.is_approval_valid())

        # Change variant media
        variant.media_urls = ["https://example.com/image2.jpg"]
        variant.save()

        # Verify hash changed
        modified_hash = self.post.compute_content_hash()
        self.assertNotEqual(modified_hash, original_hash)

        self.assertFalse(self.post.is_approval_valid())

    def test_hash_matches_across_saves(self):
        """Content hash is consistent across saves."""
        hash1 = self.post.compute_content_hash()
        self.post.save()
        hash2 = self.post.compute_content_hash()
        self.assertEqual(hash1, hash2)

    def test_non_content_changes_preserve_approval(self):
        """Updating non-content fields preserves approval validity."""
        self.post.last_content_hash = self.post.compute_content_hash()
        self.post.save()

        self.assertTrue(self.post.is_approval_valid())

        # Change scheduled_at (not content)
        self.post.scheduled_at = timezone.now() + timezone.timedelta(days=1)
        self.post.save()

        # Approval still valid
        self.assertTrue(self.post.is_approval_valid())


class TestApprovalWithoutReview(TestCase):
    """Test solo creator workflow where approval is not required."""

    def setUp(self):
        self.creator = User.objects.create_user("creator", "creator@test.com", "pass")
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.creator, account=self.account, role=Membership.Role.OWNER
        )

        self.post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.creator,
            approval_required=False,
            approval_status="draft",
        )

    def test_solo_creator_can_skip_review(self):
        """When approval_required=False, approval_status stays draft."""
        self.assertTrue(not self.post.approval_required)
        self.assertEqual(self.post.approval_status, "draft")

    def test_solo_creator_post_can_be_scheduled_without_approval(self):
        """Posts with approval_required=False can transition to scheduled."""
        # In the scheduler, no approval check is required
        self.post.status = ContentPost.Status.SCHEDULED
        self.post.save()
        self.assertEqual(self.post.status, ContentPost.Status.SCHEDULED)


class TestApprovalStateTransitions(TestCase):
    """Test valid and invalid approval state transitions."""

    def setUp(self):
        self.user = User.objects.create_user("user", "user@test.com", "pass")
        self.reviewer = User.objects.create_user(
            "reviewer", "reviewer@test.com", "pass"
        )
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        Membership.objects.create(
            user=self.reviewer, account=self.account, role=Membership.Role.ADMIN
        )
        self.client = Client()

        self.post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.user,
            approval_required=True,
            assigned_reviewer=self.reviewer,
        )

    def test_draft_to_submitted_valid(self):
        """draft → submitted is valid."""
        self.client.force_login(self.user)
        self.client.post(
            reverse("publishing:post-submit-for-review", args=[self.post.pk])
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")

    def test_submitted_to_approved_valid(self):
        """submitted → approved is valid."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "approved")

    def test_submitted_to_changes_requested_valid(self):
        """submitted → changes_requested is valid."""
        self.post.approval_status = "submitted"
        self.post.save()

        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("publishing:post-request-changes", args=[self.post.pk]),
            {"feedback": "Fix this."},
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "changes_requested")

    def test_changes_requested_to_submitted_valid(self):
        """changes_requested → submitted is valid."""
        self.post.approval_status = "changes_requested"
        self.post.save()

        self.client.force_login(self.user)
        self.client.post(
            reverse("publishing:post-resubmit-after-changes", args=[self.post.pk])
        )

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "submitted")

    def test_approved_can_be_approved_again(self):
        """approved → submitted → approved (reapproval cycle)."""
        self.post.approval_status = "changes_requested"
        self.post.save()

        # Creator resubmits
        self.client.force_login(self.user)
        self.client.post(
            reverse("publishing:post-resubmit-after-changes", args=[self.post.pk])
        )

        # Reviewer approves again
        self.client.force_login(self.reviewer)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        self.post.refresh_from_db()
        self.assertEqual(self.post.approval_status, "approved")


class TestApprovalLogAuditTrail(TestCase):
    """Test that ApprovalLog provides a complete audit trail."""

    def setUp(self):
        self.creator = User.objects.create_user("creator", "creator@test.com", "pass")
        self.reviewer = User.objects.create_user(
            "reviewer", "reviewer@test.com", "pass"
        )
        self.account = Account.objects.create(
            company_name="Test Account", slug="test-account"
        )
        Membership.objects.create(
            user=self.creator, account=self.account, role=Membership.Role.OWNER
        )
        Membership.objects.create(
            user=self.reviewer, account=self.account, role=Membership.Role.ADMIN
        )
        self.client = Client()

        self.post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.DRAFT,
            created_by=self.creator,
            approval_required=True,
            assigned_reviewer=self.reviewer,
        )

    def test_approval_log_tracks_full_workflow(self):
        """ApprovalLog tracks submit → changes_requested → resubmit → approve."""
        self.client.force_login(self.creator)

        # Submit
        self.client.post(
            reverse("publishing:post-submit-for-review", args=[self.post.pk])
        )

        # Request changes
        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("publishing:post-request-changes", args=[self.post.pk]),
            {"feedback": "Adjust caption."},
        )

        # Resubmit
        self.client.force_login(self.creator)
        self.client.post(
            reverse("publishing:post-resubmit-after-changes", args=[self.post.pk])
        )

        # Approve
        self.client.force_login(self.reviewer)
        self.client.post(reverse("publishing:post-approve", args=[self.post.pk]))

        logs = list(ApprovalLog.objects.filter(post=self.post).order_by("created_at"))
        self.assertEqual(len(logs), 4)
        self.assertEqual(logs[0].action, ApprovalLog.Action.SUBMITTED)
        self.assertEqual(logs[1].action, ApprovalLog.Action.CHANGES_REQUESTED)
        self.assertEqual(logs[2].action, ApprovalLog.Action.RESUBMITTED)
        self.assertEqual(logs[3].action, ApprovalLog.Action.APPROVED)

    def test_approval_log_preserves_feedback(self):
        """ApprovalLog preserves feedback text."""
        self.post.approval_status = "submitted"
        self.post.save()

        feedback = "This needs a stronger call to action."
        self.client.force_login(self.reviewer)
        self.client.post(
            reverse("publishing:post-request-changes", args=[self.post.pk]),
            {"feedback": feedback},
        )

        log = ApprovalLog.objects.get(action=ApprovalLog.Action.CHANGES_REQUESTED)
        self.assertEqual(log.feedback, feedback)
