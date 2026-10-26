from django.db import models

from .contact import FacebookContact


class FacebookCommentThread(models.Model):
    """
    Root comment on a post on the business's Facebook Page.

    Like Instagram's CommentThread, a thread is a passive engagement record. It
    does not become a conversations.Conversation by itself: only a private reply
    (a CommentTrigger, or a person in the Comments view) opens a Messenger thread,
    which is then linked here through ``conversation``.
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="facebook_comment_threads",
    )
    page_id = models.CharField(max_length=64)
    # The Page post id, "{page_id}_{post_id}".
    post_id = models.CharField(max_length=100, db_index=True)
    facebook_contact = models.ForeignKey(
        FacebookContact,
        on_delete=models.CASCADE,
        related_name="comment_threads",
    )
    # Root comment's external id, "{post_id}_{comment_id}".
    comment_id = models.CharField(max_length=100)
    body = models.TextField(blank=True, default="")
    is_hidden = models.BooleanField(default=False)

    # Matched buying-intent phrase from detect_buying_intent(); empty when none
    intent = models.CharField(max_length=255, blank=True, default="")

    # Set after a private reply opens a Messenger thread — that thread's spine conversation
    conversation = models.ForeignKey(
        "conversations.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="facebook_comment_threads",
    )

    # The Content Studio channel that published this post, when Akilent published it.
    content_variant = models.ForeignKey(
        "publishing.ContentPostVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="facebook_comment_threads",
    )

    # Set when a CommentTrigger fires: the idempotency anchor, so repeated webhook
    # deliveries never send a second private reply.
    trigger_fired_at = models.DateTimeField(null=True, blank=True)

    received_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "comment_id"],
                name="unique_facebook_comment_thread_per_account",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "post_id"]),
            models.Index(fields=["intent", "received_at"]),
        ]

    def __str__(self):
        return f"Facebook comment {self.comment_id}: {self.body[:60]!r}"


class FacebookComment(models.Model):
    """An individual comment or reply within a FacebookCommentThread."""

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class ModerationState(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        FLAGGED = "flagged", "Flagged for review"
        HIDDEN = "hidden", "Hidden"
        DELETED = "deleted", "Deleted"

    thread = models.ForeignKey(
        FacebookCommentThread, on_delete=models.CASCADE, related_name="comments"
    )
    comment_id = models.CharField(max_length=100)
    # Parent comment id for nested replies; empty for top-level comments
    parent_comment_id = models.CharField(max_length=100, blank=True, default="")
    facebook_contact = models.ForeignKey(
        FacebookContact,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    body = models.TextField(blank=True, default="")
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
                name="unique_facebook_comment_per_thread",
            ),
        ]

    def __str__(self):
        return f"Facebook comment {self.comment_id} ({self.direction})"
