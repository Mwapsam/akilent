from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import for its module-level ``register(...)`` calls into the
        # shared Action Registry (apps.core.actions).
        from apps.accounts import actions  # noqa: F401
