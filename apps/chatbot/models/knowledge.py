from django.db import models


class ChatbotKnowledgeSource(models.Model):
    chatbot = models.ForeignKey(
        "chatbot.ChatbotConfig",
        on_delete=models.CASCADE,
        related_name="knowledge_sources",
    )
    knowledge_entry = models.ForeignKey(
        "ai.KnowledgeBaseEntry",
        on_delete=models.CASCADE,
        related_name="chatbot_sources",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Chatbot knowledge source"
        verbose_name_plural = "Chatbot knowledge sources"
        constraints = [
            models.UniqueConstraint(
                fields=["chatbot", "knowledge_entry"],
                name="unique_chatbot_knowledge_source",
            )
        ]

    def __str__(self) -> str:
        return f"{self.chatbot} — {self.knowledge_entry}"
