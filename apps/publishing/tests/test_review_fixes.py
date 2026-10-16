from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.tasks import (
    escalate_exhausted_processing_variants,
    reconcile_stale_publishing_variants,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return Account.objects.create(company_name="Review Co")


def test_creator_cannot_approve_their_own_submitted_post(account):
    creator = User.objects.create_user("creator", "c@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        created_by=creator,
        approval_required=True,
        approval_status="submitted",
    )
    client = Client()
    client.force_login(creator)

    client.post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "submitted"
    assert post.approved_by is None


def test_reconciler_reports_the_rows_it_escalated(account):
    for _ in range(3):
        post = ContentPost.objects.create(account=account, platforms=["facebook"])
        ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.PUBLISHING,
            last_attempt_at=timezone.now() - timedelta(hours=7),
        )

    result = reconcile_stale_publishing_variants()

    assert result == {"stale_found": 3, "escalated": 3}


def test_exhaustion_task_reports_the_rows_it_escalated(account):
    for i in range(3):
        post = ContentPost.objects.create(account=account, platforms=["instagram"])
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(hours=72),
            metadata={"ig_container_id": f"c{i}"},
        )

    result = escalate_exhausted_processing_variants(max_age_hours=48)

    assert result == {"exhausted_found": 3, "escalated": 3}


def test_workflow_publish_step_keeps_media_id_keys(account):
    from apps.automation.models import Workflow, WorkflowRun
    from apps.automation.workflow_engine import _run_publish_post

    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image"
    )
    ContentPostVariant.objects.create(post=post, platform="facebook", caption="x")
    workflow = Workflow.objects.create(account=account, name="wf")
    from apps.contacts.models import Contact

    contact = Contact.objects.create(account=account, phone="+15550000002")
    run = WorkflowRun.objects.create(workflow=workflow, contact=contact)

    with patch(
        "apps.publishing.services.orchestrate_post_publication",
        return_value=("published", []),
    ):
        result = _run_publish_post(
            run, {"type": "publish_post", "post_id": str(post.pk)}
        )

    assert "ig_media_id" in result and "fb_post_id" in result
    assert "ig_media_id" in run.context and "fb_post_id" in run.context
