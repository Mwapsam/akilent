from django.contrib import admin

from .models import SEOPage


@admin.register(SEOPage)
class SEOPageAdmin(admin.ModelAdmin):
    list_display = ["path", "title", "noindex", "sitemap", "updated_at"]
    list_filter = ["noindex", "sitemap"]
    search_fields = ["path", "title", "description"]
    readonly_fields = ["updated_at"]
    fieldsets = [
        (None, {"fields": ["path", "title", "description"]}),
        ("Indexing", {"fields": ["noindex", "sitemap"]}),
        ("Advanced", {"fields": ["canonical_url", "og_image", "updated_at"]}),
    ]
