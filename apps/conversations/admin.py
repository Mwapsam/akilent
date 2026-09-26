from django.contrib import admin

from apps.conversations.models import Conversation, Message
from apps.core.admin_readonly import ReadOnlyAdmin


@admin.register(Conversation)
class ConversationAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "account", "channel", "status", "last_message_at")
    list_filter = ("channel", "status")
    search_fields = ("public_id", "account__company_name", "contact__phone")


@admin.register(Message)
class MessageAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "account", "conversation", "direction", "status")
    list_filter = ("direction",)
    search_fields = ("conversation__public_id", "account__company_name")
