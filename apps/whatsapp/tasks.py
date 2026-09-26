import logging
import mimetypes
from datetime import datetime, timedelta, timezone as dt_timezone

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Q
from django.utils import timezone

from apps.core.events import dispatcher, MessageReceived, MessageStatusChanged
from apps.whatsapp.models import (
    Conversation,
    MessageLog,
    MessageTemplate,
    OutboundMessage,
    WebhookEventLog,
    WhatsAppContact,
)
from apps.whatsapp.models.tenant import (
    TenantResolutionError,
    WhatsAppBusinessNumber,
    get_account_for_webhook,
    get_number_for_webhook,
)
from apps.whatsapp import verification_codes
from apps.whatsapp.interactive import extract_reply
from apps.whatsapp.models.contact import normalize_phone

logger = logging.getLogger(__name__)

_OUTBOUND_BATCH = 50
_MEDIA_BATCH = 20
_MAX_EVENT_ATTEMPTS = 3
_SENDING_STALE = timedelta(minutes=10)

_PAYLOAD_TYPE_TO_LOG_TYPE = {
    "text": MessageLog.MessageType.TEXT,
    "template": MessageLog.MessageType.TEMPLATE,
    "image": MessageLog.MessageType.IMAGE,
    "audio": MessageLog.MessageType.AUDIO,
    "video": MessageLog.MessageType.VIDEO,
    "document": MessageLog.MessageType.DOCUMENT,
    "sticker": MessageLog.MessageType.STICKER,
    "interactive": MessageLog.MessageType.TEXT,
}
_AUTOMATION_EVENTS_CACHE_KEY = "whatsapp_automation_events_enabled"
_AUTOMATION_EVENTS_CACHE_TTL = 60  # 60 second cache for SiteSettings flag


def _automation_events_enabled() -> bool:
    """Check if automation events are enabled, with short-lived caching.

    This caches the SiteSettings.automation_events_enabled flag for 60 seconds
    to avoid a database query on every webhook. The flag can be changed at runtime;
    the cache will refresh within the TTL.

    TODO(Phase 4): Replace with ModuleSubscription model check.
    """
    cached = cache.get(_AUTOMATION_EVENTS_CACHE_KEY)
    if cached is not None:
        return cached

    from apps.core.models import SiteSettings
    result = SiteSettings.objects.filter(automation_events_enabled=True).exists()
    cache.set(_AUTOMATION_EVENTS_CACHE_KEY, result, _AUTOMATION_EVENTS_CACHE_TTL)
    return result


@shared_task(
    bind=True,
    max_retries=_MAX_EVENT_ATTEMPTS,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_whatsapp_event(self, event_id: int):
    try:
        event = WebhookEventLog.objects.get(pk=event_id)
    except WebhookEventLog.DoesNotExist:
        logger.error("process_whatsapp_event: event %s not found", event_id)
        return

    if event.processed:
        return

    try:
        if event.event_type in ("message", "status"):
            # One POST can batch messages and statuses across several changes/entries,
            # so both handlers walk the whole payload (each is a no-op if it has none).
            _handle_inbound_message(event)
            _handle_status_update(event)
        elif event.event_type == "message_template_status_update":
            _handle_template_status_update(event)
        else:
            logger.debug(
                "process_whatsapp_event: no handler for event_type=%s", event.event_type
            )
        event.mark_processed()
    except TenantResolutionError as exc:
        logger.warning(
            "process_whatsapp_event: tenant not found for event %s: %s", event_id, exc
        )
        event.mark_failed(str(exc))
    except Exception as exc:
        event.mark_failed(str(exc))
        logger.exception("process_whatsapp_event: unhandled error for event %s", event_id)
        raise self.retry(exc=exc)


def _close_spine_conversation(whatsapp_conversation) -> None:
    """Keep the inbox in step when a WhatsApp conversation is closed (e.g. STOP)."""
    spine = getattr(whatsapp_conversation, "generic_conversation", None)
    if spine is not None:
        spine.close()


def _apply_consent_keyword(contact, conversation, body: str) -> None:
    """Honor STOP / START keywords in an inbound text message.

    A lone keyword (ignoring surrounding whitespace/punctuation) toggles the
    contact's messaging consent. STOP also closes the open conversation and
    queues a one-off confirmation reply (allowed past the opt-out block via the
    ``_consent_ack`` flag).
    """
    token = (body or "").strip().strip(".!?").upper()
    if not token:
        return

    if token in settings.WHATSAPP_STOP_KEYWORDS:
        if not contact.is_opted_out:
            contact.record_opt_out("inbound_keyword")
        conversation.close()
        _close_spine_conversation(conversation)
        confirmation = settings.WHATSAPP_OPT_OUT_CONFIRMATION
        if confirmation:
            OutboundMessage.objects.create(
                account=contact.account,
                contact=contact,
                payload={
                    "type": "text",
                    "body": confirmation,
                    "_consent_ack": True,
                },
            )
            drain_outbound_queue.delay()
    elif token in settings.WHATSAPP_START_KEYWORDS:
        contact.record_opt_in("inbound_keyword")


def _auto_reply_during_setup(phone_number_id: str, contact) -> None:
    """Confirm a brand-new connection by answering the first inbound message.

    Never lets a failure here fail (and retry) the inbound event itself.
    """
    try:
        from apps.whatsapp.verification import AUTO_REPLY_BODY, maybe_auto_reply

        result = maybe_auto_reply(get_number_for_webhook(phone_number_id), contact.phone_number)
        if result and result.get("ok") and result.get("message_id"):
            _log_setup_reply(contact, result["message_id"], AUTO_REPLY_BODY)
    except Exception as exc:
        logger.warning("auto-reply during setup failed for %s: %s", phone_number_id, exc)


def _log_setup_reply(contact, message_id: str, body: str) -> None:
    """Record the setup confirmation as a real outbound message.

    It is sent straight to the provider, so without this the inbox would show the owner's
    test message as unanswered forever (and Missed recovery would remind the team about it).
    Logging it, then projecting like any other outbound, keeps the conversation truthful.
    """
    log = MessageLog.objects.create(
        account=contact.account,
        conversation=Conversation.get_or_open(contact),
        contact=contact,
        direction=MessageLog.Direction.OUTBOUND,
        message_type=MessageLog.MessageType.TEXT,
        message_id=message_id,
        content=body,
        status=MessageLog.Status.SENT,
        timestamp=timezone.now(),
    )
    project_outbound_to_inbox(log)


def project_to_inbox(account, wa_contact, whatsapp_conversation, message_log, *, enroll_workflows: bool):
    """Put an inbound message in the Inbox (generic Conversation/Message spine).

    The Inbox is a core feature, so this never depends on the beta
    ``automation_events_enabled`` flag: that flag only decides whether Workflows
    are *enrolled*. Idempotent (a replay re-uses the same Message) and
    best-effort — a failure is logged, never raised, so the inbound event itself
    still completes.
    """
    try:
        from apps.conversations.services import record_inbound_whatsapp_message

        contact = _canonical_contact(account, wa_contact)
        record_inbound_whatsapp_message(
            contact=contact, wa_contact=wa_contact,
            whatsapp_conversation=whatsapp_conversation, message_log=message_log,
            enroll_workflows=enroll_workflows,
        )
    except Exception:
        logger.exception(
            "project_to_inbox failed for account=%s message_id=%s", account.pk, message_log.message_id,
        )


def _canonical_contact(account, wa_contact):
    """The ``apps.contacts.Contact`` behind a WhatsApp identity, created (phone-only) if missing."""
    from apps.contacts.services import upsert_contact_by_phone

    contact = wa_contact.contact
    if contact is None:
        contact, created = upsert_contact_by_phone(account, wa_contact.phone_number, source="whatsapp")
        if created and wa_contact.display_name and not contact.first_name:
            contact.first_name = wa_contact.display_name[:150]
            contact.save(update_fields=["first_name", "updated_at"])
        wa_contact.contact = contact
        wa_contact.save(update_fields=["contact"])
    return contact


def project_outbound_to_inbox(log: MessageLog) -> None:
    """Mirror a business message (human reply, template or workflow send) onto the spine.

    Called wherever the provider log for an outbound message is created or changes, so the
    inbox never has to consult ``MessageLog`` to know what the business said. Idempotent
    (a replay updates the same spine message) and best-effort like the inbound projection.
    The channel-neutral work lives in ``apps.conversations``; this only adapts.
    """
    from apps.conversations import metrics

    if verification_codes.is_verification_code(log.raw_payload):
        return  # a one-time code is a system message, not a conversation with the business

    try:
        from apps.conversations.models import Conversation as SpineConversation
        from apps.conversations.services import record_outbound_message
        from apps.whatsapp.friendly_errors import friendly_send_error

        _canonical_contact(log.account, log.contact)
        spine = SpineConversation.get_or_create_for_whatsapp(log.conversation)
        metadata = {"message_type": log.message_type}
        # An automatic AI reply (apps.ai.autonomy) is sent with an "ai-auto:" key, so the inbox
        # can label it "Sent by AI" without WhatsApp knowing anything about AI.
        key = getattr(getattr(log, "outbound_source", None), "idempotency_key", "") or ""
        if key.startswith("ai-auto:"):
            metadata["sent_by"] = "ai"
        elif key.startswith("wf:"):  # an automation's reply_text step (apps.automation.workflow_engine)
            metadata["sent_by"] = "automation"
        if log.status == MessageLog.Status.FAILED:
            # OutboundMessage carries the error code; MessageLog doesn't. A
            # human/template/workflow reply that fails to send is the one place
            # an agent needs a *reason*, not just "Failed" — so it's translated
            # here, once, for every send path (see docs/plans amendment R0).
            outbound = getattr(log, "outbound_source", None)
            error_code = getattr(outbound, "error_code", "") or ""
            metadata["error_code"] = error_code
            metadata["failure_reason"] = friendly_send_error(error_code)
        record_outbound_message(
            conversation=spine, body=log.content, timestamp=log.timestamp, status=log.status,
            metadata=metadata, whatsapp_message=log,
        )
    except Exception:
        metrics.incr("outbound_projection_failures")
        logger.exception("project_outbound_to_inbox failed for message_log=%s", log.pk)


def _iter_payload_items(payload: dict, key: str):
    """Yield ``(value, item)`` for every ``key`` ("messages"/"statuses") item in every
    change of every entry. Meta may batch any number of each in one POST."""
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for item in value.get(key) or []:
                yield value, item


def _for_each_item(items, handler) -> None:
    """Run ``handler`` on every item even if some fail, then re-raise.

    A bad item (unroutable number, transient error) must not stop the rest of the
    batch from being processed. Handlers are idempotent, so the retry that follows a
    re-raised failure safely replays the whole event. A non-tenant error wins so the
    task retries; a tenant-only failure is surfaced as before (event marked failed).
    """
    tenant_exc = other_exc = None
    for value, item in items:
        try:
            handler(value, item)
        except TenantResolutionError as exc:
            logger.warning("webhook batch item skipped: %s", exc)
            tenant_exc = tenant_exc or exc
        except Exception as exc:
            logger.exception("webhook batch item failed")
            other_exc = other_exc or exc
    if other_exc or tenant_exc:
        raise other_exc or tenant_exc


def _handle_inbound_message(event: WebhookEventLog) -> None:
    _for_each_item(
        _iter_payload_items(event.payload, "messages"),
        lambda value, message: _process_inbound_message(event, value, message),
    )


def _process_inbound_message(event: WebhookEventLog, value: dict, message: dict) -> None:
    phone_number_id = value["metadata"]["phone_number_id"]

    account = get_account_for_webhook(phone_number_id)

    try:
        from apps.billing.limits import LimitChecker, PlanLimitExceeded
        LimitChecker(account).check_conversation()
    except Exception as exc:
        from apps.billing.limits import PlanLimitExceeded
        if isinstance(exc, PlanLimitExceeded):
            logger.warning(
                "_handle_inbound_message: conversation limit exceeded for account %s: %s",
                account.pk, exc,
            )
            return
        logger.debug("_handle_inbound_message: limit check skipped: %s", exc)

    wa_id = message["from"]
    profile_name = (value.get("contacts") or [{}])[0].get("profile", {}).get("name")

    # Meta sends wa_id without "+", but contacts are stored normalized (E.164).
    # Looking up the raw value misses an existing contact and then violates the
    # unique constraint on create, so the message would never be logged.
    try:
        phone = normalize_phone(wa_id)
    except ValidationError:
        phone = wa_id
    contact, _ = WhatsAppContact.objects.get_or_create(
        account=account,
        phone_number=phone,
        defaults={"display_name": profile_name},
    )
    if profile_name and contact.display_name != profile_name:
        contact.display_name = profile_name
        contact.save(update_fields=["display_name"])

    msg_ts = datetime.fromtimestamp(int(message["timestamp"]), tz=dt_timezone.utc)
    conversation = Conversation.get_or_open(contact)
    conversation.register_inbound(msg_ts)

    try:
        from apps.billing.models import UsageSummary
        UsageSummary.increment_conversations(account)
    except Exception as exc:
        logger.debug("_handle_inbound_message: usage increment skipped: %s", exc)

    msg_type = message.get("type", "unknown")
    content = ""
    media_id = media_mime_type = None
    reply = extract_reply(message)

    if reply is not None:
        # A tap on a button or list choice. Its title is what the customer saw, so it is the
        # message text; it is stored as text so the inbox shows it, while ``msg_type`` stays
        # "interactive"/"button" so STOP-keyword handling still only reads typed messages.
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

    valid_types = {c[0] for c in MessageLog.MessageType.choices}
    message_log, created = MessageLog.objects.get_or_create(
        account=account,
        message_id=message.get("id"),
        defaults={
            "conversation": conversation,
            "contact": contact,
            "direction": MessageLog.Direction.INBOUND,
            "message_type": (
                MessageLog.MessageType.TEXT if reply is not None
                else msg_type if msg_type in valid_types else MessageLog.MessageType.UNKNOWN
            ),
            "content": content,
            "media_id": media_id,
            "media_mime_type": media_mime_type,
            "status": MessageLog.Status.DELIVERED,
            "timestamp": msg_ts,
            "raw_payload": event.payload,
        },
    )

    contact.last_message_at = msg_ts
    contact.save(update_fields=["last_message_at"])

    try:
        enroll = _automation_events_enabled()
    except Exception:
        enroll = False
    # Regardless of `created`: idempotent, and a replay repairs an earlier failed projection.
    project_to_inbox(account, contact, conversation, message_log, enroll_workflows=enroll)

    if created and msg_type == "text":
        _apply_consent_keyword(contact, conversation, content)

    if created and not contact.is_opted_out:
        _auto_reply_during_setup(phone_number_id, contact)

    if (
        created
        and message.get("id")
        and getattr(settings, "WHATSAPP_MARK_READ_ENABLED", True)
    ):
        mark_read.delay(account.id, message["id"])

    # Publish domain event for subscribers (automation, AI, analytics)
    # IMPORTANT: Only publish for newly created messages to prevent duplicate automation
    # evaluations when webhooks are replayed or messages are reprocessed.
    # Gate behind a temporary SiteSettings flag until Phase 4's ModuleSubscription exists
    if created:
        try:
            if _automation_events_enabled():
                dispatcher.publish(
                    MessageReceived(
                        account_id=account.id,
                        contact_id=contact.id,
                        message_id=message.get("id"),
                        channel="whatsapp",
                        body=content,
                        message_type=msg_type,
                        occurred_at=msg_ts,
                    )
                )
        except Exception as exc:
            logger.debug("_handle_inbound_message: failed to publish event: %s", exc)


def _handle_status_update(event: WebhookEventLog) -> None:
    from apps.conversations import metrics

    def handle(value, status_obj):
        metrics.incr("status_webhooks_received", status=status_obj.get("status") or "unknown")
        try:
            _process_status_update(value, status_obj)
        except TenantResolutionError:
            raise
        except Exception:
            metrics.incr("status_update_failures")
            raise

    _for_each_item(_iter_payload_items(event.payload, "statuses"), handle)


def _record_status_lag(status_obj: dict) -> None:
    """Seconds between the provider reporting a status and us applying it."""
    try:
        from apps.conversations import metrics

        reported = datetime.fromtimestamp(int(status_obj["timestamp"]), tz=dt_timezone.utc)
        metrics.incr("status_update_lag_seconds", (timezone.now() - reported).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass


def _process_status_update(value: dict, status_obj: dict) -> None:
    message_id = status_obj.get("id")
    new_status = status_obj.get("status")  # "sent" | "delivered" | "read" | "failed"
    if not message_id or not new_status:
        return

    # message_id is only unique per account, so the lookup must be tenant-scoped.
    phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
    account = get_account_for_webhook(phone_number_id)

    try:
        log = MessageLog.objects.get(
            account=account,
            message_id=message_id,
            direction=MessageLog.Direction.OUTBOUND,
        )
        status_changed = log.apply_status_update(new_status)

        # Keep the spine message in step with the provider log (idempotent; also repairs an
        # earlier failed projection). This is what flips a failed send back to "waiting".
        project_outbound_to_inbox(log)
        _record_status_lag(status_obj)

        # Publish domain event for subscribers (automation, AI, analytics)
        # IMPORTANT: Only publish if the status actually changed to prevent duplicate
        # automation evaluations when webhooks are replayed.
        # Gate behind a temporary SiteSettings flag until Phase 4's ModuleSubscription exists
        if status_changed:
            try:
                if _automation_events_enabled():
                    dispatcher.publish(
                        MessageStatusChanged(
                            account_id=log.account_id,
                            message_id=message_id,
                            status=new_status,
                            occurred_at=timezone.now(),
                        )
                    )
            except Exception as exc:
                logger.debug("_handle_status_update: failed to publish event: %s", exc)

    except MessageLog.DoesNotExist:
        logger.debug(
            "_handle_status_update: no outbound log for message_id=%s", message_id
        )


def _get_provider_for_account(account):
    """Get a WhatsAppProvider instance for the account.

    Returns None if the account has no usable (active, tokened) number.
    """
    from apps.whatsapp.providers import get_whatsapp_provider, WhatsAppProviderError

    try:
        return get_whatsapp_provider(account)
    except WhatsAppProviderError:
        return None


@shared_task
def mark_read(account_id: int, message_id: str) -> None:
    """Best-effort blue-tick: tell Meta we've read an inbound message.

    Failures are logged and dropped — a missed read receipt is cosmetic.
    """
    from apps.accounts.models import Account

    try:
        account = Account.objects.get(pk=account_id)
        provider = _get_provider_for_account(account)
        if provider is None:
            return
        provider.mark_as_read(message_id)
    except Exception as exc:
        logger.debug("mark_read: skipped for %s: %s", message_id, exc)


def _send_outbound(provider, contact, payload: dict) -> dict:
    """Dispatch an OutboundMessage payload via the provider.

    Args:
        provider: WhatsAppProvider instance.
        contact: WhatsAppContact instance.
        payload: Message payload dict with type, content, etc.

    Returns:
        Dict with success status and message_id (or error info).

    Raises:
        WhatsAppProviderError: if the provider call fails.
    """
    from apps.whatsapp.providers import WhatsAppProviderError

    msg_type = payload.get("type", "text")
    to = contact.phone_number

    try:
        if msg_type == "template":
            # Only `components` is Meta's wire shape. `payload["params"]` is the
            # label -> value record kept for display and must never be sent as-is
            # (a dict where Meta expects a list is rejected with a 400).
            components = payload.get("components")
            result = provider.send_template(
                to,
                payload["template_name"],
                payload.get("language", "en"),
                components if isinstance(components, list) else [],
            )
        elif msg_type in ("image", "audio", "video", "document", "sticker"):
            media_id = payload.get("media_id")
            if not media_id and payload.get("media_path"):
                # Upload a file already sitting in our storage backend, then send.
                with default_storage.open(payload["media_path"], "rb") as fh:
                    content = fh.read()
                upload = provider.upload_media(
                    content,
                    payload.get("mime_type", "application/octet-stream"),
                    payload["media_path"].rsplit("/", 1)[-1],
                )
                media_id = upload.media_id
            result = provider.send_media(
                to, msg_type, media_id, payload.get("caption", "")
            )
        elif msg_type == "interactive":
            result = provider.send_interactive(to, payload["interactive"])
        else:
            result = provider.send_text(to, payload.get("body", payload.get("text", "")))

        if not result.success:
            err = WhatsAppProviderError(f"Send failed: {result.error}")
            err.code = result.error_code or ""
            err.retryable = result.retryable
            raise err

        return {"success": True, "message_id": result.message_id}
    except WhatsAppProviderError:
        raise


def _log_type_for_payload(payload: dict) -> str:
    return _PAYLOAD_TYPE_TO_LOG_TYPE.get(
        payload.get("type", "text"), MessageLog.MessageType.UNKNOWN
    )


def _log_content_for_payload(payload: dict) -> str:
    ptype = payload.get("type", "text")
    if ptype == "text":
        return payload.get("body", payload.get("text", "")) or ""
    if ptype == "template":
        return payload.get("template_name", "") or ""
    if ptype == "interactive":
        # The transcript should show what the customer was offered, not just the question.
        body = payload.get("body", "") or ""
        options = payload.get("options") or []
        return f"{body}\n\n[{' | '.join(options)}]" if options else body
    return payload.get("caption", "") or ""


class SendNotAuthorized(Exception):
    """Raised when a queued message must not be dispatched (policy failure).

    Carries a stable ``code`` so callers / UI can react (e.g. prompt the user to
    pick an approved template). Always a terminal failure — never retried.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _authorize_send(msg: OutboundMessage) -> None:
    """Enforce WhatsApp messaging policy before a send.

    * template messages require a linked, Meta-approved MessageTemplate;
    * free-text / media may only go out inside the open 24h customer-service
      window (an inbound message from the contact in the last 24h).
    """
    payload = msg.payload or {}

    # System-generated consent acknowledgements (e.g. the STOP confirmation) are
    # always allowed — they bypass both the opt-out block and the window check.
    if payload.get("_consent_ack"):
        return

    ptype = payload.get("type", "text")
    code_request = verification_codes.is_verification_code(payload)

    if code_request and verification_codes.is_expired(payload):
        raise SendNotAuthorized("CODE_EXPIRED", "The one-time code expired before it could be sent.")

    # A one-time code was asked for by the person themselves, so an earlier STOP doesn't block it.
    if not code_request and msg.contact.opt_in_status == WhatsAppContact.OptInStatus.OPTED_OUT:
        raise SendNotAuthorized(
            "CONTACT_OPTED_OUT",
            "Contact has opted out of WhatsApp messages.",
        )

    if ptype == "template":
        template = msg.template
        if (
            template is None
            or template.approval_status != MessageTemplate.ApprovalStatus.APPROVED
        ):
            raise SendNotAuthorized(
                "TEMPLATE_NOT_APPROVED",
                "Template sends require a linked, Meta-approved MessageTemplate.",
            )
        if (
            template.category == MessageTemplate.Category.MARKETING
            and msg.contact.opt_in_status != WhatsAppContact.OptInStatus.OPTED_IN
        ):
            raise SendNotAuthorized(
                "MARKETING_REQUIRES_OPT_IN",
                "Marketing templates require an explicit opt-in from the contact.",
            )
        return

    conversation = Conversation.get_or_open(msg.contact)
    if not conversation.window_is_open:
        raise SendNotAuthorized(
            "OUTSIDE_WINDOW_NO_TEMPLATE",
            "The 24h customer-service window is closed; use an approved template.",
        )


def _ensure_outbound_log(msg: OutboundMessage) -> MessageLog:
    """Create (once) the MessageLog mirror for an OutboundMessage.

    Every outbound send is mirrored into MessageLog so that status webhooks
    (delivered/read/failed), which arrive keyed only by the provider message id,
    have a row to reconcile against.
    """
    if msg.message_log_id:
        return msg.message_log

    payload = msg.payload or {}
    pinned_id = payload.get("_conversation_id")
    if pinned_id:
        # The caller sent this from a specific conversation (e.g. an agent
        # replying with a template in the inbox). Opening a new one instead
        # would surface their message in a different thread.
        conversation = Conversation.objects.filter(
            pk=pinned_id, contact=msg.contact
        ).first() or Conversation.get_or_open(msg.contact)
    elif payload.get("_consent_ack"):
        # A consent acknowledgement must not resurrect a closed conversation —
        # attach it to the most recent one (open or closed) if any exists.
        conversation = (
            Conversation.objects.filter(contact=msg.contact)
            .order_by("-created_at")
            .first()
        ) or Conversation.get_or_open(msg.contact)
    else:
        conversation = Conversation.get_or_open(msg.contact)
    log = MessageLog.objects.create(
        account=msg.account,
        conversation=conversation,
        contact=msg.contact,
        direction=MessageLog.Direction.OUTBOUND,
        message_type=_log_type_for_payload(msg.payload),
        content=_log_content_for_payload(msg.payload),
        status=MessageLog.Status.QUEUED,
        timestamp=timezone.now(),
        raw_payload=msg.payload,
    )
    msg.message_log = log
    msg.save(update_fields=["message_log"])
    return log


def _throttle_for_account(account, cache: dict) -> None:
    """Block until a send token is available for the account's active number."""
    number = cache.get(account.id)
    if number is None:
        number = WhatsAppBusinessNumber.objects.filter(
            account=account, is_active=True
        ).first()
        cache[account.id] = number
    if number is None:
        return
    from apps.whatsapp.services import get_whatsapp_rate_limiter

    get_whatsapp_rate_limiter(
        number.phone_number_id, number.send_rate_limit
    ).wait_for(1)


def _notify_terminal_failure(msg) -> None:
    """Tell any consumer outside apps.whatsapp that this send permanently failed.

    Only fires once ``mark_failed`` has actually landed the message in its
    terminal FAILED state — a retryable failure re-queues instead, and no
    notification is due yet. Kept a thin, best-effort call (never lets a
    downstream consumer's failure break the outbound queue) so apps.whatsapp
    doesn't need to know who's listening.
    """
    if msg.status != OutboundMessage.Status.FAILED:
        return
    try:
        from apps.automation.integrations.whatsapp import mark_outbound_message_failed

        mark_outbound_message_failed(msg)
    except Exception:
        logger.exception("_notify_terminal_failure: reconciliation hook failed for message %s", msg.id)


@shared_task(acks_late=True, reject_on_worker_lost=True)
def drain_outbound_queue():
    now = timezone.now()

    # Recover messages left mid-flight by a crashed/killed worker.
    recovered = OutboundMessage.objects.filter(
        status=OutboundMessage.Status.SENDING,
        updated_at__lt=now - _SENDING_STALE,
    ).update(status=OutboundMessage.Status.QUEUED)
    if recovered:
        logger.warning(
            "drain_outbound_queue: recovered %s stale SENDING messages", recovered
        )

    due = (
        OutboundMessage.objects.filter(
            status=OutboundMessage.Status.QUEUED,
            scheduled_at__lte=now,
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .select_related("account", "contact")[:_OUTBOUND_BATCH]
    )

    sent = failed = 0
    providers: dict = {}
    numbers: dict = {}
    for msg in due:
        try:
            provider = providers.get(msg.account_id)
            if provider is None:
                provider = _get_provider_for_account(msg.account)
                providers[msg.account_id] = provider
            if provider is None:
                raise RuntimeError(
                    "No active WhatsApp number with an access token for this account."
                )

            _authorize_send(msg)

            log = _ensure_outbound_log(msg)

            msg.status = OutboundMessage.Status.SENDING
            msg.save(update_fields=["status"])

            _throttle_for_account(msg.account, numbers)
            result = _send_outbound(provider, msg.contact, msg.payload)

            message_id = result.get("message_id") or ""
            log.message_id = message_id or None
            log.status = MessageLog.Status.SENT
            log.save(update_fields=["message_id", "status"])

            msg.status = OutboundMessage.Status.SENT
            msg.sent_at = timezone.now()
            msg.save(update_fields=["status", "sent_at"])
            verification_codes.forget_code(msg)

            log.conversation.register_outbound(msg.sent_at)
            project_outbound_to_inbox(log)
            sent += 1
        except SendNotAuthorized as exc:
            msg.mark_failed(f"{exc.code}: {exc}", terminal=True)
            if msg.message_log_id:
                MessageLog.objects.filter(pk=msg.message_log_id).update(
                    status=MessageLog.Status.FAILED
                )
                project_outbound_to_inbox(MessageLog.objects.get(pk=msg.message_log_id))
            verification_codes.forget_code(msg)
            _notify_terminal_failure(msg)
            failed += 1
        except Exception as exc:
            code = getattr(exc, "code", "") or ""
            retryable = getattr(exc, "retryable", True)
            msg.mark_failed(str(exc), terminal=not retryable, error_code=code)
            if (
                msg.message_log_id
                and msg.status == OutboundMessage.Status.FAILED
            ):
                MessageLog.objects.filter(pk=msg.message_log_id).update(
                    status=MessageLog.Status.FAILED
                )
                project_outbound_to_inbox(MessageLog.objects.get(pk=msg.message_log_id))
            if msg.status == OutboundMessage.Status.FAILED:
                verification_codes.forget_code(msg)
            _notify_terminal_failure(msg)
            failed += 1

    if sent or failed:
        logger.info("drain_outbound_queue: sent=%s failed=%s", sent, failed)


@shared_task
def close_expired_conversations():
    expired = Conversation.objects.filter(
        is_open=True,
        window_expires_at__isnull=False,
        window_expires_at__lte=timezone.now(),
    )
    count = 0
    for convo in expired.iterator():
        convo.close()
        count += 1
    if count:
        logger.info("close_expired_conversations: closed %s conversations", count)


_MEDIA_MAX_ATTEMPTS = 5


def _ext_for_mime(mime: str) -> str:
    if not mime:
        return ".bin"
    return mimetypes.guess_extension(mime.split(";")[0].strip()) or ".bin"


@shared_task
def download_media():
    """Pull inbound media from Meta into the configured Django storage backend.

    Inbound webhooks only carry a ``media_id``; the bytes must be fetched via a
    short-lived provider URL. A row that keeps failing is retired after
    ``_MEDIA_MAX_ATTEMPTS`` so it stops being re-selected every run.
    """
    from apps.whatsapp.providers import WhatsAppProviderError

    pending = (
        MessageLog.objects.filter(
            direction=MessageLog.Direction.INBOUND,
            media_id__isnull=False,
            media_file="",
            media_attempts__lt=_MEDIA_MAX_ATTEMPTS,
        )
        .select_related("account")[:_MEDIA_BATCH]
    )

    downloaded = failed = 0
    providers: dict = {}
    for log in pending:
        try:
            provider = providers.get(log.account_id)
            if provider is None:
                provider = _get_provider_for_account(log.account)
                providers[log.account_id] = provider
            if provider is None:
                raise RuntimeError("No WhatsApp provider available for account.")

            meta = provider.get_media_url(log.media_id)
            if (
                meta.size_bytes
                and meta.size_bytes > settings.WHATSAPP_MAX_MEDIA_BYTES
            ):
                raise RuntimeError(
                    f"Media {meta.size_bytes}B exceeds "
                    f"WHATSAPP_MAX_MEDIA_BYTES ({settings.WHATSAPP_MAX_MEDIA_BYTES})"
                )

            content = provider.download_media(meta.url)
            if len(content) > settings.WHATSAPP_MAX_MEDIA_BYTES:
                raise RuntimeError("Downloaded media exceeds size limit")

            ext = _ext_for_mime(meta.media_type or log.media_mime_type or "")
            name = f"whatsapp/{log.account_id}/{log.message_id or log.pk}{ext}"
            saved = default_storage.save(name, ContentFile(content))

            log.media_file = saved
            log.media_url = meta.url
            log.media_mime_type = log.media_mime_type or meta.media_type
            log.media_size = len(content)
            log.media_error = ""
            log.media_attempts = log.media_attempts + 1
            log.save(update_fields=[
                "media_file", "media_url", "media_mime_type", "media_size",
                "media_error", "media_attempts",
            ])
            downloaded += 1
        except Exception as exc:
            log.media_attempts = log.media_attempts + 1
            log.media_error = str(exc)[:500]
            log.save(update_fields=["media_attempts", "media_error"])
            logger.warning(
                "download_media: failed for MessageLog pk=%s (attempt %s): %s",
                log.pk, log.media_attempts, exc,
            )
            failed += 1

    if downloaded or failed:
        logger.info(
            "download_media: downloaded=%s failed=%s", downloaded, failed
        )


# --- Failure-spike alerting -------------------------------------------------

_WA_SPIKE_WINDOW_MINUTES = 60
_WA_SPIKE_MIN_VOLUME = 30
_WA_SPIKE_THRESHOLD = 0.20  # 20% of terminal sends FAILED
_WA_SPIKE_COOLDOWN_SECONDS = 3600


@shared_task
def alert_on_whatsapp_failure_spike() -> dict:
    """Page operators when outbound WhatsApp sends start failing en masse.

    Watches terminal OutboundMessage states over a short trailing window across
    the whole platform — a token expiry, a Meta outage, an account pause.
    Mirrors apps.email.tasks.alert_on_failure_spike.
    """
    since = timezone.now() - timedelta(minutes=_WA_SPIKE_WINDOW_MINUTES)
    terminal = OutboundMessage.objects.filter(
        updated_at__gte=since,
        status__in=[OutboundMessage.Status.SENT, OutboundMessage.Status.FAILED],
    )
    total = terminal.count()
    failed = terminal.filter(status=OutboundMessage.Status.FAILED).count()
    rate = (failed / total) if total else 0.0
    result = {"total": total, "failed": failed, "rate": round(rate, 4), "alerted": False}

    if total < _WA_SPIKE_MIN_VOLUME or rate < _WA_SPIKE_THRESHOLD:
        return result

    if cache.get("whatsapp_failure_spike_alerted"):
        return result  # within cooldown
    cache.set("whatsapp_failure_spike_alerted", "1", _WA_SPIKE_COOLDOWN_SECONDS)
    result["alerted"] = True

    logger.error(
        "WHATSAPP FAILURE SPIKE: %d/%d terminal sends FAILED (%.1f%%) in the last %d min",
        failed, total, rate * 100, _WA_SPIKE_WINDOW_MINUTES,
    )
    try:
        from apps.billing.slack import post_message

        post_message(
            f":rotating_light: WhatsApp failure spike — {failed}/{total} sends FAILED "
            f"({rate:.0%}) in the last {_WA_SPIKE_WINDOW_MINUTES} min. Check the Meta "
            f"Cloud API / access-token status."
        )
    except Exception:
        logger.exception("whatsapp failure-spike Slack alert failed")
    return result


# --- Meta template sync --------------------------------------------------

_META_TEMPLATE_STATUS_MAP = {
    "APPROVED": MessageTemplate.ApprovalStatus.APPROVED,
    "PENDING": MessageTemplate.ApprovalStatus.PENDING,
    "IN_APPEAL": MessageTemplate.ApprovalStatus.PENDING,
    "PENDING_DELETION": MessageTemplate.ApprovalStatus.PENDING,
    "REJECTED": MessageTemplate.ApprovalStatus.REJECTED,
    "PAUSED": MessageTemplate.ApprovalStatus.PAUSED,
    "DISABLED": MessageTemplate.ApprovalStatus.PAUSED,
}

_META_TEMPLATE_STATUS_MAP["FLAGGED"] = MessageTemplate.ApprovalStatus.PAUSED

_VALID_TEMPLATE_CATEGORIES = {c[0] for c in MessageTemplate.Category.choices}


def _handle_template_status_update(event: WebhookEventLog) -> None:
    """Apply Meta `message_template_status_update` webhooks to the local rows.

    Template events carry the WABA id (``entry.id``), not a phone_number_id. Templates
    are only ever matched within the account(s) that own that WABA, so a same-named
    template in another tenant can never be updated.
    """
    entries = [
        (entry, change)
        for entry in (event.payload or {}).get("entry") or []
        for change in entry.get("changes") or []
    ]
    _for_each_item(entries, _process_template_status_change)


def _process_template_status_change(entry: dict, change: dict) -> None:
    value = change.get("value", {})
    name = value.get("message_template_name")
    language = value.get("message_template_language") or "en"
    raw = (value.get("event") or value.get("new_status") or "").upper()
    mapped = _META_TEMPLATE_STATUS_MAP.get(raw)
    if not name or not mapped:
        return

    waba_id = entry.get("id")
    account_ids = (
        list(WhatsAppBusinessNumber.objects.filter(waba_id=waba_id).values_list("account_id", flat=True))
        if waba_id else []
    )
    if not account_ids:
        raise TenantResolutionError(f"No WhatsApp account owns WABA id {waba_id!r}")

    scoped = MessageTemplate.objects.filter(account_id__in=account_ids)
    updated = scoped.filter(whatsapp_template_name=name, language_code=language).update(
        approval_status=mapped
    )
    if not updated:
        scoped.filter(whatsapp_template_name=name).update(approval_status=mapped)


def _upsert_meta_template(account, tpl: dict) -> None:
    name = tpl.get("name")
    if not name:
        return
    language = tpl.get("language") or "en"
    status = _META_TEMPLATE_STATUS_MAP.get(
        (tpl.get("status") or "").upper(), MessageTemplate.ApprovalStatus.PENDING
    )
    category = (tpl.get("category") or "").lower()
    if category not in _VALID_TEMPLATE_CATEGORIES:
        category = MessageTemplate.Category.UTILITY

    MessageTemplate.objects.update_or_create(
        account=account,
        whatsapp_template_name=name,
        language_code=language,
        defaults={"approval_status": status, "category": category},
        create_defaults={
            "approval_status": status,
            "category": category,
            "name": name,
            "content": "",
        },
    )


def sync_templates_for_account(account) -> dict:
    """Pull template approval status from Meta for one account's active numbers.

    Public, synchronous entry point (not a Celery task) for the "Sync now"
    button on the WhatsApp Templates page — a business owner triggering this
    themselves needs an immediate result, not a fire-and-forget queue.
    ``sync_templates`` (the periodic task, below) calls this per account
    instead of duplicating the Meta-API loop.
    """
    from apps.whatsapp.providers import get_whatsapp_provider, WhatsAppProviderError

    numbers = (
        WhatsAppBusinessNumber.objects.filter(account=account, is_active=True)
        .exclude(waba_id__isnull=True)
        .exclude(waba_id="")
    )
    synced = errors = 0
    seen_wabas: set = set()
    for number in numbers:
        if number.waba_id in seen_wabas:
            continue
        seen_wabas.add(number.waba_id)
        try:
            provider = get_whatsapp_provider(account)
            for tpl in provider.list_templates(number.waba_id):
                _upsert_meta_template(account, tpl)
                synced += 1
        except (WhatsAppProviderError, NotImplementedError) as exc:
            logger.warning("sync_templates_for_account: waba=%s failed: %s", number.waba_id, exc)
            errors += 1
    return {"synced": synced, "errors": errors}


@shared_task
def sync_templates() -> dict:
    """Pull template approval status from Meta into local MessageTemplate rows,
    across every account — the periodic (Celery beat) version of
    ``sync_templates_for_account``."""
    numbers = (
        WhatsAppBusinessNumber.objects.filter(is_active=True)
        .exclude(waba_id__isnull=True)
        .exclude(waba_id="")
        .select_related("account")
    )
    seen_accounts: dict = {}
    for number in numbers:
        seen_accounts.setdefault(number.account_id, number.account)

    synced = errors = 0
    for account in seen_accounts.values():
        result = sync_templates_for_account(account)
        synced += result["synced"]
        errors += result["errors"]

    if synced or errors:
        logger.info("sync_templates: synced=%s errors=%s", synced, errors)
    return {"synced": synced, "errors": errors}
