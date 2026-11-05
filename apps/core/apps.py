from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Real-time is one more consumer of conversation events, on the same signal apps.ai
        # already connects to; conversations never imports apps.core for this.
        from apps.conversations.signals import conversation_message_processed
        from apps.core.realtime import on_conversation_message_processed

        conversation_message_processed.connect(
            on_conversation_message_processed,
            dispatch_uid="realtime-conversation-updates",
        )

        # A session starts on the person's current session generation (apps/core/sessions.py).
        from django.contrib.auth.signals import user_logged_in

        from apps.core.sessions import stamp_signed_in

        user_logged_in.connect(stamp_signed_in, dispatch_uid="session-generation-stamp")
