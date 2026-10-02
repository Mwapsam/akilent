from django.db import models


class SupportQueue(models.Model):
    """Named inbox that tickets are routed into.

    Routing is driven by ticket category/severity; a queue maps to a team
    or specialisation (Payments, WhatsApp, General Support, Security …).
    """

    GENERAL = "general"
    ACCOUNT = "account"
    ONBOARDING = "onboarding"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    CONVERSATIONS = "conversations"
    CRM = "crm"
    PAYMENTS = "payments"
    BILLING = "billing"
    INTEGRATIONS = "integrations"
    TECHNICAL = "technical"
    SECURITY = "security"

    SLUG_CHOICES = [
        (GENERAL, "General Support"),
        (ACCOUNT, "Account & Access"),
        (ONBOARDING, "Onboarding"),
        (WHATSAPP, "WhatsApp"),
        (EMAIL, "Email"),
        (CONVERSATIONS, "Conversations"),
        (CRM, "CRM"),
        (PAYMENTS, "Payments"),
        (BILLING, "Billing"),
        (INTEGRATIONS, "Integrations"),
        (TECHNICAL, "Technical"),
        (SECURITY, "Security"),
    ]

    slug = models.SlugField(max_length=60, unique=True, choices=SLUG_CHOICES)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    # Restrict visibility of tickets in this queue to members of these groups.
    restricted = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name
