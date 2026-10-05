from django.conf import settings
from django.db import models


class ModerationRule(models.Model):
    """
    A per-account rule that evaluates inbound comments and applies a moderation
    action or triggers a downstream automation.

    Two categories of action are kept deliberately separate:

    Moderation actions  — affect the Instagram content itself:
        hide, delete, flag

    Automation triggers — affect Akilent's internal state:
        notify_staff, create_proposal

    A single rule has one moderation_action and one automation_trigger (either or
    both may be set, but they serve different concerns).
    """

    class MatchType(models.TextChoices):
        SPAM_DETECTION = "spam_detection", "Spam detection (heuristic)"
        TOXICITY = "toxicity", "Toxicity detection (heuristic)"
        KEYWORD = "keyword", "Keyword / phrase match"
        BUYING_INTENT = "buying_intent", "Buying intent"
        COMPLAINT = "complaint", "Complaint language"
        MENTION = "mention", "Mention of the account"

    class ModerationAction(models.TextChoices):
        NONE = "none", "No moderation action"
        HIDE = "hide", "Hide comment"
        DELETE = "delete", "Delete comment"
        FLAG = "flag", "Flag for staff review"

    class AutomationTrigger(models.TextChoices):
        NONE = "none", "No automation"
        NOTIFY_STAFF = "notify_staff", "Notify assigned staff / team"
        CREATE_PROPOSAL = "create_proposal", "Create purchase intent proposal"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="instagram_moderation_rules",
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    match_type = models.CharField(max_length=20, choices=MatchType.choices)
    # For KEYWORD match type: comma-separated phrases to match (case-insensitive)
    keywords = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated keywords/phrases for KEYWORD match type.",
    )

    moderation_action = models.CharField(
        max_length=10,
        choices=ModerationAction.choices,
        default=ModerationAction.NONE,
    )
    automation_trigger = models.CharField(
        max_length=20,
        choices=AutomationTrigger.choices,
        default=AutomationTrigger.NONE,
    )

    # Priority: lower number = evaluated first
    priority = models.PositiveSmallIntegerField(default=10)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "created_at"]
        indexes = [
            models.Index(fields=["account", "is_active", "priority"]),
        ]

    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def __str__(self):
        return f"{self.name} ({self.match_type} → {self.moderation_action})"


class ModerationLog(models.Model):
    """
    Immutable audit record of every moderation action taken on a comment.
    """

    class Outcome(models.TextChoices):
        APPLIED = "applied", "Applied"
        SKIPPED = "skipped", "Skipped (already in state)"
        FAILED = "failed", "Failed (API error)"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="moderation_logs"
    )
    rule = models.ForeignKey(
        ModerationRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logs",
    )
    comment = models.ForeignKey(
        "instagram.Comment",
        on_delete=models.CASCADE,
        related_name="moderation_logs",
    )

    moderation_action = models.CharField(max_length=10)
    automation_trigger = models.CharField(max_length=20, blank=True, default="")
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    error = models.TextField(blank=True, default="")

    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Set when a staff member manually triggered this action.",
    )

    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["account", "occurred_at"]),
            models.Index(fields=["comment", "occurred_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("ModerationLog rows are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("ModerationLog rows are immutable.")

    def __str__(self):
        return f"ModerationLog: {self.moderation_action} on comment {self.comment_id} ({self.outcome})"
