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

from apps.core.utils import is_operator
from apps.accounts.utils import get_current_account, is_ajax
from apps.whatsapp.models import MessageLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.registration import register_number
from apps.whatsapp.setup_errors import (
    SetupError,
    clear_setup_error,
    get_setup_error,
    redirect_with_setup_error,
)

logger = logging.getLogger(__name__)


def _ensure_display_number(numbers):
    """Fill in the number users must message, once, only when it's needed.

    Embedded Signup stores just the phone_number_id, but the "message us first"
    step needs the real number. One best-effort Graph call; failures are ignored.
    """
    from apps.whatsapp.embedded import fetch_display_number

    for n in numbers:
        if n.is_ready and not n.display_number and not n.last_successful_test():
            value = fetch_display_number(n.phone_number_id, n.access_token)
            if value:
                n.display_number = value
                n.save(update_fields=["display_number", "updated_at"])


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
    needs_registration = bool(active_number and not active_number.is_ready)

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

    from apps.whatsapp.health import number_health
    from apps.whatsapp.setup import build_setup_console

    for n in numbers:
        n.health = number_health(n, embedded_enabled=embedded_enabled)

    _ensure_display_number(numbers)

    console = build_setup_console(
        numbers, embedded_enabled=embedded_enabled, inbound_seen=inbound_seen
    )

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
            "console": console,
            "setup_error": get_setup_error(request),
            "onboarding_complete": bool(
                numbers and console.required_complete and inbound_seen and module_enabled
            ),
            "active_number": active_number,
            "needs_registration": needs_registration,
            "module_enabled": module_enabled,
            "webhook_url": webhook_url,
            "show_webhook_setup": is_operator(request.user),
            "show_config_detail": is_operator(request.user),
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

    from apps.whatsapp.embedded import subscribe_app_to_waba
    from apps.whatsapp.registration import register_number

    clear_setup_error(request)

    try:
        subscribe_app_to_waba(waba_id, token)
    except Exception as exc:  # best-effort; don't block the connection
        logger.warning("finish_embedded_connection: subscribe failed: %s", exc)

    number, _created = WhatsAppBusinessNumber.objects.update_or_create(
        phone_number_id=phone_number_id,
        defaults={
            "account": account,
            "access_token": token,
            "waba_id": waba_id or None,
            "business_id": business_id or None,
        },
    )
    logger.info(
        "finish_embedded_connection: connected number %s for account %s",
        phone_number_id, account.pk,
    )

    # Register on the Cloud API so it can send (Tech Provider flow). The outcome
    # is persisted on the number; on failure the user retries from the numbers
    # page without redoing OAuth.
    result = register_number(number)

    from apps.accounts import onboarding as ob

    ob.advance_onboarding(account)  # side effects only; the numbers page is the console
    if not result.ok:
        messages.warning(
            request,
            "WhatsApp connected, but setup isn't finished — we couldn't register "
            "this number yet. Use Retry registration below.",
        )
    return True, {"redirect": "/whatsapp/numbers/"}


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

    existing = WhatsAppBusinessNumber.objects.filter(
        phone_number_id=phone_number_id
    ).first()
    if existing and existing.account_id != account.pk:
        return JsonResponse(
            {"error": "This WhatsApp number is already connected to another account."},
            status=409,
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
        return redirect_with_setup_error(request, SetupError.NOT_CONFIGURED)

    import secrets
    from urllib.parse import urlencode

    clear_setup_error(request)  # a fresh attempt replaces any previous failure
    nonce = secrets.token_urlsafe(24)
    redirect_uri = request.build_absolute_uri(reverse("whatsapp-connect-redirect-callback"))
    request.session["whatsapp_connect_state"] = nonce
    # Stored so the token exchange in connect_redirect_callback can send the
    # exact same redirect_uri Meta saw here — a mismatch (even in scheme,
    # e.g. behind a proxy) makes Meta reject the code exchange.
    request.session["whatsapp_connect_redirect_uri"] = redirect_uri

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
    redirect_uri = request.session.pop("whatsapp_connect_redirect_uri", None)
    state = request.GET.get("state")
    if not state or not expected_state or state != expected_state:
        return redirect_with_setup_error(request, SetupError.STATE_EXPIRED)

    error = request.GET.get("error_description") or request.GET.get("error")
    if error:
        return redirect_with_setup_error(request, SetupError.CANCELLED, str(error))

    code = (request.GET.get("code") or "").strip()
    if not code:
        return redirect_with_setup_error(request, SetupError.NO_CODE)

    from apps.whatsapp.embedded import (
        EmbeddedSignupError,
        discover_waba_and_phone,
        exchange_code_for_token,
    )

    try:
        token = exchange_code_for_token(code, redirect_uri=redirect_uri)
        waba_ids, phone_numbers_by_waba = discover_waba_and_phone(token)
    except EmbeddedSignupError as exc:
        logger.error("connect_redirect_callback: %s", exc)
        return redirect_with_setup_error(
            request, SetupError.TOKEN_EXCHANGE_FAILED, str(exc)
        )

    candidates = [
        {"waba_id": waba_id, **phone}
        for waba_id in waba_ids
        for phone in phone_numbers_by_waba.get(waba_id, [])
    ]
    if not candidates:
        logger.error(
            "connect_redirect_callback: no candidates. waba_ids=%s phone_numbers_by_waba=%s",
            waba_ids, phone_numbers_by_waba,
        )
        return redirect_with_setup_error(
            request, SetupError.NO_PHONE if waba_ids else SetupError.NO_WABA
        )

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
        return redirect_with_setup_error(
            request, SetupError.CONNECT_REJECTED, payload["error"]
        )
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
        return redirect_with_setup_error(request, SetupError.SELECTION_EXPIRED)

    ok, payload = finish_embedded_connection(
        request, account, token, chosen["id"], chosen["waba_id"], None
    )
    if not ok:
        return redirect_with_setup_error(
            request, SetupError.CONNECT_REJECTED, payload["error"]
        )
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

    number = WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id=phone_number_id,
        access_token=access_token or None,
        waba_id=(request.POST.get("waba_id") or "").strip() or None,
        business_id=(request.POST.get("business_id") or "").strip() or None,
        display_number=(request.POST.get("display_number") or "").strip() or None,
    )

    # Same registration path as OAuth and retry. Needs a token to call Meta.
    result = register_number(number) if number.access_token else None

    from apps.accounts import onboarding as ob

    ob.advance_onboarding(account)  # side effects only; the numbers page is the console

    if result is None:
        msg = f"WhatsApp number {phone_number_id} added. Add an access token to finish setup."
    elif result.ok:
        msg = f"WhatsApp number {phone_number_id} registered."
    else:
        msg = f"Number {phone_number_id} added, but registration failed: {result.error}"

    if ajax:
        return JsonResponse({
            "ok": True,
            "redirect": reverse("whatsapp-numbers"),
            "message": msg,
        })

    (messages.success if result and result.ok else messages.warning)(request, msg)
    return redirect("whatsapp-numbers")


@login_required
@require_POST
def numbers_register(request, pk):
    """Retry Cloud API registration. Idempotent: an already-registered number is a no-op."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    number = get_object_or_404(WhatsAppBusinessNumber, pk=pk, account=account)
    result = register_number(number)
    if result.ok:
        messages.success(request, "Number registered — you can now send messages.")
    else:
        messages.error(request, f"Registration failed: {result.error}")
    return redirect("whatsapp-numbers")


@login_required
def numbers_status(request, pk):
    """Polled by the "message us first" step: has the tester messaged us yet?"""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"message_received": False}, status=400)

    from apps.whatsapp.verification import inbound_stage

    number = get_object_or_404(WhatsAppBusinessNumber, pk=pk, account=account)
    try:
        since = float(request.GET.get("since", "")) or None
    except ValueError:
        since = None
    stage = inbound_stage(number, since)
    return JsonResponse({**stage, "message_received": stage["stage"] == "received"})


@login_required
@require_POST
def numbers_verify(request, pk):
    """Send a verification test message; JSON result for the setup card."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error_code": "no_account", "action": "retry",
                             "message": "No account."}, status=400)

    from apps.whatsapp.verification import verify_connection

    number = get_object_or_404(WhatsAppBusinessNumber, pk=pk, account=account)
    result = verify_connection(number, request.POST.get("recipient", ""))
    return JsonResponse(result, status=200 if result["ok"] else 400)


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
