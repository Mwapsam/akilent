"""WhatsApp inbound message processing.

Business logic extracted from apps/whatsapp/tasks.py.
The Celery task remains as thin orchestration only.

Processing sequence:
  1. Resolve/get_or_create WhatsAppContact (by phone)
  2. register_inbound on WhatsApp Conversation (billing window)
  3. get_or_create MessageLog (idempotency anchor)
  4. project_to_inbox → canonical Contact + spine Conversation + Message
  5. Apply consent keywords (STOP/START/UNSTOP)
  6. Auto-reply during setup + mark_read + publish MessageReceived
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.core.exceptions import ValidationError

from apps.whatsapp.models import (
    Conversation,
    MessageLog,
    WebhookEventLog,
    WhatsAppContact,
)
from apps.whatsapp.models.contact import normalize_phone
from apps.whatsapp.models.tenant import get_account_for_webhook

logger = logging.getLogger(__name__)


class WhatsAppInboundService:
    """Stateless processor for a single inbound WhatsApp message payload item."""

    def __init__(self, event: WebhookEventLog, value: dict, message: dict) -> None:
        self.event = event
        self.value = value
        self.message = message

    def handle(self) -> None:
        from apps.whatsapp.interactive import extract_reply
        from apps.whatsapp.tasks import (
            _apply_consent_keyword,
            _auto_reply_during_setup,
            _automation_events_enabled,
            mark_read,
            project_to_inbox,
        )

        phone_number_id = self.value["metadata"]["phone_number_id"]
        account = get_account_for_webhook(phone_number_id)

        wa_id = self.message["from"]
        profile_name = (
            (self.value.get("contacts") or [{}])[0].get("profile", {}).get("name")
        )

        try:
            phone = normalize_phone(wa_id)
        except ValidationError:
            phone = wa_id

        # Step 1 — get_or_create WhatsAppContact
        contact_record, _ = WhatsAppContact.objects.get_or_create(
            account=account,
            phone_number=phone,
            defaults={"display_name": profile_name},
        )
        if profile_name and contact_record.display_name != profile_name:
            contact_record.display_name = profile_name
            contact_record.save(update_fields=["display_name"])

        msg_ts = datetime.fromtimestamp(int(self.message["timestamp"]), tz=UTC)

        # Step 2 — register_inbound / billing window
        wa_conversation = Conversation.get_or_open(contact_record)
        opens_window = (
            wa_conversation.window_expires_at is None
            or wa_conversation.window_expires_at <= msg_ts
        )
        wa_conversation.register_inbound(msg_ts)
        if opens_window:
            self._count_conversation(account)

        # Step 3 — content extraction
        msg_type = self.message.get("type", "unknown")
        content, media_id, media_mime_type = self._extract_content(
            self.message, msg_type, extract_reply
        )

        # Step 4 — get_or_create MessageLog (idempotency anchor)
        valid_types = {c[0] for c in MessageLog.MessageType.choices}
        reply = extract_reply(self.message)
        message_log, created = MessageLog.objects.get_or_create(
            account=account,
            message_id=self.message.get("id"),
            defaults={
                "conversation": wa_conversation,
                "contact": contact_record,
                "direction": MessageLog.Direction.INBOUND,
                "message_type": (
                    MessageLog.MessageType.TEXT
                    if reply is not None
                    else msg_type
                    if msg_type in valid_types
                    else MessageLog.MessageType.UNKNOWN
                ),
                "content": content,
                "media_id": media_id,
                "media_mime_type": media_mime_type,
                "status": MessageLog.Status.DELIVERED,
                "timestamp": msg_ts,
                "raw_payload": self.event.payload,
            },
        )

        contact_record.last_message_at = msg_ts
        contact_record.save(update_fields=["last_message_at"])

        if created and media_id:
            # Fetch now so a photo or voice note shows in the inbox within seconds,
            # not at the next once-a-minute sweep.
            from django.db import transaction

            from apps.whatsapp.tasks import download_media

            transaction.on_commit(download_media.delay)

        # Step 5 — project to spine (canonical contact + Conversation + Message)
        try:
            enroll = _automation_events_enabled()
        except Exception:
            enroll = False
        project_to_inbox(
            account,
            contact_record,
            wa_conversation,
            message_log,
            enroll_workflows=enroll,
        )

        # Step 6 — consent keywords, auto-reply, mark_read, domain event
        if created and msg_type == "text":
            _apply_consent_keyword(
                contact_record, wa_conversation, content, inbound_log_id=message_log.pk
            )

        if created and not contact_record.is_opted_out:
            _auto_reply_during_setup(phone_number_id, contact_record)

        if created and self.message.get("id"):
            from django.conf import settings

            if getattr(settings, "WHATSAPP_MARK_READ_ENABLED", True):
                mark_read.delay(account.id, self.message["id"])

        if created:
            self._publish_message_received(
                account,
                contact_record,
                msg_ts,
                msg_type,
                content,
                message_id=self.message.get("id") or "",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_content(message: dict, msg_type: str, extract_reply):
        content = ""
        media_id = media_mime_type = None
        reply = extract_reply(message)
        if reply is not None:
            content = reply["title"]
        elif msg_type == "text":
            content = message.get("text", {}).get("body", "")
        elif msg_type in ("image", "audio", "video", "document", "sticker"):
            block = message.get(msg_type, {})
            media_id = block.get("id")
            media_mime_type = block.get("mime_type")
            content = block.get("caption", "")
        elif msg_type == "location":
            loc = message.get("location", {})
            content = f"{loc.get('latitude')},{loc.get('longitude')}"
        return content, media_id, media_mime_type

    @staticmethod
    def _count_conversation(account) -> None:
        try:
            from apps.billing import api as billing_api

            billing_api.count_conversation(account)
        except Exception as exc:
            logger.warning(
                "WhatsAppInboundService: conversation not counted for account %s: %s",
                account.pk,
                exc,
            )

    @staticmethod
    def _publish_message_received(
        account, contact_record, msg_ts, msg_type, content, *, message_id: str
    ) -> None:
        from apps.core.events import MessageReceived, dispatcher
        from apps.whatsapp.tasks import _automation_events_enabled

        try:
            if _automation_events_enabled():
                dispatcher.publish(
                    MessageReceived(
                        account_id=account.id,
                        contact_id=contact_record.id,
                        message_id=message_id,
                        channel="whatsapp",
                        body=content,
                        message_type=msg_type,
                        occurred_at=msg_ts,
                    )
                )
        except Exception as exc:
            logger.debug("WhatsAppInboundService: failed to publish event: %s", exc)
