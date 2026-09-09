from django.contrib import admin

from apps.logs.models import MessageEvent


@admin.register(MessageEvent)
class MessageEventAdmin(admin.ModelAdmin):
    list_display = ("public_id", "type", "source", "account", "message", "occurred_at")
    list_filter = ("type", "source", "occurred_at")
    search_fields = ("public_id", "provider_event_id", "message__to_email")
    raw_id_fields = ("message", "account")
    readonly_fields = tuple(f.name for f in MessageEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
