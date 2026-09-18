from django.apps import AppConfig


class ConversationsConfig(AppConfig):
    name = "apps.conversations"
    label = "conversations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import for its module-level ``register(...)`` calls into the
        # shared Action Registry (apps.core.actions) — must run at startup
        # regardless of whether any view happens to import this module.
        from apps.conversations import actions  # noqa: F401
