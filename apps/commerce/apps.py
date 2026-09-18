from django.apps import AppConfig


class CommerceConfig(AppConfig):
    name = "apps.commerce"
    label = "commerce"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import for its module-level ``register(...)`` calls into the
        # shared Action Registry (apps.core.actions).
        from apps.commerce import actions  # noqa: F401
