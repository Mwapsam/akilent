"""Content publishing models.

ContentPillar       — content theme/category for planning.
ContentCampaign     — groups related content around a goal.
ContentPlanItem     — a planned content idea (not yet a post).
ContentPost         — a piece of content moving through publishing.
ContentCaption      — AI-generated caption drafts for a post (or standalone).
FacebookPageConnection — per-account OAuth token for a connected Facebook Page.
"""

from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from apps.accounts.fields import EncryptedTextField


def _post_id() -> str:
    return "post_" + secrets.token_hex(16)


def _fb_conn_id() -> str:
    return "fbconn_" + secrets.token_hex(12)


# ---------------------------------------------------------------------------
# Planning layer
# ---------------------------------------------------------------------------


class ContentPillar(models.Model):
    """A content theme / publishing category."""

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="pillars"
    )
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=20, default="#6366f1")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ContentCampaign(models.Model):
    """Groups related content around a launch, offer, or business objective."""

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="campaigns"
    )
    name = models.CharField(max_length=200)
    objective = models.TextField(blank=True, default="")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class ContentPlanItem(models.Model):
    """A planned content idea — not yet a post."""

    class PlanStatus(models.TextChoices):
        IDEA = "idea", "Idea"
        PLANNED = "planned", "Planned"
        IN_PRODUCTION = "in_production", "In Production"
        PUBLISHED = "published", "Published"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="plan_items"
    )
    pillar = models.ForeignKey(
        ContentPillar,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plan_items",
    )
    campaign = models.ForeignKey(
        ContentCampaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plan_items",
    )
    topic = models.CharField(max_length=255)
    objective = models.TextField(blank=True, default="")
    platforms = models.JSONField(default=list)
    media_type = models.CharField(
        max_length=10,
        choices=[
            ("image", "Image"),
            ("video", "Video"),
            ("reel", "Reel"),
            ("story", "Story"),
            ("text", "Text (Facebook only)"),
            ("carousel", "Carousel"),
        ],
        default="image",
    )
    planned_date = models.DateField(null=True, blank=True)
    plan_status = models.CharField(
        max_length=15, choices=PlanStatus.choices, default=PlanStatus.IDEA
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["planned_date", "-created_at"]

    def __str__(self) -> str:
        return self.topic

    def clean(self) -> None:
        if (
            self.pillar_id
            and self.pillar is not None
            and self.pillar.account_id != self.account_id
        ):
            raise ValidationError("Pillar does not belong to this account.")
        if (
            self.campaign_id
            and self.campaign is not None
            and self.campaign.account_id != self.account_id
        ):
            raise ValidationError("Campaign does not belong to this account.")

    def refresh_plan_status(self) -> None:
        """Sync plan_status based on linked posts' publishing outcomes."""
        posts = list(self.posts.all())
        if not posts:
            return
        if all(p.status == ContentPost.Status.PUBLISHED for p in posts):
            self.plan_status = self.PlanStatus.PUBLISHED
        elif any(
            p.status
            in (
                ContentPost.Status.DRAFT,
                ContentPost.Status.SCHEDULED,
                ContentPost.Status.PUBLISHING,
            )
            for p in posts
        ):
            self.plan_status = self.PlanStatus.IN_PRODUCTION
        self.save(update_fields=["plan_status"])


# ---------------------------------------------------------------------------
# Publishing layer
# ---------------------------------------------------------------------------


class ContentPost(models.Model):
    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        REEL = "reel", "Reel"
        STORY = "story", "Story"
        TEXT = "text", "Text (Facebook only)"
        CAROUSEL = "carousel", "Carousel"

    class Status(models.TextChoices):
        IDEA = "idea", "Idea"
        DRAFT = "draft", "Draft"
        IN_REVIEW = "in_review", "In Review"
        APPROVED = "approved", "Approved"
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    public_id = models.CharField(
        max_length=45, unique=True, default=_post_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="content_posts"
    )
    plan_item = models.ForeignKey(
        ContentPlanItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="posts",
    )
    platforms = models.JSONField(default=list)  # ["instagram", "facebook"]
    media_type = models.CharField(max_length=10, choices=MediaType.choices)
    caption = models.TextField(blank=True, default="")
    platform_captions = models.JSONField(default=dict, blank=True)
    hashtags = models.JSONField(default=list)
    media_urls = models.JSONField(default=list)  # S3/CDN URLs

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)

    # Platform-specific result IDs (set after successful publish)
    ig_media_id = models.CharField(max_length=64, blank=True, default="")
    ig_container_id = models.CharField(max_length=64, blank=True, default="")
    fb_post_id = models.CharField(max_length=64, blank=True, default="")

    # Facebook post engagement metrics (fetched after publish via pages_read_engagement)
    fb_reach = models.PositiveIntegerField(null=True, blank=True)
    fb_impressions = models.PositiveIntegerField(null=True, blank=True)
    fb_insights_fetched_at = models.DateTimeField(null=True, blank=True)

    error = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-scheduled_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.account} / {self.media_type} / {self.status}"

    @property
    def full_caption(self) -> str:
        tags = " ".join(f"#{t.lstrip('#')}" for t in self.hashtags)
        if tags:
            return f"{self.caption}\n\n{tags}".strip()
        return self.caption

    def caption_for(self, platform: str) -> str:
        """Return the platform-specific caption, falling back to the shared caption."""
        override = (self.platform_captions or {}).get(platform, "")
        return override if override else self.caption


class ContentCaption(models.Model):
    """AI-generated caption drafts — linked to a post or standalone (pre-post generation)."""

    post = models.ForeignKey(
        ContentPost,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="captions",
    )
    prompt = models.TextField()
    body = models.TextField()
    hashtags = models.JSONField(default=list)
    accepted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class FacebookPageConnection(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISCONNECTED = "disconnected", "Disconnected"

    public_id = models.CharField(
        max_length=40, unique=True, default=_fb_conn_id, editable=False
    )
    account = models.OneToOneField(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="facebook_page_connection",
    )
    page_id = models.CharField(max_length=64, blank=True, default="")
    page_name = models.CharField(max_length=255, blank=True, default="")
    access_token = EncryptedTextField(blank=True, null=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    oauth_state = models.CharField(max_length=64, blank=True, default="")
    oauth_state_created_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.account} / FB Page: {self.page_name or self.page_id}"
