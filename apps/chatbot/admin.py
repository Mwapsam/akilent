from django.contrib import admin

from apps.chatbot.models import (
    ChatActionExecution,
    ChatbotAction,
    ChatbotCategory,
    ChatbotConfig,
    ChatbotKnowledgeSource,
    ChatSession,
)


@admin.register(ChatbotCategory)
class ChatbotCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "chatbot_count", "is_active", "order")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("order", "is_active")
    ordering = ("order", "name")

    @admin.display(description="Chatbots")
    def chatbot_count(self, obj):
        return obj.chatbots.count()


class ChatbotActionInline(admin.TabularInline):
    model = ChatbotAction
    extra = 0
    fields = ("slug", "label", "description", "is_enabled")
    show_change_link = True


class ChatbotKnowledgeSourceInline(admin.TabularInline):
    model = ChatbotKnowledgeSource
    extra = 0
    fields = ("knowledge_entry", "is_active")
    show_change_link = True


@admin.register(ChatbotConfig)
class ChatbotConfigAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "account",
        "category",
        "purpose",
        "position",
        "is_active",
        "action_count",
        "created_at",
    )
    list_filter = ("purpose", "is_active", "category", "position")
    search_fields = ("name", "account__company_name", "public_key")
    readonly_fields = ("public_key", "created_at", "updated_at")
    autocomplete_fields = ("account",)
    inlines = [ChatbotActionInline, ChatbotKnowledgeSourceInline]
    fieldsets = (
        (
            "Identity",
            {
                "fields": (
                    "account",
                    "name",
                    "public_key",
                    "purpose",
                    "category",
                    "is_active",
                )
            },
        ),
        (
            "Appearance",
            {"fields": ("welcome_message", "primary_color", "position", "avatar_url")},
        ),
        (
            "Security",
            {
                "fields": ("allowed_domains", "handoff_action"),
                "description": "Empty allowed_domains = deny all origins.",
            },
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    @admin.display(description="Actions")
    def action_count(self, obj):
        return obj.actions.count()


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = (
        "session_key",
        "chatbot",
        "visitor_name",
        "visitor_email",
        "contact",
        "started_at",
        "last_activity_at",
        "identified_at",
    )
    list_filter = ("chatbot", "chatbot__account")
    search_fields = ("session_key", "visitor_name", "visitor_email", "visitor_phone")
    readonly_fields = (
        "session_key",
        "started_at",
        "last_activity_at",
        "identified_at",
    )
    date_hierarchy = "started_at"


@admin.register(ChatbotKnowledgeSource)
class ChatbotKnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = ("chatbot", "knowledge_entry", "is_active")
    list_filter = ("chatbot", "is_active")
    autocomplete_fields = ("chatbot",)


@admin.register(ChatbotAction)
class ChatbotActionAdmin(admin.ModelAdmin):
    list_display = ("chatbot", "slug", "label", "is_enabled")
    list_filter = ("chatbot__account", "chatbot", "is_enabled")
    search_fields = ("slug", "label", "chatbot__name")
    autocomplete_fields = ("chatbot",)


@admin.register(ChatActionExecution)
class ChatActionExecutionAdmin(admin.ModelAdmin):
    list_display = ("session", "action", "status", "created_at")
    list_filter = ("status", "action__chatbot")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
