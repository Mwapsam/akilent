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


def _post_for(account, creator, reviewer=None):
    return ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        created_by=creator,
        approval_required=True,
        approval_status="submitted",
        assigned_reviewer=reviewer,
    )


def _login(user):
    client = Client()
    client.force_login(user)
    return client


def test_creator_cannot_self_approve_when_another_admin_can_review(account):
    creator = User.objects.create_user("creator", "c@example.com", "pass")
    admin = User.objects.create_user("mate", "m@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=admin, account=account, role=Membership.Role.ADMIN)
    post = _post_for(account, creator)

    _login(creator).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "submitted"


def test_creator_who_assigns_themselves_as_reviewer_cannot_self_approve(account):
    creator = User.objects.create_user("selfrev", "sr@example.com", "pass")
    admin = User.objects.create_user("mate5", "m5@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=admin, account=account, role=Membership.Role.ADMIN)
    post = _post_for(account, creator, reviewer=creator)

    _login(creator).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "submitted"


def test_creator_can_self_approve_in_a_solo_workspace(account):
    creator = User.objects.create_user("solo", "s@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    post = _post_for(account, creator)

    _login(creator).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "approved"


def test_sole_admin_can_approve_their_own_post_when_others_are_members(account):
    """Otherwise nobody could ever approve the only Owner's posts."""
    owner = User.objects.create_user("owner6", "o6@example.com", "pass")
    member = User.objects.create_user("member6", "m6@example.com", "pass")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    post = _post_for(account, owner)

    _login(owner).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "approved"


def test_admin_can_approve_when_no_reviewer_is_assigned(account):
    creator = User.objects.create_user("creator2", "c2@example.com", "pass")
    admin = User.objects.create_user("mate2", "m2@example.com", "pass")
    Membership.objects.create(
        user=creator, account=account, role=Membership.Role.MEMBER
    )
    Membership.objects.create(user=admin, account=account, role=Membership.Role.ADMIN)
    post = _post_for(account, creator)

    _login(admin).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "approved"
    assert post.approved_by == admin


def test_member_cannot_approve(account):
    creator = User.objects.create_user("creator7", "c7@example.com", "pass")
    member = User.objects.create_user("member7", "m7@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    post = _post_for(account, creator)

    _login(member).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "submitted"


def test_non_member_cannot_approve(account):
    creator = User.objects.create_user("creator3", "c3@example.com", "pass")
    outsider = User.objects.create_user("outsider", "o@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    post = _post_for(account, creator)

    _login(outsider).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "submitted"


def test_explicit_reviewer_takes_precedence_over_the_fallback(account):
    creator = User.objects.create_user("creator4", "c4@example.com", "pass")
    other_admin = User.objects.create_user("mate4", "m4@example.com", "pass")
    reviewer = User.objects.create_user("rev4", "r4@example.com", "pass")
    Membership.objects.create(
        user=creator, account=account, role=Membership.Role.MEMBER
    )
    for u in (other_admin, reviewer):
        Membership.objects.create(user=u, account=account, role=Membership.Role.ADMIN)
    post = _post_for(account, creator, reviewer=reviewer)

    _login(other_admin).post(reverse("publishing:post-approve", args=[post.pk]))
    post.refresh_from_db()
    assert post.approval_status == "submitted"

    _login(reviewer).post(reverse("publishing:post-approve", args=[post.pk]))
    post.refresh_from_db()
    assert post.approval_status == "approved"


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


def test_creator_cannot_request_changes_on_own_post_when_another_admin_can(account):
    creator = User.objects.create_user("rcreator", "rc@example.com", "pass")
    admin = User.objects.create_user("rmate", "rm@example.com", "pass")
    Membership.objects.create(user=creator, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=admin, account=account, role=Membership.Role.ADMIN)
    post = _post_for(account, creator)

    _login(creator).post(
        reverse("publishing:post-request-changes", args=[post.pk]),
        {"feedback": "Please tweak"},
    )

    post.refresh_from_db()
    assert post.approval_status == "submitted"


def test_any_admin_can_approve_when_the_assigned_reviewer_has_left(account):
    creator = User.objects.create_user("lcreator", "lc@example.com", "pass")
    admin = User.objects.create_user("lmate", "lm@example.com", "pass")
    gone = User.objects.create_user("lgone", "lg@example.com", "pass")
    Membership.objects.create(
        user=creator, account=account, role=Membership.Role.MEMBER
    )
    Membership.objects.create(user=admin, account=account, role=Membership.Role.ADMIN)
    post = _post_for(account, creator, reviewer=gone)

    _login(admin).post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    assert post.approval_status == "approved"
