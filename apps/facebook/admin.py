from django.contrib import admin

from .models import (
    FacebookComment,
    FacebookCommentThread,
    FacebookContact,
    FacebookConversation,
    FacebookMessage,
    FacebookOutboundMessage,
    FacebookWebhookEventLog,
)


@admin.register(FacebookContact)
class FacebookContactAdmin(admin.ModelAdmin):
    list_display = ["psid", "name", "account", "contact", "last_seen"]
    search_fields = ["psid", "name", "account__company_name"]
    raw_id_fields = ["contact"]


@admin.register(FacebookConversation)
class FacebookConversationAdmin(admin.ModelAdmin):
    list_display = [
        "facebook_contact",
        "account",
        "page_id",
        "is_open",
        "last_inbound_at",
    ]
    list_filter = ["is_open"]
    raw_id_fields = ["facebook_contact"]


@admin.register(FacebookCommentThread)
class FacebookCommentThreadAdmin(admin.ModelAdmin):
    list_display = ["comment_id", "account", "post_id", "intent", "received_at"]
    search_fields = ["comment_id", "post_id", "body"]
    raw_id_fields = ["facebook_contact", "conversation", "content_variant"]


@admin.register(FacebookComment)
class FacebookCommentAdmin(admin.ModelAdmin):
    list_display = [
        "comment_id",
        "thread",
        "direction",
        "moderation_state",
        "timestamp",
    ]
    list_filter = ["direction", "moderation_state"]
    raw_id_fields = ["thread", "facebook_contact"]


@admin.register(FacebookMessage)
class FacebookMessageAdmin(admin.ModelAdmin):
    list_display = ["message_id", "direction", "status", "timestamp"]
    list_filter = ["direction"]
    raw_id_fields = ["conversation", "comment_thread"]


@admin.register(FacebookOutboundMessage)
class FacebookOutboundMessageAdmin(admin.ModelAdmin):
    list_display = [
        "recipient_id",
        "account",
        "action_type",
        "status",
        "human_agent",
        "attempts",
        "created_at",
    ]
    list_filter = ["status", "action_type", "human_agent"]
    search_fields = ["idempotency_key", "recipient_id"]


@admin.register(FacebookWebhookEventLog)
class FacebookWebhookEventLogAdmin(admin.ModelAdmin):
    list_display = ["event_id", "account", "event_type", "status", "received_at"]
    list_filter = ["event_type", "status"]
    search_fields = ["event_id", "page_id"]
