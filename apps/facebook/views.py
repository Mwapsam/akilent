"""
Facebook Page webhooks (Messenger + Page comments) and Meta's Facebook Login callbacks.

Webhook:
  GET  — Meta hub.mode / hub.verify_token handshake
  POST — verify X-Hub-Signature-256, store each entry as its own event, enqueue it

Meta callbacks (Facebook Login settings of the Akilent app):
  /facebook/deauthorize/   — a person removed Akilent from their Facebook settings
  /facebook/data-deletion/ — a person asked Meta to delete their data
"""

import hashlib
import hmac
import json
import logging
import secrets
from functools import partial

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.facebook.models.page import TenantResolutionError, get_page_for_webhook
from apps.facebook.models.webhook import FacebookWebhookEventLog
from apps.facebook.tasks import process_facebook_event

logger = logging.getLogger(__name__)


def _verify_signature(request) -> bool:
    """Facebook Login runs on the Akilent (WhatsApp) Meta app, which signs Page webhooks."""
    from apps.core.meta_signature import verify_meta_signature

    candidates = [
        getattr(settings, "WHATSAPP_APP_SECRET", "") or "",
        getattr(settings, "INSTAGRAM_APP_SECRET", "") or "",
    ]
    if not any(candidates):
        if settings.DEBUG:
            return True
        logger.error("Facebook webhook refused: no app secret is configured")
        return False
    return verify_meta_signature(request, candidates)


def _classify_event(payload: dict) -> str:
    try:
        for entry in payload.get("entry", []):
            if entry.get("messaging"):
                return FacebookWebhookEventLog.EventType.MESSAGE
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                if change.get("field") == "feed" and value.get("item") == "comment":
                    return FacebookWebhookEventLog.EventType.COMMENT
    except (AttributeError, TypeError):
        pass
    return FacebookWebhookEventLog.EventType.UNKNOWN


def _deterministic_event_id(account_id: int, payload: dict, event_type: str) -> str:
    """A stable key per delivered item: a message's mid, a comment's id and verb."""
    item_id = ""
    try:
        for entry in payload.get("entry", []):
            for item in entry.get("messaging", []):
                message = item.get("message") or item.get("postback") or {}
                kinds = ",".join(
                    sorted(
                        k for k in item if k not in {"sender", "recipient", "timestamp"}
                    )
                )
                item_id = message.get("mid") or (
                    f"{(item.get('sender') or {}).get('id', '')}:"
                    f"{item.get('timestamp', '')}:{kinds}"
                )
                break
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                if not item_id and value.get("comment_id"):
                    item_id = f"{value['comment_id']}:{value.get('verb', '')}"
                    if value.get("verb") == "edited":
                        item_id += f":{value.get('created_time', '')}:{hash(value.get('message', ''))}"
            if item_id:
                break
    except (AttributeError, TypeError):
        item_id = ""
    if not item_id:
        item_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
    key = f"{account_id}:{event_type}:{item_id}"
    if len(key) > 255:
        key = (
            f"{account_id}:{event_type}:{hashlib.sha256(item_id.encode()).hexdigest()}"
        )
    return key


@method_decorator(csrf_exempt, name="dispatch")
class FacebookWebhookView(View):
    def get(self, request):
        """Meta webhook verification handshake."""
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        verify_token = settings.FACEBOOK_VERIFY_TOKEN
        if (
            mode == "subscribe"
            and verify_token
            and token
            and hmac.compare_digest(token, verify_token)
        ):
            return HttpResponse(challenge, content_type="text/plain")
        logger.warning("Facebook webhook: verify_token mismatch or missing")
        return HttpResponse(status=403)

    def post(self, request):
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("Facebook webhook: invalid JSON body")
            return HttpResponse(status=200)

        if not _verify_signature(request):
            from apps.core.meta_signature import log_signature_failure

            log_signature_failure(request, "Facebook")
            return HttpResponse(status=403)

        if not isinstance(payload, dict) or payload.get("object") != "page":
            return HttpResponse(status=200)  # not a Page webhook: nothing for us

        from apps.core.channels import channel_state

        held = channel_state("facebook") != "enabled" or (
            FacebookWebhookEventLog.objects.filter(
                status=FacebookWebhookEventLog.Status.HELD
            ).exists()
        )
        # Meta batches entries for several Pages into one delivery: each is stored as its
        # own event, resolved to its own business.
        entries = payload.get("entry")
        if isinstance(entries, list) and len(entries) > 1:
            parts = [{**payload, "entry": [entry]} for entry in entries]
        else:
            parts = [payload]
        for part in parts:
            _store_webhook_event(part, held=held)
        return HttpResponse(status=200)


def _store_webhook_event(payload: dict, *, held: bool) -> None:
    page_id = _extract_page_id(payload)
    account_id = None
    if page_id:
        try:
            account_id = get_page_for_webhook(page_id).account_id
        except TenantResolutionError:
            logger.warning("Facebook webhook: unknown page_id=%s", page_id)

    event_type = _classify_event(payload)
    if event_type == FacebookWebhookEventLog.EventType.UNKNOWN:
        return  # reads, deliveries without messages, likes, posts: nothing to do
    event_id = _deterministic_event_id(account_id or 0, payload, event_type)
    try:
        with transaction.atomic():
            event = FacebookWebhookEventLog.objects.create(
                account_id=account_id,
                page_id=page_id,
                event_id=event_id,
                event_type=event_type,
                raw_payload=payload,
                status=(
                    FacebookWebhookEventLog.Status.HELD
                    if held
                    else FacebookWebhookEventLog.Status.RECEIVED
                ),
            )
    except IntegrityError:
        logger.debug("Facebook webhook: duplicate event_id=%s", event_id)
        return
    if not held:
        transaction.on_commit(lambda: process_facebook_event.delay(event.pk))


def _extract_page_id(payload: dict) -> str:
    try:
        return str(payload.get("entry", [{}])[0].get("id", "") or "")
    except (IndexError, TypeError, AttributeError):
        return ""


# ---------------------------------------------------------------------------
# Meta deauthorize / data-deletion callbacks
# ---------------------------------------------------------------------------


def _signed_request_pages(request):
    """The (payload, Page connections) a Meta signed_request names."""
    from apps.instagram.oauth import parse_signed_request
    from apps.publishing.models import FacebookPageConnection

    data = parse_signed_request(request.POST.get("signed_request", ""))
    if data is None:
        return None, []
    user_id = str(data.get("user_id") or "")
    if not user_id:
        return data, []
    return data, list(FacebookPageConnection.objects.filter(fb_user_id=user_id))


@csrf_exempt
@require_POST
def facebook_deauthorize(request):
    """Meta calls this when a person removes Akilent from their Facebook settings."""
    from apps.facebook.services.pages import disconnect_page

    data, conns = _signed_request_pages(request)
    if data is None:
        logger.warning("facebook_deauthorize: invalid signed_request")
        return HttpResponse(status=400)
    for conn in conns:
        # The token is already revoked on Meta's side: nothing to unsubscribe.
        disconnect_page(conn, unsubscribe=False)
    return HttpResponse(status=200)


@csrf_exempt
@require_POST
def facebook_data_deletion(request):
    """Meta's data-deletion request: delete what we hold from this person's Pages."""
    from apps.facebook.services.pages import disconnect_page
    from apps.facebook.tasks import delete_facebook_page_data

    data, conns = _signed_request_pages(request)
    if data is None:
        logger.warning("facebook_data_deletion: invalid signed_request")
        return HttpResponse(status=400)
    code = secrets.token_hex(8)
    for conn in conns:
        account_id, page_id = conn.account_id, conn.page_id
        disconnect_page(conn, unsubscribe=False)
        if page_id:
            transaction.on_commit(
                partial(delete_facebook_page_data.delay, account_id, page_id, code)
            )
    logger.info(
        "facebook_data_deletion: user_id=%s pages=%s code=%s",
        data.get("user_id"),
        [c.pk for c in conns],
        code,
    )
    status_url = request.build_absolute_uri(reverse("data-deletion")) + f"?code={code}"
    return JsonResponse({"url": status_url, "confirmation_code": code})
