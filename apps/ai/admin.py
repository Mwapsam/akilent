from django.contrib import admin

from apps.ai.models import AIProposal, AISettings
from apps.core.admin_readonly import ReadOnlyAdmin


@admin.register(AISettings)
class AISettingsAdmin(ReadOnlyAdmin):
    list_display = ("account", "enabled", "reply_mode", "consented_at")
    list_filter = ("enabled", "reply_mode")
    search_fields = ("account__company_name",)


@admin.register(AIProposal)
class AIProposalAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "account", "status", "intent", "model", "latency_ms", "error")
    list_filter = ("status",)
    search_fields = ("account__company_name", "error")
