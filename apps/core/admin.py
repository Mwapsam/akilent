from django.contrib import admin

from apps.core.admin_readonly import ReadOnlyAdmin
from apps.core.models import AdminAction, MailProviderSettings, SiteSettings

admin.site.site_header = "Akilent raw data"
admin.site.site_title = "Akilent raw data"
admin.site.index_title = "Read-only data. Make changes in the Operator console (/manage/)."


@admin.register(AdminAction)
class AdminActionAdmin(ReadOnlyAdmin):
    list_display = ("at", "actor", "action", "account", "target")
    list_filter = ("action",)
    search_fields = ("target", "account__company_name", "actor__username")


@admin.register(SiteSettings)
class SiteSettingsAdmin(ReadOnlyAdmin):
    list_display = ("app_name", "signups_enabled", "payments_enabled", "automation_events_enabled", "updated_at")


@admin.register(MailProviderSettings)
class MailProviderSettingsAdmin(ReadOnlyAdmin):
    list_display = ("infra_backend", "send_backend", "updated_at")
