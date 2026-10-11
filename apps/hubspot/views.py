"""HubSpot OAuth and webhook views."""

from __future__ import annotations

import json
import logging
from datetime import UTC

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.utils import get_current_account
from apps.hubspot.models import HubSpotConnection, HubSpotWebhookEvent
from apps.hubspot.signature import SignatureError, verify_v3

logger = logging.getLogger(__name__)

_OAUTH_AUTH_URL = "https://app.hubspot.com/oauth/authorize"
_OAUTH_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
_SCOPES = (
    "crm.objects.contacts.read crm.objects.contacts.write "
    "crm.objects.deals.read crm.objects.deals.write "
    "crm.schemas.contacts.read crm.schemas.deals.read"
)


@login_required
def oauth_connect(request):
    """Start HubSpot OAuth flow."""
    import secrets as _secrets

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    conn, _ = HubSpotConnection.objects.get_or_create(account=account)
    state = _secrets.token_urlsafe(32)
    conn.oauth_state = state
    conn.oauth_state_created_at = timezone.now()
    conn.save(update_fields=["oauth_state", "oauth_state_created_at", "updated_at"])

    client_id = getattr(settings, "HUBSPOT_CLIENT_ID", "")
    if not client_id:
        logger.error("HUBSPOT_CLIENT_ID is not configured")
        from django.contrib import messages

        messages.error(
            request, "HubSpot integration is not configured. Set HUBSPOT_CLIENT_ID."
        )
        return redirect("/settings/integrations/")
    redirect_uri = request.build_absolute_uri("/hubspot/oauth/callback/")
    url = (
        f"{_OAUTH_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={_SCOPES.replace(' ', '%20')}"
        f"&state={state}"
    )
    return redirect(url)


@login_required
@require_GET
def oauth_callback(request):
    """Handle HubSpot OAuth callback."""
    import requests as _req

    code = request.GET.get("code")
    state = request.GET.get("state")

    if not code or not state:
        return HttpResponseBadRequest("Missing code or state")

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        conn = HubSpotConnection.objects.get(account=account)
    except HubSpotConnection.DoesNotExist:
        return HttpResponseBadRequest("No pending OAuth session")

    # Single-use state: reject replays.
    if conn.oauth_state != state or not conn.oauth_state:
        return HttpResponseBadRequest("Invalid OAuth state")

    from datetime import timedelta

    age = (
        timezone.now() - (conn.oauth_state_created_at or timezone.now())
    ).total_seconds()
    if age > 600:
        return HttpResponseBadRequest("OAuth state expired")

    # Consume the state immediately.
    conn.oauth_state = ""
    conn.oauth_state_created_at = None
    conn.save(update_fields=["oauth_state", "oauth_state_created_at", "updated_at"])

    redirect_uri = request.build_absolute_uri("/hubspot/oauth/callback/")
    resp = _req.post(
        _OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": getattr(settings, "HUBSPOT_CLIENT_ID", ""),
            "client_secret": getattr(settings, "HUBSPOT_CLIENT_SECRET", ""),
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        logger.error(
            "HubSpot OAuth callback failed: %s %s", resp.status_code, resp.text[:200]
        )
        return HttpResponseBadRequest("OAuth token exchange failed")

    data = resp.json()
    conn.access_token = data["access_token"]
    conn.refresh_token = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 1800)
    conn.token_expires_at = timezone.now() + timedelta(seconds=expires_in - 60)
    conn.scopes = _SCOPES.split()
    conn.status = HubSpotConnection.Status.ACTIVE

    # Fetch portal ID from HubSpot token info.
    info_resp = _req.get(
        f"https://api.hubapi.com/oauth/v1/access-tokens/{data['access_token']}",
        timeout=30,
    )
    if info_resp.status_code == 200:
        conn.portal_id = str(info_resp.json().get("hub_id", ""))

    conn.save()
    return redirect("/settings/integrations/hubspot/")


@login_required
@require_POST
def oauth_disconnect(request):
    """Disconnect the HubSpot integration."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        conn = HubSpotConnection.objects.get(account=account)
        conn.status = HubSpotConnection.Status.DISCONNECTED
        conn.access_token = None
        conn.refresh_token = None
        conn.save(
            update_fields=["status", "access_token", "refresh_token", "updated_at"]
        )
    except HubSpotConnection.DoesNotExist:
        pass
    return redirect("/settings/integrations/")


@csrf_exempt
@require_POST
def webhook(request):
    """Receive HubSpot webhook events."""
    client_secret = getattr(settings, "HUBSPOT_CLIENT_SECRET", "")
    sig_v3 = request.headers.get("X-HubSpot-Signature-v3", "")
    sig_ts = request.headers.get("X-HubSpot-Request-Timestamp", "")

    if sig_v3:
        try:
            verify_v3(
                client_secret,
                request.method,
                request.build_absolute_uri(),
                request.body,
                sig_v3,
                sig_ts,
            )
        except SignatureError as exc:
            logger.warning("HubSpot webhook signature invalid: %s", exc)
            return HttpResponse(status=401)
    # No signature in test/sandbox? Log and continue if secret is empty.
    elif client_secret:
        return HttpResponse(status=401)

    try:
        events = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    if not isinstance(events, list):
        events = [events]

    for raw in events:
        portal_id = str(raw.get("portalId", ""))
        _ingest_event(portal_id, raw)

    return HttpResponse(status=200)


def _ingest_event(portal_id: str, raw: dict) -> None:
    try:
        conn = HubSpotConnection.objects.get(
            portal_id=portal_id, status=HubSpotConnection.Status.ACTIVE
        )
    except HubSpotConnection.DoesNotExist:
        logger.info("HubSpot webhook: no active connection for portal %s", portal_id)
        return

    event_id = (
        str(raw.get("eventId", ""))
        or f"noid-{raw.get('objectId')}-{raw.get('subscriptionType')}"
    )
    occurred_ms = raw.get("occurredAt", 0)
    from datetime import datetime

    occurred_at = datetime.fromtimestamp(occurred_ms / 1000, tz=UTC)

    subscription_type = raw.get("subscriptionType", "")
    object_type = "contact" if "contact" in subscription_type else "deal"
    object_id = str(raw.get("objectId", ""))
    property_name = raw.get("propertyName", "")
    property_value = raw.get("propertyValue", "")

    event, created = HubSpotWebhookEvent.objects.get_or_create(
        event_id=event_id,
        defaults={
            "account": conn.account,
            "portal_id": portal_id,
            "object_type": object_type,
            "object_id": object_id,
            "property_name": property_name,
            "property_value": property_value,
            "occurred_at": occurred_at,
            "raw_payload": raw,
        },
    )
    if not created:
        return  # Duplicate — already received.

    # Process synchronously (production would use a Celery task).
    from apps.hubspot.inbound import process_webhook_event

    try:
        process_webhook_event(event)
    except Exception as exc:
        logger.error("process_webhook_event failed for %s: %s", event_id, exc)
