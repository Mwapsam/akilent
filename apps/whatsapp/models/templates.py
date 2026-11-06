from django.db import models


class MessageTemplateAsset(models.Model):
    """A file uploaded for use as a template's header media (image/video/document).

    Separate from MessageTemplate so the upload endpoint can create one, get
    Meta's app-scoped handle back, and hand the builder UI a preview — all
    before the surrounding template form is submitted.
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="whatsapp_template_assets",
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
        # Deleted at Meta from Akilent. The row stays so past campaigns still show what was
        # sent; it is hidden from every list and picker (template_builder.delete_template).
        DELETED = "deleted", "Deleted"

    class Category(models.TextChoices):
        MARKETING = "marketing", "Marketing"
        UTILITY = "utility", "Utility"
        AUTHENTICATION = "authentication", "Authentication"

    class OtpButtonType(models.TextChoices):
        URL = "url", "URL / autofill"
        COPY_CODE = "copy_code", "Copy code"

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
    # Meta's id for this template (one language). Editing goes through it, and deleting one
    # language needs it. Stored on create and filled in by sync for older rows.
    meta_template_id = models.CharField(max_length=64, blank=True, default="")
    # Why Meta rejected or paused it, in words an owner can act on (tasks.REJECTION_REASONS).
    rejection_reason = models.CharField(max_length=255, blank=True, default="")

    content = models.TextField()
    variables = models.JSONField(default=list)  # ["name", "company"]
    variable_examples = models.JSONField(
        default=list, blank=True
    )  # example values, same order as `variables`

    class HeaderFormat(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"

    header_format = models.CharField(
        max_length=10, choices=HeaderFormat.choices, default=HeaderFormat.TEXT
    )
    header = models.CharField(
        max_length=60, blank=True, default=""
    )  # header_format == TEXT only
    header_media = models.ForeignKey(
        MessageTemplateAsset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    footer = models.CharField(max_length=60, blank=True, default="")
    buttons = models.JSONField(default=list, blank=True)  # Meta BUTTONS component shape
    otp_button_type = models.CharField(
        max_length=10,
        choices=OtpButtonType.choices,
        default=OtpButtonType.URL,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        unique_together = ("account", "whatsapp_template_name", "language_code")

    @property
    def sendable_outside_window(self) -> bool:
        return self.approval_status == self.ApprovalStatus.APPROVED

    @property
    def label(self) -> str:
        """The name as people read it ("Download app" for ``download_app``); Meta's name is
        unchanged and is what's sent."""
        words = " ".join(self.name.replace("_", " ").replace("-", " ").split())
        return words[:1].upper() + words[1:]

    def fields(self) -> list[dict]:
        """Each parameter as the picker asks for it: ``{{1}}`` has no name of its own, so it's
        "Value 1", with Meta's example as the hint."""
        examples = list(self.variable_examples or [])
        out = []
        for i, v in enumerate(self.variables or []):
            name = str(v)
            out.append(
                {
                    "name": name,
                    "label": f"Value {name}"
                    if name.isdigit()
                    else name.replace("_", " ").capitalize(),
                    "example": str(examples[i])
                    if i < len(examples) and examples[i]
                    else "",
                }
            )
        return out

    def __str__(self):
        return f"{self.name} [{self.approval_status}]"
