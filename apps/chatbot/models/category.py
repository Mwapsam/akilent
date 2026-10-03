from django.db import models
from django.utils.text import slugify


class ChatbotCategory(models.Model):
    """Admin-defined categories for grouping chatbots across businesses.

    These are operator-managed (not business-owner-managed) and exist
    platform-wide. Examples: "E-commerce", "SaaS", "Healthcare".
    """

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(
        default=0, help_text="Lower = appears first."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Chatbot category"
        verbose_name_plural = "Chatbot categories"
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)
