from django.db import models


class MessageTemplateAsset(models.Model):
    """A file uploaded for use as a template's header media (image/video/document).

    Separate from MessageTemplate so the upload endpoint can create one, get
    Meta's app-scoped handle back, and hand the builder UI a preview — all
    before the surrounding template form is submitted.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="whatsapp_template_assets"
    )
    file = models.FileField(upload_to="whatsapp_headers/%Y/%m/")
    content_type = models.CharField(max_length=100, blank=True, default="")
    meta_handle = models.CharField(max_length=512, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.file.name


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

    class HeaderFormat(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"

    header_format = models.CharField(max_length=10, choices=HeaderFormat.choices, default=HeaderFormat.TEXT)
    header = models.CharField(max_length=60, blank=True, default="")  # header_format == TEXT only
    header_media = models.ForeignKey(
        MessageTemplateAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
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
