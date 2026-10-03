from django.db import models


class ChatbotAction(models.Model):
    chatbot = models.ForeignKey(
        "chatbot.ChatbotConfig",
        on_delete=models.CASCADE,
        related_name="actions",
    )
    slug = models.SlugField(max_length=80)
    label = models.CharField(max_length=200)
    description = models.TextField(
        blank=True,
        help_text="Shown to the LLM as the tool description.",
    )
    is_enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Chatbot action"
        verbose_name_plural = "Chatbot actions"
        constraints = [
            models.UniqueConstraint(
                fields=["chatbot", "slug"],
                name="unique_chatbot_action",
            )
        ]

    def __str__(self) -> str:
        return f"{self.chatbot} / {self.slug}"


class ChatActionExecution(models.Model):
    class Status(models.TextChoices):
        AUTHORIZED = "authorized", "Authorized"
        DENIED = "denied", "Denied"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    session = models.ForeignKey(
        "chatbot.ChatSession",
        on_delete=models.CASCADE,
        related_name="action_executions",
    )
    action = models.ForeignKey(
        "chatbot.ChatbotAction",
        on_delete=models.CASCADE,
        related_name="executions",
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    # Sanitised inputs — never raw browser or LLM payloads.
    input_metadata = models.JSONField(default=dict)
    # Sanitised result — never raw model output or PII.
    output_metadata = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Chat action execution"
        verbose_name_plural = "Chat action executions"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action.slug} [{self.status}]"
