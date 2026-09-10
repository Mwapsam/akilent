from django.contrib import admin

from apps.scheduler.models import ScheduledJob


@admin.register(ScheduledJob)
class ScheduledJobAdmin(admin.ModelAdmin):
    list_display = ("public_id", "account", "kind", "status", "fire_at", "tz",
                    "attempts", "occurrence_count", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("public_id", "idempotency_key", "account__company_name")
    readonly_fields = ("public_id", "created_at", "updated_at", "fired_at")
    date_hierarchy = "fire_at"
