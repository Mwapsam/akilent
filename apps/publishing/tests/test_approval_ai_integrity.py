"""Workstream 1: the approval trail keeps the hash of what was approved, AI suggestions
leave a provenance mark and the accepted caption, and the post's brief reaches the prompt."""

import json
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.ai import drafting
from apps.ai.models import AIDraft
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.publishing.models import (
    ApprovalLog,
    ContentCampaign,
    ContentCaption,
    ContentPillar,
    ContentPlanItem,
    ContentPost,
    PublishingSettings,
)

pytestmark = pytest.mark.django_db

IMG = "https://example.com/a.jpg"


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Integrity Co")
    user = User.objects.create_user("integrity", "integrity@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, user, client


def _form(**extra):
    data = {
        "caption": "Hello",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_urls_json": json.dumps([IMG]),
    }
    data.update(extra)
    return data


def _ready_draft(account, options=None):
    return AIDraft.objects.create(
        account=account,
        kind=AIDraft.Kind.CONTENT_CAPTION,
        prompt="Summer launch",
        status=AIDraft.Status.READY,
        result={
            "options": options
            or [
                {"caption": "First idea", "hashtags": ["one"]},
                {"caption": "Second idea", "hashtags": ["two", "summer"]},
            ]
        },
    )


# --- Approval trail ---------------------------------------------------------------------


def test_the_trail_keeps_the_hash_of_what_was_submitted_and_approved(owner):
    account, _, client = owner
    PublishingSettings.objects.create(account=account, requires_review=True)
    client.post(reverse("publishing:post-new"), _form(action="submit_review"))
    post = ContentPost.objects.get(account=account)
    submitted = post.approval_logs.get(action=ApprovalLog.Action.SUBMITTED)
    assert submitted.content_hash == post.compute_content_hash()

    client.post(reverse("publishing:post-approve", args=[post.pk]))

    post.refresh_from_db()
    approved = post.approval_logs.get(action=ApprovalLog.Action.APPROVED)
    assert post.is_approval_valid()
    assert (
        approved.content_hash == post.last_content_hash == post.compute_content_hash()
    )


def test_an_edit_after_approval_leaves_the_approved_hash_as_history(owner):
    account, _, client = owner
    PublishingSettings.objects.create(account=account, requires_review=True)
    client.post(reverse("publishing:post-new"), _form(action="submit_review"))
    post = ContentPost.objects.get(account=account)
    client.post(reverse("publishing:post-approve", args=[post.pk]))
    approved_hash = post.approval_logs.get(
        action=ApprovalLog.Action.APPROVED
    ).content_hash

    client.post(
        reverse("publishing:post-new"),
        _form(action="draft", post_id=str(post.pk), caption="Changed after approval"),
    )

    post.refresh_from_db()
    assert not post.is_approval_valid()
    assert post.approval_status == "draft"
    assert post.compute_content_hash() != approved_hash
    log = post.approval_logs.get(action=ApprovalLog.Action.APPROVED)
    assert log.content_hash == approved_hash


def test_requesting_changes_and_resubmitting_record_their_hash(owner):
    account, user, client = owner
    other = User.objects.create_user("writer", "writer@example.com", "pass")
    Membership.objects.create(user=other, account=account, role=Membership.Role.MEMBER)
    PublishingSettings.objects.create(account=account, requires_review=True)
    writer = Client()
    writer.force_login(other)
    writer.post(reverse("publishing:post-new"), _form(action="submit_review"))
    post = ContentPost.objects.get(account=account)

    client.post(
        reverse("publishing:post-request-changes", args=[post.pk]),
        {"feedback": "Shorter please"},
    )
    writer.post(reverse("publishing:post-resubmit-after-changes", args=[post.pk]))

    expected = post.compute_content_hash()
    for action in (
        ApprovalLog.Action.CHANGES_REQUESTED,
        ApprovalLog.Action.RESUBMITTED,
    ):
        assert post.approval_logs.get(action=action).content_hash == expected


# --- AI provenance ----------------------------------------------------------------------


def test_saving_an_applied_suggestion_marks_the_post_and_keeps_the_caption(owner):
    account, _, client = owner
    draft = _ready_draft(account)

    client.post(
        reverse("publishing:post-new"),
        _form(
            action="draft", caption="Second idea", ai_draft=str(draft.pk), ai_option="1"
        ),
    )

    post = ContentPost.objects.get(account=account)
    assert post.ai_assisted
    caption = ContentCaption.objects.get(post=post)
    assert caption.accepted and caption.body == "Second idea"
    assert caption.hashtags == ["two", "summer"]
    assert caption.prompt == "Summer launch"
    draft.refresh_from_db()
    assert draft.status == AIDraft.Status.USED and draft.used_at is not None


def test_saving_again_keeps_one_accepted_caption(owner):
    account, _, client = owner
    draft = _ready_draft(account)
    form = _form(action="draft", ai_draft=str(draft.pk), ai_option="0")
    client.post(reverse("publishing:post-new"), form)
    post = ContentPost.objects.get(account=account)

    client.post(reverse("publishing:post-new"), {**form, "post_id": str(post.pk)})

    assert ContentCaption.objects.filter(post=post).count() == 1


@pytest.mark.parametrize(
    "case", ["other_workspace", "pending", "out_of_range", "no_pick", "garbage"]
)
def test_only_a_real_finished_suggestion_counts(owner, case):
    account, _, client = owner
    other = Account.objects.create(company_name="Someone Else")
    draft = _ready_draft(other if case == "other_workspace" else account)
    if case == "pending":
        draft.status = AIDraft.Status.PENDING
        draft.save(update_fields=["status"])
    extra = {
        "other_workspace": {"ai_draft": str(draft.pk), "ai_option": "0"},
        "pending": {"ai_draft": str(draft.pk), "ai_option": "0"},
        "out_of_range": {"ai_draft": str(draft.pk), "ai_option": "5"},
        "no_pick": {},
        "garbage": {"ai_draft": "x1", "ai_option": "-1"},
    }[case]

    client.post(reverse("publishing:post-new"), _form(action="draft", **extra))

    post = ContentPost.objects.get(account=account)
    assert not post.ai_assisted
    assert not ContentCaption.objects.exists()


def test_the_post_page_tells_reviewers_the_caption_came_from_ai(owner):
    account, _, client = owner
    PublishingSettings.objects.create(account=account, requires_review=True)
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="image",
        caption="Hi",
        ai_assisted=True,
    )

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    assert "AI-assisted caption" in html
    assert "Started from an AI suggestion" in html


# --- The brief reaches the prompt --------------------------------------------------------


@pytest.fixture
def planned_post(owner):
    account, user, _ = owner
    item = ContentPlanItem.objects.create(
        account=account,
        topic="Summer sandals",
        notes="Show them on the beach",
        pillar=ContentPillar.objects.create(account=account, name="Product demos"),
        campaign=ContentCampaign.objects.create(
            account=account, name="Summer sale", objective="Clear last season's stock"
        ),
        platforms=["facebook"],
    )
    return ContentPost.objects.create(
        account=account,
        plan_item=item,
        platforms=["facebook"],
        media_type="image",
        created_by=user,
        brief_objective="Drive sales",
        brief_audience="Women 25-40 in Lusaka",
        brief_offer="20% off this week",
    )


def test_generating_for_a_post_sends_its_brief_and_writes_no_empty_caption(
    owner, planned_post
):
    account, _, client = owner

    with patch("apps.ai.tasks.build_draft.delay"):
        res = client.post(
            reverse("publishing:caption-generate"),
            {
                "prompt": "Summer sandals",
                "platform": "facebook",
                "post_id": planned_post.pk,
            },
        )

    assert res.status_code == 200
    brief = AIDraft.objects.get(account=account).context["brief"]
    assert brief["Objective"] == "Drive sales"
    assert brief["Audience"] == "Women 25-40 in Lusaka"
    assert brief["Offer"] == "20% off this week"
    assert brief["Planned topic"] == "Summer sandals"
    assert brief["Planning notes"] == "Show them on the beach"
    assert brief["Content pillar"] == "Product demos"
    assert brief["Campaign"] == "Summer sale"
    assert brief["Campaign objective"] == "Clear last season's stock"
    assert not ContentCaption.objects.exists()


def test_another_workspaces_post_brings_no_brief(owner, planned_post):
    _, _, _ = owner
    stranger = User.objects.create_user("stranger", "s@example.com", "pass")
    other = Account.objects.create(company_name="Other Co")
    Membership.objects.create(user=stranger, account=other, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(stranger)

    with patch("apps.ai.tasks.build_draft.delay"):
        client.post(
            reverse("publishing:caption-generate"),
            {"prompt": "x", "platform": "facebook", "post_id": planned_post.pk},
        )

    assert "brief" not in AIDraft.objects.get(account=other).context


class _Fake(AIProvider):
    calls: list = []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        _Fake.calls.append(messages[-1].content)
        return CompletionResult(
            text=json.dumps({"options": [{"caption": "Hi", "hashtags": []}]}),
            model="fake",
        )


def test_the_brief_is_in_the_message_the_model_reads(owner):
    account, _, _ = owner
    _Fake.calls = []
    draft = AIDraft.objects.create(
        account=account,
        kind=AIDraft.Kind.CONTENT_CAPTION,
        prompt="Summer sandals",
        context={
            "platform": "facebook",
            "media_type": "image",
            "brief": {"Audience": "Women 25-40", "Offer": "20% off"},
        },
    )

    drafting.run(draft, _Fake())

    assert "- Audience: Women 25-40" in _Fake.calls[0]
    assert "- Offer: 20% off" in _Fake.calls[0]
    assert draft.result == {"options": [{"caption": "Hi", "hashtags": []}]}


# --- AI help for a plan item's draft -----------------------------------------------------


def test_a_draft_made_from_a_plan_item_offers_ai_with_its_topic(owner, planned_post):
    _, _, client = owner

    res = client.get(reverse("publishing:post-new"), {"post_id": planned_post.pk})

    html = res.content.decode()
    assert 'id="brief-topic"' in html
    assert res.context["editor_data"]["brief_topic"] == "Summer sandals"


def test_a_written_draft_does_not_offer_to_start_from_ai(owner, planned_post):
    _, _, client = owner
    planned_post.caption = "Already written"
    planned_post.save(update_fields=["caption"])

    html = client.get(
        reverse("publishing:post-new"), {"post_id": planned_post.pk}
    ).content.decode()

    assert 'id="brief-topic"' not in html
