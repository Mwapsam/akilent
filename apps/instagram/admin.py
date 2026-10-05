from django.contrib import admin

from .models import (
    Comment,
    CommentThread,
    CommentTrigger,
    InstagramBusinessAccount,
    InstagramContact,
    InstagramConversation,
    InstagramMessage,
    ModerationLog,
    ModerationRule,
    OutboundMessage,
    WebhookEventLog,
)


@admin.register(InstagramBusinessAccount)
class InstagramBusinessAccountAdmin(admin.ModelAdmin):
    list_display = [
        "account",
        "username",
        "instagram_business_account_id",
        "is_active",
        "token_expired",
    ]
    list_filter = ["is_active", "token_expired"]
    search_fields = [
        "account__company_name",
        "username",
        "instagram_business_account_id",
    ]
    readonly_fields = ["created_at", "updated_at", "webhook_subscribed_at"]


@admin.register(InstagramContact)
class InstagramContactAdmin(admin.ModelAdmin):
    list_display = [
        "instagram_scoped_id",
        "username",
        "account",
        "contact",
        "opted_out",
    ]
    list_filter = ["opted_out", "messaging_eligible"]
    search_fields = ["instagram_scoped_id", "username", "name"]
    raw_id_fields = ["contact"]


@admin.register(InstagramConversation)
class InstagramConversationAdmin(admin.ModelAdmin):
    list_display = [
        "instagram_contact",
        "instagram_account",
        "is_open",
        "last_message_at",
    ]
    list_filter = ["is_open"]
    raw_id_fields = ["instagram_contact", "instagram_account"]


@admin.register(CommentThread)
class CommentThreadAdmin(admin.ModelAdmin):
    list_display = [
        "comment_id",
        "instagram_contact",
        "post_type",
        "intent",
        "received_at",
    ]
    list_filter = ["post_type"]
    search_fields = ["comment_id", "body", "intent"]
    raw_id_fields = ["instagram_contact", "instagram_account", "conversation"]


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = [
        "comment_id",
        "thread",
        "direction",
        "moderation_state",
        "timestamp",
    ]
    list_filter = ["direction", "moderation_state"]
    raw_id_fields = ["thread", "instagram_contact"]


@admin.register(InstagramMessage)
class InstagramMessageAdmin(admin.ModelAdmin):
    list_display = ["message_id", "direction", "status", "timestamp"]
    list_filter = ["direction", "status"]
    search_fields = ["message_id", "body"]
    raw_id_fields = ["conversation", "comment_thread"]


@admin.register(OutboundMessage)
class OutboundMessageAdmin(admin.ModelAdmin):
    list_display = [
        "recipient_igsid",
        "action_type",
        "status",
        "attempts",
        "created_at",
    ]
    list_filter = ["status", "action_type"]
    search_fields = ["recipient_igsid", "idempotency_key"]
    readonly_fields = ["created_at", "updated_at", "sent_at"]


@admin.register(WebhookEventLog)
class WebhookEventLogAdmin(admin.ModelAdmin):
    list_display = ["event_type", "status", "attempts", "received_at", "processed_at"]
    list_filter = ["event_type", "status"]
    readonly_fields = ["received_at", "processed_at"]


@admin.register(CommentTrigger)
class CommentTriggerAdmin(admin.ModelAdmin):
    list_display = ["name", "account", "match_type", "priority", "is_active"]
    list_filter = ["match_type", "is_active"]
    search_fields = ["name", "keywords", "reply_template"]
    raw_id_fields = ["account"]


@admin.register(ModerationRule)
class ModerationRuleAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "account",
        "match_type",
        "moderation_action",
        "automation_trigger",
        "priority",
        "is_active",
    ]
    list_filter = ["match_type", "moderation_action", "automation_trigger", "is_active"]
    search_fields = ["name", "keywords"]
    raw_id_fields = ["account"]


@admin.register(ModerationLog)
class ModerationLogAdmin(admin.ModelAdmin):
    list_display = [
        "comment",
        "moderation_action",
        "automation_trigger",
        "outcome",
        "occurred_at",
    ]
    list_filter = ["moderation_action", "automation_trigger", "outcome"]
    raw_id_fields = ["account", "rule", "comment", "acted_by"]
    readonly_fields = ["occurred_at"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
