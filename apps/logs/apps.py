from django.apps import AppConfig


class LogsConfig(AppConfig):
    name = "apps.logs"
    label = "logs"
    verbose_name = "Observability logs"
    default_auto_field = "django.db.models.BigAutoField"
