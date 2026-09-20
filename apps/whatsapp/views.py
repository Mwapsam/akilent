import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.whatsapp.models import WebhookEventLog
from apps.whatsapp.tasks import process_whatsapp_event

logger = logging.getLogger(__name__)


def _verify_meta_signature(request) -> bool:
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    their_digest = header.removeprefix("sha256=")

    expected = hmac.new(
        key=settings.WHATSAPP_APP_SECRET.encode(),
        msg=request.body,  
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(their_digest, expected)


def _classify_event(payload: dict) -> str:
    """One label per POST. A POST may batch several entries/changes, so look at all
    of them: any message makes it "message", else any status makes it "status"."""
    try:
        changes = [c for entry in payload["entry"] for c in entry["changes"]]
        values = [c.get("value") or {} for c in changes]
        if any(v.get("messages") for v in values):
            return "message"
        if any(v.get("statuses") for v in values):
            return "status"
        return changes[0].get("field", "unknown")
    except (KeyError, IndexError, TypeError, AttributeError):
        return "unknown"


@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(View):

    def get(self, request):
        """Meta webhook verification handshake."""
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")

        if mode == "subscribe" and hmac.compare_digest(
            token or "", settings.WHATSAPP_VERIFY_TOKEN
        ):
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse(status=403)

    def post(self, request):
        if not _verify_meta_signature(request):
            logger.warning("WhatsApp webhook: bad signature from %s",
                           request.META.get("REMOTE_ADDR"))
            return HttpResponse(status=403)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("WhatsApp webhook: signed but invalid JSON")
            WebhookEventLog.objects.create(
                source=WebhookEventLog.Source.WHATSAPP,
                event_type="invalid_json",
                payload={"raw": request.body.decode(errors="replace")[:10000]},
                processed=True,  # nothing to process
                error_message="Body was not valid JSON",
            )
            return HttpResponse(status=200)

        event = WebhookEventLog.objects.create(
            source=WebhookEventLog.Source.WHATSAPP,
            event_type=_classify_event(payload),
            payload=payload,
        )

        transaction.on_commit(lambda: process_whatsapp_event.delay(event.id))

        return HttpResponse(status=200)