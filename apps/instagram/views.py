"""
Instagram views: webhook ingestion + business account connection settings.

Webhook responsibilities:
  GET  — Meta hub.mode / hub.verify_token handshake
  POST — Verify X-Hub-Signature-256; store raw payload; enqueue Celery task.

Settings responsibilities:
  /instagram/accounts/            — list connected Instagram Business Accounts
  /instagram/accounts/connect/    — connect a new account (manual token entry)
  /instagram/accounts/<pk>/edit/  — update credentials for an existing account
  /instagram/accounts/<pk>/delete/ — disconnect an account
"""

import hashlib
import hmac
import json
import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.accounts.utils import get_current_account
from apps.instagram.models.account import (
    InstagramBusinessAccount,
    TenantResolutionError,
    get_instagram_account_for_webhook,
)
from apps.instagram.models.webhook import WebhookEventLog
from apps.instagram.tasks import process_instagram_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings views
# ---------------------------------------------------------------------------


@login_required
def instagram_accounts(request):
    """List all Instagram Business Accounts connected to this tenant."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    iba_list = list(
        InstagramBusinessAccount.objects.filter(account=account).order_by("created_at")
    )
    return render(
        request,
        "instagram/accounts.html",
        {
            "account": account,
            "iba_list": iba_list,
            "webhook_url_hint": request.build_absolute_uri("/instagram/webhook/"),
        },
    )


@login_required
def instagram_account_connect(request):
    """Connect a new Instagram Business Account via manual token entry."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        iba_id = (request.POST.get("instagram_business_account_id") or "").strip()
        page_id = (request.POST.get("page_id") or "").strip()
        access_token = (request.POST.get("access_token") or "").strip()
        name = (request.POST.get("name") or "").strip()
        username = (request.POST.get("username") or "").strip()

        if not iba_id or not access_token:
            messages.error(
                request, "Instagram Business Account ID and access token are required."
            )
            return render(
                request,
                "instagram/account_form.html",
                {"account": account, "form_data": request.POST, "mode": "connect"},
            )

        if (
            InstagramBusinessAccount.objects.filter(
                instagram_business_account_id=iba_id
            )
            .exclude(account=account)
            .exists()
        ):
            messages.error(
                request,
                "That Instagram Business Account ID is already connected to another workspace.",
            )
            return render(
                request,
                "instagram/account_form.html",
                {"account": account, "form_data": request.POST, "mode": "connect"},
            )

        verify_token = secrets.token_hex(32)
        _iba, created = InstagramBusinessAccount.objects.update_or_create(
            account=account,
            instagram_business_account_id=iba_id,
            defaults={
                "page_id": page_id,
                "name": name,
                "username": username,
                "access_token": access_token,
                "token_expired": False,
                "verify_token": verify_token,
                "is_active": True,
            },
        )
        action = "connected" if created else "updated"
        messages.success(
            request,
            f"Instagram account {action}. Copy the verify token below and configure your Meta webhook.",
        )
        return redirect("instagram-accounts")

    return render(
        request,
        "instagram/account_form.html",
        {"account": account, "form_data": {}, "mode": "connect"},
    )


@login_required
def instagram_account_edit(request, pk: int):
    """Update credentials for an existing Instagram Business Account."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    iba = get_object_or_404(InstagramBusinessAccount, pk=pk, account=account)

    if request.method == "POST":
        page_id = (request.POST.get("page_id") or "").strip()
        access_token = (request.POST.get("access_token") or "").strip()
        name = (request.POST.get("name") or "").strip()
        username = (request.POST.get("username") or "").strip()

        iba.page_id = page_id
        iba.name = name
        iba.username = username
        if access_token:
            iba.access_token = access_token
            iba.token_expired = False
        iba.save()
        messages.success(request, "Instagram account updated.")
        return redirect("instagram-accounts")

    return render(
        request,
        "instagram/account_form.html",
        {"account": account, "iba": iba, "form_data": {}, "mode": "edit"},
    )


@login_required
def instagram_account_delete(request, pk: int):
    """Disconnect (delete) an Instagram Business Account."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    iba = get_object_or_404(InstagramBusinessAccount, pk=pk, account=account)

    if request.method == "POST":
        label = iba.username or iba.instagram_business_account_id
        iba.delete()
        messages.success(request, f"Instagram account @{label} disconnected.")
        return redirect("instagram-accounts")

    return render(
        request,
        "instagram/account_confirm_delete.html",
        {"account": account, "iba": iba},
    )


def _verify_signature(request, access_token: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header using the page access token as secret."""
    signature_header = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
    if not signature_header.startswith("sha256="):
        return False
    received = signature_header[7:]
    expected = hmac.new(access_token.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _classify_event(payload: dict) -> str:
    """Classify the Instagram webhook payload into an event type string."""
    try:
        for entry in payload.get("entry", []):
            if entry.get("messaging"):
                return WebhookEventLog.EventType.MESSAGE
            for change in entry.get("changes", []):
                field = change.get("field", "")
                if field == "comments":
                    return WebhookEventLog.EventType.COMMENT
                if field == "mentions":
                    return WebhookEventLog.EventType.MENTION
    except (KeyError, TypeError, AttributeError):
        pass
    return WebhookEventLog.EventType.UNKNOWN


def _deterministic_event_id(
    instagram_account_id: int, payload: dict, event_type: str
) -> str:
    """Build a stable idempotency key for the event."""
    try:
        entries = payload.get("entry", [])
        if entries:
            platform_id = entries[0].get("id", "")
            time_val = entries[0].get("time", "")
            return f"{instagram_account_id}:{platform_id}:{time_val}:{event_type}"
    except (KeyError, TypeError, IndexError):
        pass
    import secrets

    return f"{instagram_account_id}:{event_type}:{secrets.token_hex(8)}"


@method_decorator(csrf_exempt, name="dispatch")
class InstagramWebhookView(View):
    def get(self, request):
        """Meta webhook verification handshake."""
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")

        if mode != "subscribe" or not token:
            return HttpResponse(status=403)

        # Match the verify_token against any active Instagram account
        match = InstagramBusinessAccount.objects.filter(
            verify_token=token, is_active=True
        ).first()
        if not match:
            logger.warning("Instagram webhook: verify_token not recognised")
            return HttpResponse(status=403)

        return HttpResponse(challenge, content_type="text/plain")

    def post(self, request):
        """Receive and store an Instagram webhook event, then enqueue processing."""
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("Instagram webhook: invalid JSON body")
            return HttpResponse(status=200)  # return 200 so Meta doesn't retry

        # Resolve to an InstagramBusinessAccount for HMAC verification
        page_id = _extract_page_id(payload)
        instagram_account = None
        if page_id:
            try:
                instagram_account = get_instagram_account_for_webhook(page_id)
            except TenantResolutionError:
                logger.warning(
                    "Instagram webhook: unknown page_id=%s — storing as unresolved",
                    page_id,
                )

        if instagram_account and instagram_account.access_token:
            if not _verify_signature(request, instagram_account.access_token):
                logger.warning(
                    "Instagram webhook: bad signature from %s",
                    request.META.get("REMOTE_ADDR"),
                )
                return HttpResponse(status=403)

        event_type = _classify_event(payload)
        event_id_key = _deterministic_event_id(
            instagram_account.pk if instagram_account else 0,
            payload,
            event_type,
        )

        # Store raw payload (idempotency enforced by unique constraint)
        from django.db import IntegrityError

        try:
            with transaction.atomic():
                event = WebhookEventLog.objects.create(
                    instagram_account=instagram_account,
                    event_id=event_id_key,
                    event_type=event_type,
                    raw_payload=payload,
                )
        except IntegrityError:
            logger.debug(
                "Instagram webhook: duplicate event_id=%s — skipping", event_id_key
            )
            return HttpResponse(status=200)

        transaction.on_commit(lambda: process_instagram_event.delay(event.pk))
        return HttpResponse(status=200)


def _extract_page_id(payload: dict) -> str:
    """Extract the Facebook Page ID from the webhook payload."""
    try:
        return payload.get("entry", [{}])[0].get("id", "")
    except (IndexError, TypeError, AttributeError):
        return ""
