"""Workstream 2: every unfinished post is in exactly one bucket with one permission-aware
next action, the library filters by the same rule, and Owners/Admins get one email per
channel problem episode."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing import alerts
from apps.publishing.models import ContentPost, ContentPostVariant, PublishingSettings
from apps.publishing.services import initialize_post_with_variants
from apps.publishing.worklist import (
    BUCKETS,
    active_posts,
    bucket_for,
    next_action,
    worklist,
)

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status


@pytest.fixture
def team():
    account = Account.objects.create(company_name="Worklist Co")
    owner = User.objects.create_user("own", "own@example.com", "pass")
    admin = User.objects.create_user("adm", "adm@example.com", "pass")
    member = User.objects.create_user("mem", "mem@example.com", "pass")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    Membership.objects.create(user=admin, account=account, role=Membership.Role.ADMIN)
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    return account, owner, admin, member


def _post(account, user=None, *, status="draft", variant_statuses=None, **fields):
    post = ContentPost.objects.create(
        account=account,
        platforms=list((variant_statuses or {"facebook": None}).keys()),
        media_type="image",
        caption=fields.pop("caption", "A post"),
        status=status,
        created_by=user,
        **fields,
    )
    for v in initialize_post_with_variants(post, post.platforms):
        wanted = (variant_statuses or {}).get(v.platform)
        if wanted:
            v.status = wanted
            v.save(update_fields=["status"])
    return ContentPost.objects.prefetch_related("variants").get(pk=post.pk)


def _approve(post, user):
    post.approval_status = "approved"
    post.approved_by = user
    post.approved_at = timezone.now()
    post.last_content_hash = post.compute_content_hash()
    post.save()


def _always(_post):
    return True


def _never(_post):
    return False


# --- Buckets -----------------------------------------------------------------------------


def test_each_unfinished_post_is_in_exactly_one_bucket(team):
    account, owner, _, _ = team
    PublishingSettings.objects.create(account=account, requires_review=True)
    made = {
        "idea": _post(account, owner, status="idea"),
        "draft": _post(account, owner),
        "awaiting_approval": _post(
            account, owner, status="scheduled", approval_status="submitted"
        ),
        "changes_requested": _post(account, owner, approval_status="changes_requested"),
        "failed": _post(
            account, owner, status="failed", variant_statuses={"facebook": S.FAILED}
        ),
        "publishing": _post(
            account,
            owner,
            status="publishing",
            variant_statuses={"facebook": S.PUBLISHING},
        ),
        "needs_check": _post(
            account,
            owner,
            status="publishing",
            variant_statuses={"facebook": S.FAILED, "instagram": S.RECONCILING},
        ),
    }
    approved = _post(account, owner)
    _approve(approved, owner)
    made["approved"] = approved
    scheduled = _post(account, owner, status="scheduled")
    _approve(scheduled, owner)
    made["scheduled"] = scheduled
    _post(
        account, owner, status="published", variant_statuses={"facebook": S.PUBLISHED}
    )
    _post(account, owner, status="cancelled")

    for expected, post in made.items():
        post = ContentPost.objects.prefetch_related("variants").get(pk=post.pk)
        assert bucket_for(post) == expected, expected

    work = worklist(account, owner, _always)
    assert sum(work["counts"].values()) == len(made) == active_posts(account).count()
    assert all(work["counts"][key] == 1 for key, _ in BUCKETS)


def test_a_published_post_with_a_channel_to_check_stays_on_the_list(team):
    account, owner, _, _ = team
    post = _post(
        account,
        owner,
        status="published",
        variant_statuses={"facebook": S.PUBLISHED, "instagram": S.RECONCILING},
    )

    assert bucket_for(post) == "needs_check"
    assert [p.pk for p in active_posts(account)] == [post.pk]


# --- Next action -------------------------------------------------------------------------


def test_a_channel_to_check_is_never_offered_a_retry(team):
    account, owner, _, member = team
    post = _post(
        account,
        owner,
        status="publishing",
        variant_statuses={"facebook": S.FAILED, "instagram": S.RECONCILING},
    )

    for_owner = next_action(post, bucket_for(post), owner, _always)
    for_member = next_action(post, bucket_for(post), member, _never)

    assert "retry" not in for_owner["label"].lower()
    assert for_owner["url"] == reverse("publishing:reconciliation-queue")
    assert for_member["url"] == "" and "Owner or Admin" in for_member["label"]


def test_review_is_offered_only_to_someone_who_may_review(team):
    account, owner, _, member = team
    PublishingSettings.objects.create(account=account, requires_review=True)
    post = _post(account, member, approval_status="submitted")

    assert next_action(post, "awaiting_approval", owner, _always) == {
        "label": "Review it",
        "url": reverse("publishing:post-detail", args=[post.pk]),
    }
    waiting = next_action(post, "awaiting_approval", member, _never)
    assert waiting["url"] == "" and "Waiting" in waiting["label"]


def test_only_the_writer_is_asked_to_make_requested_changes(team):
    account, owner, _, member = team
    PublishingSettings.objects.create(account=account, requires_review=True)
    post = _post(account, member, approval_status="changes_requested")

    assert "resubmit" in next_action(post, "changes_requested", member, _never)["label"]
    assert next_action(post, "changes_requested", owner, _always)["url"] == ""


def test_an_unapproved_scheduled_post_is_not_told_it_will_go_out(team):
    account, owner, _, _ = team
    PublishingSettings.objects.create(account=account, requires_review=True)
    post = _post(account, owner, status="scheduled")

    action = next_action(post, bucket_for(post), owner, _always)

    assert bucket_for(post) == "scheduled"
    assert "Submit for review" in action["label"]


def test_a_draft_needing_review_is_not_told_to_publish(team):
    account, owner, _, _ = team
    PublishingSettings.objects.create(account=account, requires_review=True)
    post = _post(account, owner)

    label = next_action(post, "draft", owner, _always)["label"]

    assert "review" in label and "schedule" not in label


# --- Overview and library ----------------------------------------------------------------


@pytest.fixture
def owner_client(team):
    client = Client()
    client.force_login(team[1])
    return client


def test_the_overview_shows_every_bucket_and_what_needs_you(team, owner_client):
    account, owner, _, _ = team
    _post(
        account,
        owner,
        status="failed",
        caption="Broken one",
        error="Token expired",
        variant_statuses={"facebook": S.FAILED},
    )

    html = owner_client.get(reverse("publishing:index")).content.decode()

    for _, label in BUCKETS:
        assert label in html
    assert "Broken one" in html
    assert "See why it failed and retry" in html


def test_the_library_lists_exactly_the_posts_the_overview_counts(team, owner_client):
    account, owner, _, _ = team
    PublishingSettings.objects.create(account=account, requires_review=True)
    scheduled_waiting = _post(
        account,
        owner,
        status="scheduled",
        approval_status="submitted",
        caption="Sched wait",
    )
    draft_waiting = _post(
        account, owner, approval_status="submitted", caption="Draft wait"
    )
    _post(account, owner, caption="Plain draft")
    _post(
        account,
        owner,
        status="failed",
        approval_status="submitted",
        caption="Failed but submitted",
        variant_statuses={"facebook": S.FAILED},
    )

    res = owner_client.get(
        reverse("publishing:library"), {"bucket": "awaiting_approval"}
    )

    listed = {p.pk for p in res.context["page_obj"].object_list}
    assert listed == {scheduled_waiting.pk, draft_waiting.pk}
    assert worklist(account, owner, _always)["counts"]["awaiting_approval"] == len(
        listed
    )


def test_the_failed_bucket_includes_a_post_with_one_failed_channel(team, owner_client):
    account, owner, _, _ = team
    partial = _post(
        account,
        owner,
        status="publishing",
        variant_statuses={"facebook": S.PUBLISHED, "instagram": S.FAILED},
    )

    res = owner_client.get(reverse("publishing:library"), {"bucket": "failed"})

    assert [p.pk for p in res.context["page_obj"].object_list] == [partial.pk]


def test_an_unknown_bucket_is_ignored(team, owner_client):
    account, owner, _, _ = team
    _post(account, owner)

    res = owner_client.get(reverse("publishing:library"), {"bucket": "nope"})

    assert res.context["bucket_filter"] == ""
    assert len(res.context["page_obj"].object_list) == 1


def test_the_status_filters_no_longer_offer_in_review(team, owner_client):
    res = owner_client.get(reverse("publishing:library"))

    assert "in_review" not in dict(res.context["status_choices"])


def test_another_workspaces_posts_are_never_counted(team):
    account, owner, _, _ = team
    other = Account.objects.create(company_name="Other")
    _post(other, None, status="failed", variant_statuses={"facebook": S.FAILED})

    assert sum(worklist(account, owner, _always)["counts"].values()) == 0


# --- Alerts ------------------------------------------------------------------------------


@pytest.fixture
def outbox():
    sent = []
    with patch(
        "apps.email.services.send.send_system_email",
        side_effect=lambda **kw: sent.append(kw),
    ):
        yield sent


def _fail(
    post,
    platform="facebook",
    *,
    ago=timedelta(minutes=20),
    error="Bad media",
    kind=None,
):
    v = post.variants.get(platform=platform)
    v.status = S.FAILED
    v.last_error = error
    v.last_error_type = kind or ContentPostVariant.ErrorType.VALIDATION
    v.last_error_at = timezone.now() - ago
    v.save()
    return v


def test_a_settled_failure_emails_owners_and_admins_once(team, outbox):
    account, owner, _, _ = team
    post = _post(account, owner, caption="Summer sale is on")
    _fail(post)

    assert alerts.send_due_alerts()["sent"] == 1
    assert alerts.send_due_alerts()["sent"] == 0

    assert sorted(m["to_email"] for m in outbox) == [
        "adm@example.com",
        "own@example.com",
    ]
    body = outbox[0]["text_body"]
    assert outbox[0]["subject"] == "Your Facebook post didn't publish"
    assert "Summer sale is on" in body and "Reason: Bad media" in body
    assert reverse("publishing:post-detail", args=[post.pk]) in body


def test_a_failure_still_being_retried_is_not_reported_yet(team, outbox):
    account, owner, _, _ = team
    _fail(_post(account, owner), ago=timedelta(minutes=5))

    assert alerts.send_due_alerts()["sent"] == 0


def test_old_failures_are_not_reported_when_alerts_start(team, outbox):
    account, owner, _, _ = team
    _fail(_post(account, owner), ago=timedelta(days=4))

    assert alerts.send_due_alerts()["sent"] == 0


def test_a_retry_that_fails_again_is_the_same_episode(team, outbox):
    account, owner, _, _ = team
    post = _post(account, owner)
    variant = _fail(post)
    alerts.send_due_alerts()
    outbox.clear()

    variant.refresh_from_db()
    variant.status = S.PUBLISHING
    variant.save(update_fields=["status"])
    _fail(post, error="Still bad")

    assert alerts.send_due_alerts()["sent"] == 0
    assert outbox == []


def test_a_channel_that_needs_a_check_gets_its_own_message(team, outbox):
    account, owner, _, _ = team
    post = _post(account, owner)
    variant = _fail(post)
    alerts.send_due_alerts()
    outbox.clear()

    variant.refresh_from_db()
    variant.status = S.RECONCILING
    variant.save(update_fields=["status"])

    assert alerts.send_due_alerts()["sent"] == 1
    assert outbox[0]["subject"] == "Check whether your Facebook post went live"
    assert "won't retry it on its own" in outbox[0]["text_body"]
    assert reverse("publishing:reconciliation-queue") in outbox[0]["text_body"]


def test_publishing_closes_the_episode_so_a_later_failure_is_reported(team, outbox):
    account, owner, _, _ = team
    post = _post(account, owner)
    variant = _fail(post)
    alerts.send_due_alerts()

    variant.refresh_from_db()
    variant.status = S.PUBLISHED
    variant.save(update_fields=["status"])
    assert alerts.send_due_alerts()["closed"] == 1
    variant.refresh_from_db()
    assert alerts.ALERTS_KEY not in variant.metadata

    outbox.clear()
    _fail(post)
    assert alerts.send_due_alerts()["sent"] == 1


def test_a_permission_failure_says_to_reconnect(team, outbox):
    account, owner, _, _ = team
    _fail(_post(account, owner), kind=ContentPostVariant.ErrorType.PERMISSION)

    alerts.send_due_alerts()

    assert "Reconnect Facebook" in outbox[0]["text_body"]


def test_each_workspace_hears_only_about_its_own_posts(team, outbox):
    account, owner, _, _ = team
    other = Account.objects.create(company_name="Other Shop")
    stranger = User.objects.create_user("str", "str@example.com", "pass")
    Membership.objects.create(user=stranger, account=other, role=Membership.Role.OWNER)
    _fail(_post(other, stranger, caption="Their secret launch"))

    alerts.send_due_alerts()

    assert [m["to_email"] for m in outbox] == ["str@example.com"]
    assert "Other Shop" in outbox[0]["text_body"]
