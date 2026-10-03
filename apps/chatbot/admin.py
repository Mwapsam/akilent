from django.contrib import admin

from apps.chatbot.models import (
    ChatActionExecution,
    ChatbotAction,
    ChatbotConfig,
    ChatbotKnowledgeSource,
    ChatSession,
)


@admin.register(ChatbotConfig)
class ChatbotConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "account", "purpose", "is_active", "created_at")
    list_filter = ("purpose", "is_active")
    search_fields = ("name", "account__name")
    readonly_fields = ("public_key", "created_at", "updated_at")


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = (
        "session_key",
        "chatbot",
        "contact",
        "started_at",
        "last_activity_at",
    )
    list_filter = ("chatbot",)
    readonly_fields = ("session_key", "started_at", "last_activity_at", "identified_at")


@admin.register(ChatbotKnowledgeSource)
class ChatbotKnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = ("chatbot", "knowledge_entry", "is_active")
    list_filter = ("chatbot", "is_active")


@admin.register(ChatbotAction)
class ChatbotActionAdmin(admin.ModelAdmin):
    list_display = ("chatbot", "slug", "label", "is_enabled")
    list_filter = ("chatbot", "is_enabled")
    search_fields = ("slug", "label")


@admin.register(ChatActionExecution)
class ChatActionExecutionAdmin(admin.ModelAdmin):
    list_display = ("session", "action", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = ("created_at",)
