"""Audience signals: what each comment says, kept as append-only history (spec §4).

* ``AudienceSignalRun``: one classification of one comment by one classifier version
  and source. A new version or an edited comment adds a run; nothing is overwritten.
* ``AudienceSignal``: one category/subcategory inside a run. No comment text is copied.
* ``AudienceSignalCorrection``: an owner's add/reject on a comment. It belongs to the
  comment, not a run, so it survives reclassification; it stops applying when the
  comment text is edited (its ``text_hash`` no longer matches).

Explicit Instagram/Facebook comment FKs (CASCADE) rather than a generic FK, so erasing
a customer or deleting a comment removes its signals with it.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from .taxonomy import Category

_EXACTLY_ONE_COMMENT = Q(ig_comment__isnull=False, fb_comment__isnull=True) | Q(
    ig_comment__isnull=True, fb_comment__isnull=False
)


class Channel(models.TextChoices):
    INSTAGRAM = "instagram", "Instagram"
    FACEBOOK = "facebook", "Facebook"


class AudienceSignalRun(models.Model):
    class Source(models.TextChoices):
        RULE = "rule", "Rules"
        AI = "ai", "AI"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="audience_runs"
    )
    channel = models.CharField(max_length=12, choices=Channel.choices)
    ig_comment = models.ForeignKey(
        "instagram.Comment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audience_runs",
    )
    fb_comment = models.ForeignKey(
        "facebook.FacebookComment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audience_runs",
    )
    source = models.CharField(max_length=8, choices=Source.choices)
    classifier_version = models.CharField(max_length=40)
    language = models.CharField(max_length=12, blank=True, default="")
    # SHA-256 of the comment text that was classified; an edit makes a new run.
    text_hash = models.CharField(max_length=64)
    # When the comment was made: the reporting window, not when we classified it.
    occurred_at = models.DateTimeField()
    # Diagnostics only (e.g. AI/rule conflicts). Never shown to owners.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=_EXACTLY_ONE_COMMENT, name="audience_run_exactly_one_comment"
            ),
            models.UniqueConstraint(
                fields=["ig_comment", "source", "classifier_version", "text_hash"],
                condition=Q(ig_comment__isnull=False),
                name="audience_run_unique_ig",
            ),
            models.UniqueConstraint(
                fields=["fb_comment", "source", "classifier_version", "text_hash"],
                condition=Q(fb_comment__isnull=False),
                name="audience_run_unique_fb",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "occurred_at"]),
            models.Index(fields=["account", "channel", "occurred_at"]),
        ]

    def __str__(self):
        return f"{self.channel} run {self.pk} ({self.source} {self.classifier_version})"


class AudienceSignal(models.Model):
    run = models.ForeignKey(
        AudienceSignalRun, on_delete=models.CASCADE, related_name="signals"
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    subcategory = models.CharField(max_length=20, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    # The listed phrases that matched (Akilent's own terms, never free comment text).
    matched_terms = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run", "category", "subcategory"],
                name="audience_signal_unique_per_run",
            ),
        ]
        indexes = [models.Index(fields=["category", "subcategory"])]

    def __str__(self):
        return f"{self.category}/{self.subcategory}"


class AudienceSignalCorrection(models.Model):
    class Action(models.TextChoices):
        ADD = "add", "Add"
        REJECT = "reject", "Reject"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="audience_corrections",
    )
    ig_comment = models.ForeignKey(
        "instagram.Comment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audience_corrections",
    )
    fb_comment = models.ForeignKey(
        "facebook.FacebookComment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audience_corrections",
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    subcategory = models.CharField(max_length=20, blank=True, default="")
    action = models.CharField(max_length=8, choices=Action.choices)
    # The text the owner corrected; an edited comment no longer matches it.
    text_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=_EXACTLY_ONE_COMMENT,
                name="audience_correction_exactly_one_comment",
            ),
            models.UniqueConstraint(
                fields=["ig_comment", "category", "subcategory"],
                condition=Q(ig_comment__isnull=False),
                name="audience_correction_unique_ig",
            ),
            models.UniqueConstraint(
                fields=["fb_comment", "category", "subcategory"],
                condition=Q(fb_comment__isnull=False),
                name="audience_correction_unique_fb",
            ),
        ]

    def __str__(self):
        return f"{self.action} {self.category}/{self.subcategory}"
