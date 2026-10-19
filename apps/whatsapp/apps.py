from django.apps import AppConfig


class WhatsappConfig(AppConfig):
    name = "apps.whatsapp"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.contacts.models import Contact
        from apps.whatsapp.signals import on_contact_saved

        post_save.connect(
            on_contact_saved, sender=Contact, dispatch_uid="whatsapp_contact_identity"
        )
