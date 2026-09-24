from django.apps import AppConfig


class ContactsConfig(AppConfig):
    name = "apps.contacts"
    label = "contacts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import for its module-level ``register(...)`` calls into the shared Action Registry.
        from apps.contacts import actions  # noqa: F401
