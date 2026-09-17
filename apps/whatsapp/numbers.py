"""Account-scoped self-service management of WhatsApp Business numbers.

Replaces the former staff-only tenant CRUD: an account owner registers and
manages their own numbers (phone_number_id + access token) from the dashboard.
"""

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account, is_ajax
from apps.whatsapp.models import MessageLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

logger = logging.getLogger(__name__)


@login_required
def numbers_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    numbers = list(
        WhatsAppBusinessNumber.objects.filter(account=account).order_by(
            "phone_number_id"
        )
    )
    embedded_enabled = bool(settings.WHATSAPP_APP_ID and settings.WHATSAPP_CONFIG_ID)

    active_number = next(
        (n for n in numbers if n.is_active and n.access_token), None
    )
    # Embedded-Signup numbers store a PIN once registered on the Cloud API;
    # a manually-entered number legitimately has none.
    needs_registration = bool(
        active_number and active_number.waba_id and not active_number.verification_pin
    )

    try:
        from apps.billing.api import has_feature

        module_enabled = has_feature(account, "whatsapp")
    except Exception:  # pragma: no cover - billing optional in some setups
        module_enabled = False

    inbound_seen = MessageLog.objects.filter(
        account=account, direction=MessageLog.Direction.INBOUND
    ).exists()

    try:
        webhook_url = request.build_absolute_uri(reverse("whatsapp-webhook"))
    except NoReverseMatch:  # urls only mounted when WHATSAPP_ENABLED
        webhook_url = request.build_absolute_uri("/whatsapp/webhook/")

    steps = [
        {
            "label": "Connect a WhatsApp Business number",
            "done": bool(numbers),
            "hint": "Use “Connect with WhatsApp” below, or add one manually.",
        },
        {
            "label": "Number ready to send",
            "done": bool(active_number) and not needs_registration,
            "hint": "An active number with an access token, registered on the Cloud API.",
        },
        {
            "label": "Receiving messages from customers",
            "done": inbound_seen,
            "hint": "Send a message to the number from a phone to confirm inbound works.",
        },
        {
            "label": "WhatsApp enabled on your plan",
            "done": module_enabled,
            "hint": "Managed in billing / by your account admin.",
        },
    ]

    return render(
        request,
        "whatsapp/numbers.html",
        {
            "account": account,
            "numbers": numbers,
            "embedded_enabled": embedded_enabled,
            "wa_app_id": settings.WHATSAPP_APP_ID,
            "wa_config_id": settings.WHATSAPP_CONFIG_ID,
            "wa_graph_version": settings.WHATSAPP_GRAPH_VERSION,
            "steps": steps,
            "onboarding_complete": all(s["done"] for s in steps),
            "active_number": active_number,
            "needs_registration": needs_registration,
            "module_enabled": module_enabled,
            "webhook_url": webhook_url,
            "show_webhook_setup": request.user.is_staff,
            "show_config_detail": request.user.is_staff,
        },
    )


def finish_embedded_connection(request, account, token, phone_number_id, waba_id, business_id):
    """Shared tail of Embedded Signup: register the number and store it.

    Used by both the popup flow (``connect_complete``, which already has a
    ``code``-exchanged ``token`` plus IDs from the ``WA_EMBEDDED_SIGNUP``
    postMessage) and the redirect flow (``connect_redirect_callback``, which
    discovers the IDs via ``discover_waba_and_phone`` instead).

    Returns:
        (ok, payload) where ``payload`` is either ``{"redirect": next_url}``
        on success or ``{"error": msg, "status": code}`` on failure.
    """
    existing = WhatsAppBusinessNumber.objects.filter(
        phone_number_id=phone_number_id
    ).first()
    if existing and existing.account_id != account.pk:
        return False, {
            "error": "This WhatsApp number is already connected to another account.",
            "status": 409,
        }

    if existing is None:
        from apps.billing.limits import LimitChecker, PlanLimitExceeded
        try:
            LimitChecker(account).check_whatsapp_number()
        except PlanLimitExceeded as exc:
            return False, {"error": str(exc), "status": 403}

    import secrets

    from apps.whatsapp.embedded import register_phone_number, subscribe_app_to_waba

    try:
        subscribe_app_to_waba(waba_id, token)
    except Exception as exc:  # best-effort; don't block the connection
        logger.warning("finish_embedded_connection: subscribe failed: %s", exc)

    # Register the number on the Cloud API so it can send (Tech Provider flow).
    pin = f"{secrets.randbelow(1_000_000):06d}"
    try:
        register_phone_number(phone_number_id, token, pin)
    except Exception as exc:  # best-effort; owner can retry from the dashboard
        logger.warning("finish_embedded_connection: phone registration failed: %s", exc)
        pin = None

    WhatsAppBusinessNumber.objects.update_or_create(
        phone_number_id=phone_number_id,
        defaults={
            "account": account,
            "access_token": token,
            "waba_id": waba_id or None,
            "business_id": business_id or None,
            "verification_pin": pin,
        },
    )
    logger.info(
        "finish_embedded_connection: connected number %s for account %s",
        phone_number_id, account.pk,
    )
    from apps.accounts import onboarding as ob
    from apps.accounts.models import Account

    was_onboarding = account.onboarding_state != Account.Onboarding.COMPLETED
    next_url = ob.advance_onboarding(account)
    if not next_url:
        next_url = "/onboarding/" if was_onboarding else "/whatsapp/numbers/"

    if pin is None:
        # A flash message (not a JSON field the JS would have to relay) so the
        # warning survives regardless of where advance_onboarding sends the
        # user next — it may not be back to the numbers page.
        messages.warning(
            request,
            "Connected, but we couldn't finish activating this number for "
            "sending — you may need to retry from the numbers page.",
        )
    return True, {"redirect": next_url}


@login_required
@require_POST
def connect_complete(request):
    """Finish Embedded Signup: exchange the code and store the number.

    Called by the front end after Meta's Embedded Signup popup returns an auth
    ``code`` plus the ``phone_number_id`` / ``waba_id`` session info.
    """
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "No account"}, status=400)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    code = (body.get("code") or "").strip()
    phone_number_id = (body.get("phone_number_id") or "").strip()
    waba_id = (body.get("waba_id") or "").strip()
    business_id = (body.get("business_id") or "").strip()

    if not code or not phone_number_id:
        return JsonResponse(
            {"error": "Onboarding did not return a code and phone number. Try again."},
            status=400,
        )

    from apps.whatsapp.embedded import EmbeddedSignupError, exchange_code_for_token

    try:
        token = exchange_code_for_token(code)
    except EmbeddedSignupError as exc:
        logger.error("connect_complete: token exchange failed: %s", exc)
        return JsonResponse({"error": f"Could not complete onboarding: {exc}"}, status=502)

    ok, payload = finish_embedded_connection(
        request, account, token, phone_number_id, waba_id, business_id
    )
    if not ok:
        return JsonResponse({"error": payload["error"]}, status=payload["status"])
    return JsonResponse({"ok": True, "redirect": payload["redirect"]})


@login_required
def connect_redirect_start(request):
    """Kick off the redirect-based (non-popup) Embedded Signup fallback.

    Avoids the FB JS SDK popup entirely — a full-page redirect to Meta's OAuth
    dialog sidesteps popup blockers and third-party-cookie issues, which are
    the most common causes of the popup flow's opaque "Login blocked or
    cancelled" failure.
    """
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if not (settings.WHATSAPP_APP_ID and settings.WHATSAPP_CONFIG_ID):
        messages.error(request, "WhatsApp connection is not configured.")
        return redirect("whatsapp-numbers")

    import secrets
    from urllib.parse import urlencode

    nonce = secrets.token_urlsafe(24)
    request.session["whatsapp_connect_state"] = nonce

    redirect_uri = request.build_absolute_uri(reverse("whatsapp-connect-redirect-callback"))
    params = {
        "client_id": settings.WHATSAPP_APP_ID,
        "config_id": settings.WHATSAPP_CONFIG_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": nonce,
    }
    oauth_url = (
        f"https://www.facebook.com/{settings.WHATSAPP_GRAPH_VERSION}/dialog/oauth"
        f"?{urlencode(params)}"
    )
    return redirect(oauth_url)


@login_required
def connect_redirect_callback(request):
    """Handle Meta's redirect back from the non-popup Embedded Signup dialog."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    expected_state = request.session.pop("whatsapp_connect_state", None)
    state = request.GET.get("state")
    if not state or not expected_state or state != expected_state:
        messages.error(request, "Connection request expired or was tampered with. Try again.")
        return redirect("whatsapp-numbers")

    error = request.GET.get("error_description") or request.GET.get("error")
    if error:
        messages.error(request, f"Meta sign-in was cancelled: {error}")
        return redirect("whatsapp-numbers")

    code = (request.GET.get("code") or "").strip()
    if not code:
        messages.error(request, "Meta did not return a sign-in code. Try again.")
        return redirect("whatsapp-numbers")

    from apps.whatsapp.embedded import (
        EmbeddedSignupError,
        discover_waba_and_phone,
        exchange_code_for_token,
    )

    try:
        token = exchange_code_for_token(code)
        waba_ids, phone_numbers_by_waba = discover_waba_and_phone(token)
    except EmbeddedSignupError as exc:
        logger.error("connect_redirect_callback: %s", exc)
        messages.error(request, f"Could not complete onboarding: {exc}")
        return redirect("whatsapp-numbers")

    candidates = [
        {"waba_id": waba_id, **phone}
        for waba_id in waba_ids
        for phone in phone_numbers_by_waba.get(waba_id, [])
    ]
    if not candidates:
        messages.error(
            request,
            "No phone number was found on the connected WhatsApp Business Account.",
        )
        return redirect("whatsapp-numbers")

    if len(candidates) > 1:
        # More than one number to choose from — let the owner pick rather than
        # silently connecting the wrong one.
        request.session["whatsapp_connect_token"] = token
        request.session["whatsapp_connect_candidates"] = candidates
        return render(
            request,
            "whatsapp/connect_select_number.html",
            {"candidates": candidates},
        )

    chosen = candidates[0]
    ok, payload = finish_embedded_connection(
        request, account, token, chosen["id"], chosen["waba_id"], None
    )
    if not ok:
        messages.error(request, payload["error"])
        return redirect("whatsapp-numbers")
    return redirect(payload["redirect"])


@login_required
@require_POST
def connect_redirect_select(request):
    """Complete the redirect flow after the owner picked a number (multi-WABA case)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    token = request.session.pop("whatsapp_connect_token", None)
    candidates = request.session.pop("whatsapp_connect_candidates", None)
    phone_number_id = (request.POST.get("phone_number_id") or "").strip()

    chosen = next(
        (c for c in (candidates or []) if c.get("id") == phone_number_id), None
    )
    if not token or not chosen:
        messages.error(request, "Selection expired. Try connecting again.")
        return redirect("whatsapp-numbers")

    ok, payload = finish_embedded_connection(
        request, account, token, chosen["id"], chosen["waba_id"], None
    )
    if not ok:
        messages.error(request, payload["error"])
        return redirect("whatsapp-numbers")
    return redirect(payload["redirect"])


@login_required
def numbers_create(request):
    account = get_current_account(request)
    ajax = is_ajax(request)
    if account is None:
        if ajax:
            return JsonResponse({"error": "No account"}, status=400)
        return redirect("dashboard")

    if request.method != "POST":
        return redirect("whatsapp-numbers")

    def fail(msg, status=400):
        if ajax:
            return JsonResponse({"error": msg}, status=status)
        messages.error(request, msg)
        return redirect("whatsapp-numbers")

    phone_number_id = (request.POST.get("phone_number_id") or "").strip()
    access_token = (request.POST.get("access_token") or "").strip()
    if not phone_number_id:
        return fail("Phone number ID is required.")

    from apps.billing.limits import LimitChecker, PlanLimitExceeded

    try:
        LimitChecker(account).check_whatsapp_number()
    except PlanLimitExceeded as exc:
        return fail(str(exc), status=403)

    if WhatsAppBusinessNumber.objects.filter(phone_number_id=phone_number_id).exists():
        return fail("This phone number ID is already registered.")

    WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id=phone_number_id,
        access_token=access_token or None,
        waba_id=(request.POST.get("waba_id") or "").strip() or None,
        business_id=(request.POST.get("business_id") or "").strip() or None,
        display_number=(request.POST.get("display_number") or "").strip() or None,
    )

    from apps.accounts import onboarding as ob
    from apps.accounts.models import Account

    was_onboarding = account.onboarding_state != Account.Onboarding.COMPLETED
    next_url = ob.advance_onboarding(account)
    if not next_url:
        next_url = "onboarding" if was_onboarding else "whatsapp-numbers"

    if ajax:
        redirect_url = next_url if next_url.startswith("/") else reverse(next_url)
        return JsonResponse({
            "ok": True,
            "redirect": redirect_url,
            "message": f"WhatsApp number {phone_number_id} registered.",
        })

    messages.success(request, f"WhatsApp number {phone_number_id} registered.")
    return redirect(next_url)


@login_required
def numbers_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.method != "POST":
        return redirect("whatsapp-numbers")

    number = get_object_or_404(WhatsAppBusinessNumber, pk=pk, account=account)
    number.delete()
    messages.success(request, "WhatsApp number removed.")
    return redirect("whatsapp-numbers")
