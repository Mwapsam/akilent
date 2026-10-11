from django.apps import AppConfig


class HubSpotConfig(AppConfig):
    name = "apps.hubspot"
    default_auto_field = "django.db.models.BigAutoField"
    label = "hubspot"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.hubspot.signals import on_contact_saved

        post_save.connect(on_contact_saved, sender="contacts.Contact")
