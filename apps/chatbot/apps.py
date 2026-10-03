from django.apps import AppConfig


class ChatbotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.chatbot"
    verbose_name = "Chatbot"

    def ready(self) -> None:
        import apps.chatbot.signals  # noqa: F401
