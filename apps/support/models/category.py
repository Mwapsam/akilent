from django.db import models


class SupportCategory(models.Model):
    """Two-level taxonomy: category + optional subcategory.

    Seeded categories map to the product taxonomy (Account, Channels,
    Conversations, CRM, Payments, Billing, Integrations, Technical …).
    Subcategories are children of a category row (parent=None).
    """

    ACCOUNT = "account"
    ORGANIZATION = "organization"
    ONBOARDING = "onboarding"
    CHANNELS = "channels"
    CONVERSATIONS = "conversations"
    CRM = "crm"
    AUTOMATION = "automation"
    PAYMENTS = "payments"
    BILLING = "billing"
    INTEGRATIONS = "integrations"
    TECHNICAL = "technical"
    SECURITY = "security"
    GENERAL = "general"

    SLUG_CHOICES = [
        (ACCOUNT, "Account"),
        (ORGANIZATION, "Organization"),
        (ONBOARDING, "Onboarding"),
        (CHANNELS, "Channels"),
        (CONVERSATIONS, "Conversations"),
        (CRM, "CRM"),
        (AUTOMATION, "Automation"),
        (PAYMENTS, "Payments"),
        (BILLING, "Billing"),
        (INTEGRATIONS, "Integrations"),
        (TECHNICAL, "Technical"),
        (SECURITY, "Security"),
        (GENERAL, "General"),
    ]

    slug = models.SlugField(max_length=60, choices=SLUG_CHOICES)
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="subcategories",
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "Support Category"
        verbose_name_plural = "Support Categories"
        ordering = ["sort_order", "name"]
        unique_together = [("slug", "parent")]

    def __str__(self) -> str:
        if self.parent:
            return f"{self.parent.name} / {self.name}"
        return self.name

    @property
    def is_subcategory(self) -> bool:
        return self.parent_id is not None
