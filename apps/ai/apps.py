from django.apps import AppConfig


class AIConfig(AppConfig):
    name = "apps.ai"
    label = "ai"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # AI is one more consumer of conversation events; conversations never imports apps.ai.
        from apps.ai.api import on_message_processed
        from apps.conversations.signals import conversation_message_processed

        conversation_message_processed.connect(on_message_processed, dispatch_uid="ai-proposals")
