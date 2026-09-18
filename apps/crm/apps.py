from django.apps import AppConfig


class CrmConfig(AppConfig):
    name = "apps.crm"
    label = "crm"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import for its module-level ``register(...)`` calls into the
        # shared Action Registry (apps.core.actions).
        from apps.crm import actions  # noqa: F401
