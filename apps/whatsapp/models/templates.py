from django.db import models


class MessageTemplate(models.Model):

    class ApprovalStatus(models.TextChoices):
        DRAFT = "draft", "Draft (local only)"
        PENDING = "pending", "Pending Meta approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAUSED = "paused", "Paused by Meta"

    class Category(models.TextChoices):
        MARKETING = "marketing", "Marketing"
        UTILITY = "utility", "Utility"
        AUTHENTICATION = "authentication", "Authentication"

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    whatsapp_template_name = models.CharField(max_length=255, blank=True, null=True)
    language_code = models.CharField(max_length=10, default="en")

    category = models.CharField(
        max_length=20, choices=Category.choices, default=Category.UTILITY
    )
    approval_status = models.CharField(
        max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.DRAFT
    )

    content = models.TextField()
    variables = models.JSONField(default=list)  # ["name", "company"]
    variable_examples = models.JSONField(default=list, blank=True)  # example values, same order as `variables`

    header = models.CharField(max_length=60, blank=True, default="")
    footer = models.CharField(max_length=60, blank=True, default="")
    buttons = models.JSONField(default=list, blank=True)  # Meta BUTTONS component shape

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("account", "whatsapp_template_name", "language_code")

    @property
    def sendable_outside_window(self) -> bool:
        return self.approval_status == self.ApprovalStatus.APPROVED

    def __str__(self):
        return f"{self.name} [{self.approval_status}]"
