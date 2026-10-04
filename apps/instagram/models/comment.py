from django.db import models

from .contact import InstagramContact


class CommentThread(models.Model):
    """
    Root comment on an Instagram post, reel, or story.

    A CommentThread is a passive engagement record. It does NOT become a
    conversations.Conversation by itself. Under the DM-first rule, only a
    private reply (triggered by buying intent or a CommentTrigger) spawns a
    DM, and that DM conversation is linked here via ``conversation``.
    """

    class PostType(models.TextChoices):
        POST = "post", "Post"
        REEL = "reel", "Reel"
        STORY = "story", "Story"
        LIVE = "live", "Live"
        AD = "ad", "Ad"

    instagram_account = models.ForeignKey(
        "instagram.InstagramBusinessAccount",
        on_delete=models.CASCADE,
        related_name="comment_threads",
    )
    post_id = models.CharField(max_length=50, db_index=True)
    post_type = models.CharField(
        max_length=10, choices=PostType.choices, default=PostType.POST
    )
    instagram_contact = models.ForeignKey(
        InstagramContact,
        on_delete=models.CASCADE,
        related_name="comment_threads",
    )
    # Root comment's external ID from Instagram
    comment_id = models.CharField(max_length=50)
    body = models.TextField()
    is_hidden = models.BooleanField(default=False)

    # Matched buying-intent phrase from detect_buying_intent(); empty when none
    intent = models.CharField(max_length=255, blank=True, default="")

    # Set after a private reply spawns a DM — the spine conversation for that DM
    conversation = models.ForeignKey(
        "conversations.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="instagram_comment_threads",
    )

    received_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["instagram_account", "comment_id"],
                name="unique_comment_thread_per_account",
            ),
        ]
        indexes = [
            models.Index(fields=["instagram_account", "post_id"]),
            models.Index(fields=["intent", "received_at"]),
        ]

    def __str__(self):
        snippet = self.body[:60]
        return f"Comment {self.comment_id}: {snippet!r}"


class Comment(models.Model):
    """
    An individual comment or reply within a CommentThread.
    """

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class ModerationState(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        HIDDEN = "hidden", "Hidden"
        DELETED = "deleted", "Deleted"

    thread = models.ForeignKey(
        CommentThread, on_delete=models.CASCADE, related_name="comments"
    )
    # Individual comment's external ID from Instagram
    comment_id = models.CharField(max_length=50)
    # Parent comment ID for nested replies; null for top-level comments
    parent_comment_id = models.CharField(max_length=50, blank=True, default="")
    instagram_contact = models.ForeignKey(
        InstagramContact,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    body = models.TextField()
    direction = models.CharField(
        max_length=10, choices=Direction.choices, default=Direction.INBOUND
    )
    is_hidden = models.BooleanField(default=False)
    moderation_state = models.CharField(
        max_length=10,
        choices=ModerationState.choices,
        default=ModerationState.PENDING,
    )
    timestamp = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "comment_id"],
                name="unique_comment_per_thread",
            ),
        ]

    def __str__(self):
        return f"Comment {self.comment_id} ({self.direction})"
