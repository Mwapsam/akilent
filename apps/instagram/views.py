"""
Instagram views: webhook ingestion + business account connection settings.

Webhook responsibilities:
  GET  — Meta hub.mode / hub.verify_token handshake
  POST — Verify X-Hub-Signature-256; store raw payload; enqueue Celery task.

Settings responsibilities:
  /instagram/accounts/            — list connected Instagram Business Accounts
  /instagram/accounts/connect/oauth/ — connect via Instagram Business Login (businesses)
  /instagram/accounts/connect/    — manual token entry (operators only)
  /instagram/accounts/<pk>/edit/  — update credentials (operators only)
  /instagram/accounts/<pk>/delete/ — disconnect an account (history is kept)

Meta callbacks (Instagram Business Login settings):
  /instagram/deauthorize/   — a business removed Akilent from its Instagram account
  /instagram/data-deletion/ — a business asked Meta to delete its data
"""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.module_gate import module_required
from apps.core.utils import is_operator
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
@module_required("instagram")
def instagram_accounts(request):
    """List all Instagram Business Accounts connected to this tenant."""
    from apps.instagram.setup_errors import get_setup_error

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    iba_list = list(
        InstagramBusinessAccount.objects.filter(
            account=account, is_active=True
        ).order_by("created_at")
    )
    oauth_enabled = bool(
        getattr(settings, "INSTAGRAM_APP_ID", "")
        and getattr(settings, "INSTAGRAM_APP_SECRET", None)
    )
    return render(
        request,
        "instagram/accounts.html",
        {
            "account": account,
            "iba_list": iba_list,
            "oauth_enabled": oauth_enabled,
            "setup_error": get_setup_error(request),
            # Businesses connect through Instagram login only; pasting tokens and
            # the app-level webhook settings are for Akilent operators.
            "can_manage_tokens": is_operator(request.user),
            "show_webhook_setup": is_operator(request.user),
            "webhook_url_hint": request.build_absolute_uri("/instagram/webhook/"),
            "webhook_verify_token": (
                getattr(settings, "INSTAGRAM_VERIFY_TOKEN", "")
                if is_operator(request.user)
                else ""
            ),
        },
    )


def _require_operator(request) -> None:
    """Manual token entry is an operator tool: a business connects through Instagram login."""
    if not is_operator(request.user):
        raise Http404


@login_required
@module_required("instagram")
def instagram_account_connect(request):
    """Connect a new Instagram Business Account via manual token entry (operators only)."""
    _require_operator(request)
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
        messages.success(request, f"Instagram account {action}.")
        return redirect("instagram-accounts")

    return render(
        request,
        "instagram/account_form.html",
        {"account": account, "form_data": {}, "mode": "connect"},
    )


@login_required
@module_required("instagram")
def instagram_account_edit(request, pk: int):
    """Update credentials for an existing Instagram Business Account (operators only)."""
    _require_operator(request)
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

    iba = get_object_or_404(
        InstagramBusinessAccount, pk=pk, account=account, is_active=True
    )

    if request.method == "POST":
        label = iba.username or iba.instagram_business_account_id
        disconnect_instagram_account(iba, unsubscribe=True)
        messages.success(request, f"Instagram account @{label} disconnected.")
        return redirect("instagram-accounts")

    return render(
        request,
        "instagram/account_confirm_delete.html",
        {"account": account, "iba": iba},
    )


@login_required
@module_required("instagram")
def instagram_connect_oauth_start(request):
    """Kick off Instagram Business Login — redirect to Meta's OAuth dialog."""
    from apps.instagram.setup_errors import (
        SetupError,
        clear_setup_error,
        redirect_with_setup_error,
    )

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if not (
        getattr(settings, "INSTAGRAM_APP_ID", "")
        and getattr(settings, "INSTAGRAM_APP_SECRET", None)
    ):
        return redirect_with_setup_error(request, SetupError.NOT_CONFIGURED)

    clear_setup_error(request)
    nonce = secrets.token_urlsafe(24)
    redirect_uri = request.build_absolute_uri(
        reverse("instagram-connect-oauth-callback")
    )
    request.session["instagram_connect_state"] = nonce
    request.session["instagram_connect_redirect_uri"] = redirect_uri

    from apps.instagram.oauth import SCOPES

    params = {
        "client_id": settings.INSTAGRAM_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": nonce,
    }
    oauth_url = f"https://www.instagram.com/oauth/authorize?{urlencode(params)}"
    return redirect(oauth_url)


@login_required
@module_required("instagram")
def instagram_connect_oauth_callback(request):
    """Handle Meta's redirect back from the Business Login dialog."""
    from apps.instagram.oauth import (
        InstagramOAuthError,
        discover_instagram_account,
        exchange_code_for_token,
        get_long_lived_token,
        subscribe_page_to_webhooks,
    )
    from apps.instagram.setup_errors import (
        SetupError,
        clear_setup_error,
        redirect_with_setup_error,
    )

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    expected_state = request.session.pop("instagram_connect_state", None)
    redirect_uri = request.session.pop("instagram_connect_redirect_uri", None)
    state = request.GET.get("state")
    if not state or not expected_state or state != expected_state:
        return redirect_with_setup_error(request, SetupError.STATE_EXPIRED)

    error = request.GET.get("error_description") or request.GET.get("error")
    if error:
        return redirect_with_setup_error(request, SetupError.CANCELLED, str(error))

    code = (request.GET.get("code") or "").strip()
    if not code:
        return redirect_with_setup_error(request, SetupError.NO_CODE)

    try:
        token = exchange_code_for_token(code, redirect_uri=redirect_uri)
        token, expires_in = get_long_lived_token(token)
        chosen = discover_instagram_account(token)
        chosen["expires_in"] = expires_in
    except InstagramOAuthError as exc:
        logger.error("instagram_connect_oauth_callback: %s", exc)
        return redirect_with_setup_error(
            request, SetupError.TOKEN_EXCHANGE_FAILED, str(exc)
        )

    ok, error_msg = _finish_instagram_oauth(
        request, account, chosen, subscribe_page_to_webhooks
    )
    if not ok:
        return redirect_with_setup_error(
            request, SetupError.CONNECT_REJECTED, error_msg
        )
    clear_setup_error(request)
    return redirect("instagram-accounts")


@login_required
@module_required("instagram")
@require_POST
def instagram_connect_oauth_select(request):
    """Complete the OAuth flow after the owner picks one of multiple accounts."""
    from apps.instagram.oauth import subscribe_page_to_webhooks
    from apps.instagram.setup_errors import (
        SetupError,
        clear_setup_error,
        redirect_with_setup_error,
    )

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    token = request.session.pop("instagram_connect_token", None)
    candidates = request.session.pop("instagram_connect_candidates", None)
    iba_id = (request.POST.get("instagram_business_account_id") or "").strip()

    chosen = next(
        (
            c
            for c in (candidates or [])
            if c.get("instagram_business_account_id") == iba_id
        ),
        None,
    )
    if not token or not chosen:
        return redirect_with_setup_error(request, SetupError.SELECTION_EXPIRED)

    ok, error_msg = _finish_instagram_oauth(
        request, account, chosen, subscribe_page_to_webhooks
    )
    if not ok:
        return redirect_with_setup_error(
            request, SetupError.CONNECT_REJECTED, error_msg
        )
    clear_setup_error(request)
    return redirect("instagram-accounts")


def _finish_instagram_oauth(request, account, chosen: dict, subscribe_fn) -> tuple:
    """Shared tail of the OAuth flow: save the account and subscribe webhooks.

    ``chosen`` is a dict from ``discover_instagram_accounts``.
    Returns (ok: bool, error_message: str).
    """
    iba_id = chosen["instagram_business_account_id"]
    page_id = chosen["page_id"]
    page_access_token = chosen["page_access_token"]

    existing = InstagramBusinessAccount.objects.filter(
        instagram_business_account_id=iba_id
    ).first()
    if existing and existing.account_id != account.pk:
        return False, (
            "That Instagram Business Account is already connected to another workspace."
        )

    try:
        subscribed = subscribe_fn(page_id, page_access_token)
        # For Instagram Business Login accounts (no page_id), subscribe via IG API directly
        if not subscribed and not page_id:
            from apps.instagram.oauth import subscribe_ig_account_to_webhooks

            subscribed = subscribe_ig_account_to_webhooks(iba_id, page_access_token)
    except Exception as exc:
        logger.warning("_finish_instagram_oauth: subscribe failed: %s", exc)
        subscribed = False

    verify_token = secrets.token_hex(32)
    now = timezone.now() if subscribed else None
    expires_in = chosen.get("expires_in")
    token_expires_at = (
        timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None
    )

    InstagramBusinessAccount.objects.update_or_create(
        account=account,
        instagram_business_account_id=iba_id,
        defaults={
            "page_id": page_id,
            "name": chosen.get("name", ""),
            "username": chosen.get("username", ""),
            "access_token": page_access_token,
            "token_expires_at": token_expires_at,
            "token_expired": False,
            "verify_token": verify_token,
            "webhook_subscribed_at": now,
            "is_active": True,
        },
    )

    label = chosen.get("username") or iba_id
    if subscribed:
        messages.success(
            request,
            f"Instagram account @{label} connected and webhooks activated.",
        )
    else:
        messages.warning(
            request,
            f"Instagram account @{label} connected. "
            "We couldn't switch on message delivery yet — try Reconnect, or contact support "
            "if this keeps happening.",
        )
    return True, ""


def _verify_signature(request) -> bool:
    """Verify Meta's X-Hub-Signature-256 header.

    Instagram Business Login webhooks may be signed with the Instagram app secret
    or the Meta app secret, so either is accepted. If neither is configured,
    verification is skipped so development environments without secrets still work.
    """
    from apps.core.meta_signature import verify_meta_signature

    candidates = [
        getattr(settings, "INSTAGRAM_APP_SECRET", "") or "",
        getattr(settings, "WHATSAPP_APP_SECRET", "") or "",
    ]
    if not any(candidates):
        return True
    return verify_meta_signature(request, candidates)


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
    """Build a stable idempotency key for the event: the same delivery always maps to
    the same key, while two different messages never share one.

    Keyed on what Meta identifies the item by (a DM's ``mid``, a comment's id). An
    entry's ``time`` is not unique — two messages in the same second used to collide,
    and the second one was silently dropped as a "duplicate".
    """
    item_id = ""
    try:
        for entry in payload.get("entry", []):
            for item in entry.get("messaging", []):
                message = item.get("message") or {}
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
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                item_id = item_id or value.get("id") or value.get("comment_id") or ""
            if item_id:
                break
    except (AttributeError, TypeError):
        item_id = ""
    if not item_id:
        item_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
    key = f"{instagram_account_id}:{event_type}:{item_id}"
    if len(key) > 255:
        digest = hashlib.sha256(item_id.encode()).hexdigest()
        key = f"{instagram_account_id}:{event_type}:{digest}"
    return key


@method_decorator(csrf_exempt, name="dispatch")
class InstagramWebhookView(View):
    def get(self, request):
        """Meta webhook verification handshake."""
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")

        verify_token = settings.INSTAGRAM_VERIFY_TOKEN
        if (
            mode == "subscribe"
            and verify_token
            and token
            and hmac.compare_digest(token, verify_token)
        ):
            return HttpResponse(challenge, content_type="text/plain")

        logger.warning("Instagram webhook: verify_token mismatch or missing")
        return HttpResponse(status=403)

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

        if not _verify_signature(request):
            from apps.core.meta_signature import log_signature_failure

            log_signature_failure(request, "Instagram")
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


# ---------------------------------------------------------------------------
# Disconnect + Meta deauthorize / data-deletion callbacks
# ---------------------------------------------------------------------------


def disconnect_instagram_account(
    iba: InstagramBusinessAccount, *, unsubscribe: bool
) -> None:
    """Stop using an Instagram account without losing its conversations.

    The row is kept (inactive, token cleared) because deleting it would cascade into
    the business's Instagram conversations and their inbox history. Reconnecting the
    same account later reactivates this row.
    """
    if unsubscribe and iba.access_token:
        from apps.instagram.oauth import unsubscribe_ig_account_from_webhooks

        unsubscribe_ig_account_from_webhooks(
            iba.instagram_business_account_id, iba.access_token
        )
    iba.is_active = False
    iba.access_token = None
    iba.token_expires_at = None
    iba.webhook_subscribed_at = None
    iba.save(
        update_fields=[
            "is_active",
            "access_token",
            "token_expires_at",
            "webhook_subscribed_at",
            "updated_at",
        ]
    )


def _signed_request_account(request):
    """The (payload, InstagramBusinessAccount or None) a Meta signed_request names."""
    from apps.instagram.oauth import parse_signed_request

    data = parse_signed_request(request.POST.get("signed_request", ""))
    if data is None:
        return None, None
    user_id = str(data.get("user_id") or "")
    iba = (
        InstagramBusinessAccount.objects.filter(instagram_business_account_id=user_id)
        .order_by("-is_active")
        .first()
        if user_id
        else None
    )
    return data, iba


@csrf_exempt
@require_POST
def instagram_deauthorize(request):
    """Meta calls this when a business removes Akilent from its Instagram settings."""
    data, iba = _signed_request_account(request)
    if data is None:
        logger.warning("instagram_deauthorize: invalid signed_request")
        return HttpResponse(status=400)
    if iba is not None and iba.is_active:
        # The token is already revoked on Meta's side, so there's nothing to unsubscribe.
        disconnect_instagram_account(iba, unsubscribe=False)
        logger.info("instagram_deauthorize: disconnected instagram_account=%s", iba.pk)
    return HttpResponse(status=200)


@csrf_exempt
@require_POST
def instagram_data_deletion(request):
    """Meta's data-deletion request callback: delete what we hold for this account.

    Responds with the status URL and confirmation code Meta shows the person.
    """
    from apps.instagram.tasks import delete_instagram_account_data

    data, iba = _signed_request_account(request)
    if data is None:
        logger.warning("instagram_data_deletion: invalid signed_request")
        return HttpResponse(status=400)
    code = secrets.token_hex(8)
    if iba is not None:
        disconnect_instagram_account(iba, unsubscribe=False)
        iba_pk = iba.pk
        transaction.on_commit(lambda: delete_instagram_account_data.delay(iba_pk, code))
    logger.info(
        "instagram_data_deletion: user_id=%s instagram_account=%s code=%s",
        data.get("user_id"),
        iba.pk if iba else None,
        code,
    )
    status_url = request.build_absolute_uri(reverse("data-deletion")) + f"?code={code}"
    return JsonResponse({"url": status_url, "confirmation_code": code})
