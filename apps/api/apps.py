from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "apps.api"
    label = "api"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from apps.api import schema  # noqa: F401  (registers OpenAPI extensions)
