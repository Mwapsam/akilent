"""Where each post stands and the one thing someone should do next.

Every post that still needs attention falls into exactly one bucket, checked in the order of
``BUCKETS``: a channel that may or may not have gone live outranks a failure, a failure
outranks review, and so on. Review state comes from ``approval_status``, never from the
post's lifecycle ``status``: a scheduled post can be waiting for approval and stay scheduled.
"""

from __future__ import annotations

from collections.abc import Callable

from django.db.models import Q
from django.urls import reverse

from .models import ContentPost, ContentPostVariant

# (key, label), in the order a post is checked against them.
BUCKETS = (
    ("needs_check", "Needs a check"),
    ("failed", "Failed"),
    ("publishing", "Publishing now"),
    ("changes_requested", "Changes requested"),
    ("awaiting_approval", "Awaiting approval"),
    ("approved", "Approved"),
    ("scheduled", "Scheduled"),
    ("draft", "Drafts"),
    # Posts saved as an idea; planned ideas on the Planning tab aren't posts yet.
    ("idea", "Post ideas"),
)
BUCKET_LABELS = dict(BUCKETS)

# Buckets whose posts wait on someone in the business, shown in "Needs you".
ACTION_BUCKETS = (
    "needs_check",
    "failed",
    "changes_requested",
    "awaiting_approval",
    "approved",
)

_IN_FLIGHT = (
    ContentPostVariant.Status.PUBLISHING,
    ContentPostVariant.Status.PROCESSING,
)


def active_posts(account):
    """Posts that are not finished: everything except published and cancelled ones, plus
    any whose channel still failed or needs a check."""
    open_channels = Q(
        variants__status__in=[
            ContentPostVariant.Status.FAILED,
            ContentPostVariant.Status.RECONCILING,
        ]
    )
    finished = Q(
        status__in=[ContentPost.Status.PUBLISHED, ContentPost.Status.CANCELLED]
    )
    return (
        ContentPost.objects.filter(account=account)
        .filter(~finished | open_channels)
        .distinct()
        .prefetch_related("variants")
        .select_related("created_by")
    )


def bucket_for(post: ContentPost) -> str:
    """The single bucket ``post`` belongs to (``post.variants`` should be prefetched)."""
    statuses = {
        v.status
        for v in post.variants.all()
        if v.status != ContentPostVariant.Status.CANCELLED
    }
    if ContentPostVariant.Status.RECONCILING in statuses:
        return "needs_check"
    if (
        post.status == ContentPost.Status.FAILED
        or ContentPostVariant.Status.FAILED in statuses
    ):
        return "failed"
    if post.status == ContentPost.Status.PUBLISHING or statuses & set(_IN_FLIGHT):
        return "publishing"
    if post.requires_approval:
        if post.approval_status == "changes_requested":
            return "changes_requested"
        if post.approval_status == "submitted":
            return "awaiting_approval"
        if (
            post.status != ContentPost.Status.SCHEDULED
            and post.approval_status == "approved"
            and post.is_approval_valid()
        ):
            return "approved"
    if post.status == ContentPost.Status.SCHEDULED:
        return "scheduled"
    if post.status == ContentPost.Status.IDEA:
        return "idea"
    return "draft"


def next_action(
    post, bucket: str, user, can_review: Callable, is_manager: bool | None = None
) -> dict:
    """``{"label", "url"}`` for what ``user`` should do about ``post``; ``url`` is empty
    when the next step is someone else's, or nobody's.

    ``can_review(post)`` says whether ``user`` may review this post (the views' policy).
    """
    detail = reverse("publishing:post-detail", args=[post.pk])
    edit = f"{reverse('publishing:post-new')}?post_id={post.pk}"
    if is_manager is None:
        is_manager = can_review_any(post, user)

    if bucket == "needs_check":
        if is_manager:
            return {
                "label": "Check whether it went live",
                "url": reverse("publishing:reconciliation-queue"),
            }
        return {"label": "An Owner or Admin needs to check this", "url": ""}
    if bucket == "failed":
        return {"label": "See why it failed and fix it", "url": detail}
    if bucket == "publishing":
        return {"label": "Publishing now", "url": ""}
    if bucket == "changes_requested":
        if post.created_by_id == user.pk:
            return {"label": "Make the changes and resubmit", "url": edit}
        return {"label": "Waiting for the writer's changes", "url": ""}
    if bucket == "awaiting_approval":
        if can_review(post):
            return {"label": "Review it", "url": detail}
        return {"label": "Waiting for an Owner or Admin to review", "url": ""}
    if bucket == "approved":
        return {"label": "Schedule or publish it", "url": edit}
    if bucket == "scheduled":
        if post.requires_approval and not post.is_approval_valid():
            return {
                "label": "Submit for review: it won't go out until approved",
                "url": detail,
            }
        return {"label": "Nothing to do: it goes out on time", "url": ""}
    if bucket == "idea":
        return {"label": "Write the post", "url": edit}
    if post.requires_approval:
        return {"label": "Finish it and submit for review", "url": edit}
    return {"label": "Finish it and schedule", "url": edit}


def can_review_any(post, user) -> bool:
    """Whether ``user`` is an Owner or Admin of the post's workspace."""
    from apps.accounts.models import Membership

    return Membership.objects.filter(
        account_id=post.account_id,
        user_id=user.pk,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()


_WARN_BUCKETS = ("needs_check", "failed", "changes_requested", "awaiting_approval")


def bucket_url(key: str) -> str:
    """The library filtered to exactly this bucket's posts (same rule as the count)."""
    return f"{reverse('publishing:library')}?bucket={key}"


def post_ids_in(account, key: str) -> list[int]:
    """Ids of the account's posts in bucket ``key``."""
    return [p.pk for p in active_posts(account) if bucket_for(p) == key]


def worklist(account, user, can_review: Callable, limit: int = 8) -> dict:
    """Counts for every bucket, and the posts waiting on someone with their next action."""
    from apps.accounts.models import Membership

    from .models import PublishingSettings

    counts = dict.fromkeys(BUCKET_LABELS, 0)
    needs_you = []
    order = {key: i for i, key in enumerate(ACTION_BUCKETS)}
    # Read once for the whole list, not once per post.
    review = PublishingSettings.requires_review_for(account.pk)
    is_manager = Membership.objects.filter(
        account_id=account.pk,
        user_id=user.pk,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()
    for post in active_posts(account):
        post._workspace_requires_review = review
        bucket = bucket_for(post)
        counts[bucket] += 1
        if bucket in order:
            needs_you.append((order[bucket], post, bucket))
    needs_you.sort(key=lambda row: (row[0], -row[1].updated_at.timestamp()))
    return {
        "counts": counts,
        "buckets": [
            {
                "key": key,
                "label": label,
                "count": counts[key],
                "url": bucket_url(key),
                "warn": bool(counts[key]) and key in _WARN_BUCKETS,
            }
            for key, label in BUCKETS
        ],
        "needs_you": [
            {
                "post": post,
                "bucket": bucket,
                "bucket_label": BUCKET_LABELS[bucket],
                "action": next_action(post, bucket, user, can_review, is_manager),
            }
            for _, post, bucket in needs_you[:limit]
        ],
        "needs_you_total": len(needs_you),
        "empty_labels": [label for key, label in BUCKETS if not counts[key]],
    }
