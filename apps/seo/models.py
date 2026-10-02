from django.db import models


class SEOPage(models.Model):
    path = models.CharField(
        max_length=255, unique=True, help_text="URL path, e.g. /features/whatsapp/"
    )
    title = models.CharField(max_length=70, blank=True)
    description = models.CharField(max_length=160, blank=True)
    canonical_url = models.URLField(
        blank=True,
        help_text="Override only for intentional canonicalization (duplicate/alternate URLs). Leave blank to auto-generate from the request path.",
    )
    og_image = models.ImageField(upload_to="seo/", blank=True)
    noindex = models.BooleanField(default=False)
    sitemap = models.BooleanField(
        default=True,
        help_text="Include this URL in the XML sitemap. Automatically set to False when noindex is True.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "SEO page"
        verbose_name_plural = "SEO pages"
        ordering = ["path"]

    def __str__(self):
        return self.path

    def save(self, *args, **kwargs):
        if self.noindex:
            self.sitemap = False
        super().save(*args, **kwargs)
