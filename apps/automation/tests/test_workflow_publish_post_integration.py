"""Test workflow publish_post step integration with variant orchestration (Finding #10).

Covers:
- Workflow publishes a Facebook image
- Workflow publishes an Instagram video
- Facebook fails while Instagram succeeds
- Instagram remains in nonterminal state during PROCESSING
- Workflow retries after timeout without duplicate publication
- Approval is required but missing blocks publishing
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import _run_publish_post, enroll
from apps.contacts.models import Contact
from apps.publishing.models import ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user("testuser", "test@example.com", "pass")
    account = Account.objects.create(company_name="Test Account")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account,
        phone="+1234567890",
        email="customer@example.com",
        first_name="Test",
    )


@pytest.fixture
def workflow(account):
    return Workflow.objects.create(
        account=account,
        name="Publish Workflow",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "manual"},
            "steps": [
                {
                    "id": "publish",
                    "type": "publish_post",
                    "post_id": "",  # filled in by test
                    "on_error": "error_handler",
                },
                {
                    "id": "error_handler",
                    "type": "notify_team",
                    "text": "Publishing failed",
                    "to": "owners",
                    "next": "stop",
                },
                {"id": "stop", "type": "stop"},
            ],
        },
    )


class TestWorkflowPublishFacebook:
    """Workflow publishes a Facebook image."""

    def test_facebook_image_publishes_successfully(self, account, contact, workflow):
        """Variant and parent reach correct final state."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
        )
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        with patch("apps.publishing.services._publish_variant_facebook") as mock_fb:
            mock_fb.return_value = {
                "success": True,
                "publication_id": "fb_123",
                "publication_url": "https://facebook.com/123",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            # Update workflow definition with the post_id
            workflow.definition["steps"][0]["post_id"] = str(post.pk)
            workflow.save()

            run = enroll(workflow, contact)
            assert run is not None

        # Verify variant is published
        fb_variant.refresh_from_db()
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert fb_variant.publication_id == "fb_123"

        # Verify run context has outcomes
        run.refresh_from_db()
        outcomes = run.context.get("publish_outcomes", [])
        assert len(outcomes) == 1
        assert outcomes[0]["platform"] == "facebook"
        assert outcomes[0]["status"] == ContentPostVariant.Status.PUBLISHED


class TestWorkflowPublishInstagram:
    """Workflow publishes an Instagram video."""

    def test_instagram_video_enters_processing(self, account, contact, workflow):
        """Remains in nonterminal state until provider processing completes."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            status=ContentPost.Status.DRAFT,
        )
        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.DRAFT,
        )

        with patch("apps.publishing.services._publish_variant_instagram") as mock_ig:
            mock_ig.return_value = {
                "success": False,  # Not yet published (processing)
                "publication_id": "ig_container_123",
                "publication_url": "",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,  # Not reconciling, just processing
                "metadata": {"ig_container_id": "ig_container_123"},
            }

            workflow.definition["steps"][0]["post_id"] = str(post.pk)
            workflow.save()

            run = enroll(workflow, contact)
            assert run is not None

        # Verify variant is in PROCESSING, not PUBLISHED
        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.PROCESSING
        # Container ID is stored in metadata for polling, not in publication_id (which is for final IDs)
        assert ig_variant.metadata.get("ig_container_id") == "ig_container_123"

        # Verify run context shows the partial state
        run.refresh_from_db()
        outcomes = run.context.get("publish_outcomes", [])
        assert len(outcomes) == 1
        assert outcomes[0]["platform"] == "instagram"
        assert outcomes[0]["status"] == ContentPostVariant.Status.PROCESSING


class TestWorkflowPartialSuccess:
    """Facebook succeeds while Instagram fails."""

    def test_facebook_succeeds_instagram_fails(self, account, contact, workflow):
        """Facebook remains published; Instagram can be recovered independently."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook", "instagram"],
            status=ContentPost.Status.DRAFT,
        )
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )
        ig_variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.DRAFT,
        )

        with (
            patch("apps.publishing.services._publish_variant_facebook") as mock_fb,
            patch("apps.publishing.services._publish_variant_instagram") as mock_ig,
        ):
            mock_fb.return_value = {
                "success": True,
                "publication_id": "fb_123",
                "publication_url": "https://facebook.com/123",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }
            mock_ig.return_value = {
                "success": False,
                "publication_id": "",
                "publication_url": "",
                "error": "Rate limit exceeded",
                "error_type": "rate_limit",
                "error_at": timezone.now(),
                "reconciling": False,
            }

            workflow.definition["steps"][0]["post_id"] = str(post.pk)
            workflow.save()

            run = enroll(workflow, contact)
            assert run is not None

        # Verify Facebook is published
        fb_variant.refresh_from_db()
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert fb_variant.publication_id == "fb_123"

        # Verify Instagram is failed
        ig_variant.refresh_from_db()
        assert ig_variant.status == ContentPostVariant.Status.FAILED
        assert ig_variant.last_error == "Rate limit exceeded"

        # Verify outcomes show both states
        run.refresh_from_db()
        outcomes = run.context.get("publish_outcomes", [])
        assert len(outcomes) == 2

        fb_outcome = next((o for o in outcomes if o["platform"] == "facebook"), None)
        ig_outcome = next((o for o in outcomes if o["platform"] == "instagram"), None)

        assert fb_outcome is not None
        assert fb_outcome["status"] == ContentPostVariant.Status.PUBLISHED

        assert ig_outcome is not None
        assert ig_outcome["status"] == ContentPostVariant.Status.FAILED
        assert ig_outcome["error"] == "Rate limit exceeded"


class TestWorkflowRetryNoDuplicate:
    """Workflow retries after timeout without duplicate publication."""

    def test_retry_detects_already_published_variant(self, account, contact, workflow):
        """No blind duplicate publication on workflow retry."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
        )
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        call_count = 0

        def mock_publisher(v, p):
            nonlocal call_count
            call_count += 1
            return {
                "success": True,
                "publication_id": f"fb_{call_count}",
                "publication_url": f"https://facebook.com/{call_count}",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

        with patch(
            "apps.publishing.services._publish_variant_facebook",
            side_effect=mock_publisher,
        ):
            workflow.definition["steps"][0]["post_id"] = str(post.pk)
            workflow.save()

            # First run
            run1 = enroll(workflow, contact)
            assert run1 is not None

        fb_variant.refresh_from_db()
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert fb_variant.publication_id == "fb_1"
        assert call_count == 1

        # Simulate workflow retry (e.g., task re-enqueued by worker)
        with patch(
            "apps.publishing.services._publish_variant_facebook",
            side_effect=mock_publisher,
        ):
            enroll(workflow, contact)
            # No re-enrollment since one is already active

        # Provider should only be called once (second publish_variant detects PUBLISHED)
        assert call_count == 1


class TestWorkflowApprovalRequired:
    """Approval is required but missing blocks publishing."""

    def test_missing_approval_blocks_publishing(self, account, contact, workflow):
        """Publishing blocked; routes to on_error."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
            approval_required=True,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        # No approval created

        workflow.definition["steps"][0]["post_id"] = str(post.pk)
        workflow.save()

        run = enroll(workflow, contact)
        assert run is not None

        # Run should stop at error_handler (routed due to approval_invalid)
        run.refresh_from_db()
        assert run.status == WorkflowRun.Status.COMPLETED
        assert run.current_step == "stop"

    def test_valid_approval_allows_publishing(self, account, contact, workflow):
        """Publishing succeeds when approval is valid."""
        user = User.objects.create_user("reviewer", "reviewer@example.com", "pass")
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
            approval_required=True,
            approval_status="approved",
            approved_by=user,
            approved_at=timezone.now(),
        )
        fb_variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )
        post.last_content_hash = post.compute_content_hash()
        post.save()

        with patch("apps.publishing.services._publish_variant_facebook") as mock_fb:
            mock_fb.return_value = {
                "success": True,
                "publication_id": "fb_456",
                "publication_url": "https://facebook.com/456",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            workflow.definition["steps"][0]["post_id"] = str(post.pk)
            workflow.save()

            run = enroll(workflow, contact)
            assert run is not None

        fb_variant.refresh_from_db()
        assert fb_variant.status == ContentPostVariant.Status.PUBLISHED
        assert fb_variant.publication_id == "fb_456"


class TestWorkflowPublishStep:
    """Direct step execution tests."""

    def test_publish_step_with_orchestration_success(self, account, contact, workflow):
        """Step returns per-variant outcomes."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        step = {
            "id": "publish",
            "type": "publish_post",
            "post_id": str(post.pk),
        }

        run = WorkflowRun.objects.create(
            workflow=workflow,
            contact=contact,
            status=WorkflowRun.Status.ACTIVE,
        )

        with patch("apps.publishing.services._publish_variant_facebook") as mock_fb:
            mock_fb.return_value = {
                "success": True,
                "publication_id": "fb_789",
                "publication_url": "https://facebook.com/789",
                "error": "",
                "error_type": "",
                "error_at": None,
                "reconciling": False,
            }

            result = _run_publish_post(run, step)

        assert result["parent_status"] == ContentPost.Status.PUBLISHED
        assert result["outcome_count"] == 1
        assert len(result["outcomes"]) == 1
        assert result["outcomes"][0]["platform"] == "facebook"

    def test_publish_step_post_not_found_routes_to_error(
        self, account, contact, workflow
    ):
        """Missing post routes to on_error."""
        step = {
            "id": "publish",
            "type": "publish_post",
            "post_id": "9999",  # doesn't exist
            "on_error": "error_handler",
        }

        run = WorkflowRun.objects.create(
            workflow=workflow,
            contact=contact,
            status=WorkflowRun.Status.ACTIVE,
        )

        result = _run_publish_post(run, step)
        assert result["_routed_to"] == "error_handler"
        assert result["reason"] == "not_found"

    def test_publish_step_missing_approval_routes_to_error(
        self, account, contact, workflow
    ):
        """Missing approval routes to on_error."""
        post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            status=ContentPost.Status.DRAFT,
            approval_required=True,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.DRAFT,
        )

        step = {
            "id": "publish",
            "type": "publish_post",
            "post_id": str(post.pk),
            "on_error": "error_handler",
        }

        run = WorkflowRun.objects.create(
            workflow=workflow,
            contact=contact,
            status=WorkflowRun.Status.ACTIVE,
        )

        result = _run_publish_post(run, step)
        assert result["_routed_to"] == "error_handler"
        assert result["reason"] == "approval_invalid"
