from django.apps import AppConfig


class InternalDebugConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.internal_debug"
    label = "internal_debug"
