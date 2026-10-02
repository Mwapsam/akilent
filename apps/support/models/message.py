from django.db import models


class SupportMessage(models.Model):
    """A public message on a support ticket (visible to the customer)."""

    ticket = models.ForeignKey(
        "support.SupportTicket", on_delete=models.CASCADE, related_name="messages"
    )
    author = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_messages",
    )
    # Null author means the message came from the customer via email/portal.
    is_from_customer = models.BooleanField(default=False)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"[{self.ticket.ticket_number}] message #{self.pk}"


class SupportAttachment(models.Model):
    """File attachment on a message or directly on the ticket."""

    ticket = models.ForeignKey(
        "support.SupportTicket", on_delete=models.CASCADE, related_name="attachments"
    )
    message = models.ForeignKey(
        SupportMessage,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="support/attachments/%Y/%m/")
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.filename


class SupportInternalNote(models.Model):
    """Agent-only note on a ticket (never shown to the customer)."""

    ticket = models.ForeignKey(
        "support.SupportTicket", on_delete=models.CASCADE, related_name="internal_notes"
    )
    author = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_internal_notes",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"[{self.ticket.ticket_number}] internal note #{self.pk}"
