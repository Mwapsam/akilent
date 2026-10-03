import secrets

from django.db import models


def _default_session_key() -> str:
    return "sess_" + secrets.token_hex(16)


class ChatSession(models.Model):
    chatbot = models.ForeignKey(
        "chatbot.ChatbotConfig",
        on_delete=models.CASCADE,
        related_name="sessions",
    )
    session_key = models.CharField(
        max_length=64,
        unique=True,
        default=_default_session_key,
        editable=False,
    )
    # Set once the first message is sent and a Conversation is created.
    conversation = models.OneToOneField(
        "conversations.Conversation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_session",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_sessions",
    )
    visitor_name = models.CharField(max_length=200, blank=True)
    visitor_email = models.EmailField(blank=True)
    visitor_phone = models.CharField(max_length=30, blank=True)
    identified_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    last_activity_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Chat session"
        verbose_name_plural = "Chat sessions"
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return self.session_key
