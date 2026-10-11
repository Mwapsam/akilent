"""Content publishing models.

ContentPost — a piece of content destined for Instagram feed/reels/stories
and/or a Facebook Page. The post moves through: draft → scheduled → publishing
→ published | failed.

FacebookPageConnection — per-account OAuth token for a connected Facebook Page.
"""

from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.db import models

from apps.accounts.fields import EncryptedTextField


def _post_id() -> str:
    return "post_" + secrets.token_hex(16)


def _fb_conn_id() -> str:
    return "fbconn_" + secrets.token_hex(12)


class ContentPost(models.Model):
    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        REEL = "reel", "Reel"
        STORY = "story", "Story"
        TEXT = "text", "Text (Facebook only)"
        CAROUSEL = "carousel", "Carousel"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"

    public_id = models.CharField(
        max_length=45, unique=True, default=_post_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="content_posts"
    )
    platforms = models.JSONField(default=list)  # ["instagram", "facebook"]
    media_type = models.CharField(max_length=10, choices=MediaType.choices)
    caption = models.TextField(blank=True, default="")
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


class ContentCaption(models.Model):
    """AI-generated caption drafts for a ContentPost."""

    post = models.ForeignKey(
        ContentPost, on_delete=models.CASCADE, related_name="captions"
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
