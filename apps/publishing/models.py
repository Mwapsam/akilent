"""Content publishing models.

ContentPillar       — content theme/category for planning.
ContentCampaign     — groups related content around a goal.
ContentPlanItem     — a planned content idea (not yet a post).
ContentPost         — a piece of content moving through publishing.
ContentCaption      — AI-generated caption drafts for a post (or standalone).
FacebookPageConnection — per-account OAuth token for a connected Facebook Page.
PublishingSettings  — per-account Content Studio settings (review requirement).
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

    # Brief context — inherited from plan item or entered in composer
    brief_objective = models.TextField(blank=True, default="")
    brief_audience = models.TextField(blank=True, default="")
    brief_problem = models.TextField(blank=True, default="")
    brief_offer = models.TextField(blank=True, default="")
    brief_message = models.TextField(blank=True, default="")
    brief_cta = models.TextField(blank=True, default="")

    # The post's call to action (see apps.publishing.links.parse_cta):
    # {"kind": "none"|"whatsapp"|"link", "text": str, "url": str, "prefill": str}
    cta = models.JSONField(default=dict, blank=True)

    error = models.TextField(blank=True, default="")

    # Approval workflow
    approval_required = models.BooleanField(
        default=False
    )  # Can solo creator publish without review?
    assigned_reviewer = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posts_to_review",
    )
    approval_status = models.CharField(
        max_length=20,
        choices=[
            ("draft", "Draft"),
            ("submitted", "Submitted for Review"),
            ("changes_requested", "Changes Requested"),
            ("approved", "Approved"),
        ],
        default="draft",
    )
    approved_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_posts",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_feedback = models.TextField(blank=True, default="")
    last_content_hash = models.CharField(
        max_length=64, blank=True, default=""
    )  # SHA256 of caption+platform_captions+media_urls when approved

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

    def compute_content_hash(self) -> str:
        return self.content_hash_for()

    def content_hash_for(self, variants=None) -> str:
        """SHA256 over every field the provider payload can draw from.

        Includes post-level fallback fields (caption, platform_captions, hashtags,
        media_urls) because publishing uses them when a variant leaves them empty.
        Pass ``variants`` to hash an in-memory snapshot instead of re-querying.
        """
        import hashlib
        import json

        if variants is None:
            variants = self.variants.all()
        variant_contents = sorted(
            (
                {
                    "platform": v.platform,
                    "caption": v.caption or "",
                    "media_urls": v.media_urls or [],
                    "format_choice": v.format_choice or "",
                    # Only when set, so posts approved before options keep their hash.
                    **({"options": v.options} if v.options else {}),
                }
                for v in variants
            ),
            key=lambda d: d["platform"],
        )
        content = {
            "caption": self.caption or "",
            "platform_captions": self.platform_captions or {},
            "hashtags": self.hashtags or [],
            "media_urls": self.media_urls or [],
            "variants": variant_contents,
        }
        # Only when set, so posts approved before the CTA existed keep their hash.
        if self.cta:
            content["cta"] = self.cta
        content_json = json.dumps(content, sort_keys=True)
        return hashlib.sha256(content_json.encode()).hexdigest()

    def has_content_changed_since_approval(self) -> bool:
        """Check if content has changed since last approval (post-level).

        DEPRECATED: Phase C requires variant-level approval. Use ContentPostVariant
        methods instead. This method is retained for backward compatibility.
        """
        if not self.approved_at or not self.last_content_hash:
            return False
        return self.compute_content_hash() != self.last_content_hash

    @property
    def requires_approval(self) -> bool:
        """Approval is needed when the post asks for it or its workspace requires review.

        Read live from the workspace setting, so turning review on also covers posts that
        were already scheduled.
        """
        return self.approval_required or PublishingSettings.requires_review_for(
            self.account_id
        )

    def is_approval_valid(self, variants=None) -> bool:
        """Check if the post's approval is currently valid.

        Phase C: Approval is valid only when:
        1. approval is required (see requires_approval; if not, no approval needed)
        2. approval_status is "approved"
        3. approved_by is set (reviewer assignment required)
        4. approved_at is set
        5. last_content_hash matches current computed hash (content unchanged)

        Returns True if approval is not required (solo publishing allowed).
        Returns False if approval is required but any of the above conditions fail.
        """
        if not self.requires_approval:
            return True

        if self.approval_status != "approved":
            return False

        if not self.approved_by:
            return False

        if not self.approved_at or not self.last_content_hash:
            return False

        return self.content_hash_for(variants) == self.last_content_hash


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


class ContentPostVariant(models.Model):
    """Per-channel variant of a ContentPost with independent schedule, status, and publication outcome."""

    class Platform(models.TextChoices):
        FACEBOOK = "facebook", "Facebook"
        INSTAGRAM = "instagram", "Instagram"
        EMAIL = "email", "Email"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHING = "publishing", "Publishing"
        PROCESSING = "processing", "Processing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        RECONCILING = "reconciling", "Pending reconciliation"

    class ErrorType(models.TextChoices):
        VALIDATION = "validation", "Validation"
        PERMISSION = "permission", "Permission/Auth"
        RATE_LIMIT = "rate_limit", "Rate Limit"
        PROVIDER_OUTAGE = "provider_outage", "Provider Outage"
        TIMEOUT = "timeout", "Timeout"
        UNKNOWN = "unknown", "Unknown"

    post = models.ForeignKey(
        ContentPost, on_delete=models.CASCADE, related_name="variants"
    )
    platform = models.CharField(max_length=20, choices=Platform.choices)

    # Content (independent per platform)
    caption = models.TextField(blank=True, default="")
    media_urls = models.JSONField(default=list, blank=True)
    format_choice = models.CharField(max_length=30, blank=True, default="")
    # Channel options beyond the caption (see apps.publishing.post_options).
    options = models.JSONField(default=dict, blank=True)

    # Scheduling and publication
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # When the scheduler last enqueued this channel. A due variant is re-enqueued only
    # after a grace window, so a lost enqueue is retried without a 60 s loop.
    dispatched_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    publication_id = models.CharField(max_length=255, blank=True, default="")
    publication_url = models.URLField(max_length=2048, blank=True, default="")

    # Status lifecycle
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    # Error handling
    last_error = models.TextField(blank=True, default="")
    last_error_type = models.CharField(
        max_length=50, choices=ErrorType.choices, blank=True, default=""
    )
    last_error_at = models.DateTimeField(null=True, blank=True)

    # Retry tracking
    attempt_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    # Processing duration tracking (separate from polling attempts)
    # Set when the provider first enters PROCESSING state (e.g., IG video upload starts)
    # Used to detect genuinely overdue provider operations (not just polling health)
    processing_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the provider first entered PROCESSING state",
    )

    # Provider-specific metadata (ig_container_id, etc.)
    metadata = models.JSONField(default=dict, blank=True)

    # Performance numbers (see apps.publishing.metrics). ``metrics_latest`` mirrors the
    # newest VariantMetricSnapshot as {"values": {key: int|None}, "states": {key: state}}.
    metrics_latest = models.JSONField(default=dict, blank=True)
    metrics_collected_at = models.DateTimeField(null=True, blank=True)
    # The next planned read; None once collection is finished (or before publish).
    metrics_next_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("post", "platform")
        ordering = ["platform", "-created_at"]
        indexes = [
            models.Index(fields=["scheduled_at", "status"]),
            models.Index(fields=["post", "platform"]),
            models.Index(
                fields=["processing_started_at", "status"],
                name="publishing__processings_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.post.public_id} / {self.platform}"

    def compute_content_hash(self) -> str:
        """Compute SHA256 hash of this variant's actual publication content.

        Phase C: Approval is based on the exact content that will be published,
        including variant-specific caption, media URLs, and format choices.
        """
        import hashlib
        import json

        content = {
            "platform": self.platform,
            "caption": self.caption,
            "media_urls": self.media_urls or [],
            "format_choice": self.format_choice or "",
        }
        if self.options:
            content["options"] = self.options
        content_json = json.dumps(content, sort_keys=True)
        return hashlib.sha256(content_json.encode()).hexdigest()

    def variant_approval_valid(self) -> bool:
        """Check if the variant's approval (on parent post) is still valid.

        Approval is valid if:
        1. Parent post approval_status is "approved"
        2. Parent post has a reviewer
        3. No content in ANY variant has changed since the post was approved

        Returns False if content changed after approval (prevents silent unapproved publishing).
        Note: Changing ANY variant invalidates the entire post's approval.
        """
        post = self.post
        if post.approval_status != "approved":
            return False
        if not post.approved_by:
            return False
        if not post.approved_at or not post.last_content_hash:
            return False

        # Check if the post's aggregate content matches what was approved
        # If any variant changed, the post's hash will differ from the approved hash
        return post.compute_content_hash() == post.last_content_hash


class VariantMetricSnapshot(models.Model):
    """One read of a published channel's performance numbers. Append-only history.

    ``values`` holds a number or None per metric key; ``states`` says why a value is
    None (see ``apps.publishing.metrics.State``). None is never shown as 0.
    """

    variant = models.ForeignKey(
        ContentPostVariant, on_delete=models.CASCADE, related_name="metric_snapshots"
    )
    collected_at = models.DateTimeField(db_index=True)
    values = models.JSONField(default=dict)
    states = models.JSONField(default=dict)
    error_type = models.CharField(max_length=50, blank=True, default="")
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-collected_at"]

    def __str__(self) -> str:
        return f"{self.variant} @ {self.collected_at:%Y-%m-%d %H:%M}"


class TrackedLink(models.Model):
    """A short link for one channel of a post that opens a WhatsApp chat with the business.

    ``/go/<code>`` counts the click and opens WhatsApp with ``prefill_text`` plus
    "(ref CODE)". When the customer sends that message, the conversation is linked to
    this post and channel (apps.conversations.entry_points), and any lead or order from
    it carries the post as its source. Only chats that used the link are counted.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="tracked_links"
    )
    post = models.ForeignKey(
        ContentPost, on_delete=models.CASCADE, related_name="tracked_links"
    )
    variant = models.OneToOneField(
        ContentPostVariant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracked_link",
    )
    campaign = models.ForeignKey(
        ContentCampaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracked_links",
    )
    code = models.CharField(max_length=12, unique=True)
    wa_number = models.CharField(max_length=20)
    prefill_text = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    disabled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.code} → {self.variant or self.post}"


class LinkClick(models.Model):
    """One open of a tracked link. No IP is stored; ``visitor_hash`` is a salted,
    daily-rotating hash used only to count distinct visitors."""

    link = models.ForeignKey(
        TrackedLink, on_delete=models.CASCADE, related_name="clicks"
    )
    clicked_at = models.DateTimeField(auto_now_add=True, db_index=True)
    visitor_hash = models.CharField(max_length=64, blank=True, default="")
    referer_host = models.CharField(max_length=255, blank=True, default="")
    is_bot = models.BooleanField(default=False)


class ApprovalLog(models.Model):
    """Audit trail for content approval actions."""

    class Action(models.TextChoices):
        SUBMITTED = "submitted", "Submitted for Review"
        APPROVED = "approved", "Approved"
        CHANGES_REQUESTED = "changes_requested", "Changes Requested"
        RESUBMITTED = "resubmitted", "Resubmitted After Changes"

    post = models.ForeignKey(
        ContentPost, on_delete=models.CASCADE, related_name="approval_logs"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approval_actions",
    )
    feedback = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.post.public_id} / {self.action} / {self.created_at}"


class ReconciliationLog(models.Model):
    """Audit trail for variant reconciliation actions."""

    class Action(models.TextChoices):
        CONFIRMED_PUBLISHED = "confirmed_published", "Confirmed Published"
        RETRY = "retry", "Retry Publication"
        LEFT_UNRESOLVED = "left_unresolved", "Left Unresolved"

    variant = models.ForeignKey(
        ContentPostVariant,
        on_delete=models.CASCADE,
        related_name="reconciliation_logs",
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reconciliation_actions",
    )

    # For confirmed_published action
    provider_publication_id = models.CharField(max_length=255, blank=True, default="")
    provider_publication_url = models.URLField(max_length=2048, blank=True, default="")
    evidence_notes = models.TextField(blank=True, default="")

    # For left_unresolved action
    next_investigative_step = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["variant", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.variant.post.public_id}/{self.variant.platform} - {self.action}"


class FacebookPageConnection(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISCONNECTED = "disconnected", "Disconnected"
        # Meta rejected the stored token (revoked, password change); reconnect needed.
        EXPIRED = "expired", "Reconnect needed"

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


class PublishingSettings(models.Model):
    """Per-workspace Content Studio settings."""

    account = models.OneToOneField(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="publishing_settings",
    )
    # When on, every post needs an Owner/Admin approval of its current content before it
    # can be scheduled or published. Off by default so solo creators publish directly.
    requires_review = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.account} publishing settings"

    @classmethod
    def requires_review_for(cls, account) -> bool:
        return cls.objects.filter(account=account, requires_review=True).exists()
