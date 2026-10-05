import secrets

from django.core.exceptions import ValidationError
from django.db import models


def _default_public_key() -> str:
    return "pk_chat_" + secrets.token_hex(16)


class ChatbotConfig(models.Model):
    class Purpose(models.TextChoices):
        SUPPORT = "support", "Support"
        SALES = "sales", "Sales"
        GENERAL = "general", "General assistant"
        CUSTOM = "custom", "Custom"

    class ChatbotType(models.TextChoices):
        SYSTEM = "system", "System"
        CUSTOMER = "customer", "Customer"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="chatbots",
    )
    public_key = models.CharField(
        max_length=64,
        unique=True,
        default=_default_public_key,
        editable=False,
    )
    name = models.CharField(max_length=100)
    purpose = models.CharField(
        max_length=20, choices=Purpose.choices, default=Purpose.GENERAL
    )
    avatar_url = models.URLField(blank=True)
    welcome_message = models.TextField(blank=True)
    primary_color = models.CharField(max_length=7, default="#1a56db")
    position = models.CharField(
        max_length=20,
        choices=[("bottom_right", "Bottom right"), ("bottom_left", "Bottom left")],
        default="bottom_right",
    )
    # Empty list = no origins authorized (not "allow all").
    # Development mode is a separate flag, not an empty list.
    allowed_domains = models.JSONField(default=list, blank=True)
    # The action slug to invoke on handoff — configured per chatbot.
    # e.g. "create_support_ticket" for support bots, "create_sales_lead" for sales bots.
    handoff_action = models.ForeignKey(
        "chatbot.ChatbotAction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    category = models.ForeignKey(
        "chatbot.ChatbotCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chatbots",
    )
    chatbot_type = models.CharField(
        max_length=20,
        choices=ChatbotType.choices,
        default=ChatbotType.CUSTOMER,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Chatbot config"
        verbose_name_plural = "Chatbot configs"
        ordering = ["-created_at"]

    def clean(self):
        super().clean()
        if not self.account_id:
            # No account yet — reject SYSTEM type explicitly rather than
            # silently passing and letting the DB NOT NULL constraint fire.
            if self.chatbot_type == self.ChatbotType.SYSTEM:
                raise ValidationError(
                    "System chatbots must be assigned to the platform account."
                )
            return
        is_platform = self.account.is_platform_account
        if self.chatbot_type == self.ChatbotType.SYSTEM and not is_platform:
            raise ValidationError(
                "System chatbots must belong to the platform account."
            )
        if self.chatbot_type == self.ChatbotType.CUSTOMER and is_platform:
            raise ValidationError(
                "Customer chatbots cannot belong to the platform account."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.account})"
