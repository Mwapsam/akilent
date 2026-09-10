import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.models import Account
from apps.accounts.utils import get_current_account, is_ajax
from apps.email import dnscheck
from apps.email.verification import refresh_domain
from apps.email.models import (
    EmailApiKey,
    EmailDomain,
    EmailMessage,
    EmailTrackingEvent,
    EmailTrackingToken,
    SmtpCredential,
    SuppressionListEntry,
    UnsubscribeToken,
    WebhookDelivery,
    WebhookEndpoint,
)
from apps.email.providers import MailProviderError
from apps.email.services import DomainService, SmtpCredentialService

logger = logging.getLogger(__name__)


def _is_admin(request) -> bool:
    return bool(getattr(request.user, "is_superuser", False))


def _scoped(manager, request, account):
    qs = manager.all()
    if not _is_admin(request):
        qs = qs.filter(account=account)
    return qs


# --- AJAX helpers -------------------------------------------------------------

def _toast(response, kind: str, message: str):
    from urllib.parse import quote
    response["X-Toast"] = f"{kind}|{quote(message)}"
    return response


def _ajax_error(message: str, status: int = 400):
    return JsonResponse({"error": message}, status=status)


def _domain_card(request, record):
    from django.conf import settings
    return render(request, "email/_domain_card.html", {
        "d": record,
        "is_admin": _is_admin(request),
        "email_apis_enabled": _require_email_apis(request, record.account),
        "smtp_relay_host": settings.SMTP_RELAY_HOST,
        "smtp_relay_port": settings.SMTP_RELAY_PORT,
    })


_MSG_LEVEL = {"success": messages.SUCCESS, "warning": messages.WARNING, "danger": messages.ERROR}


# --- Dashboard ----------------------------------------------------------------

@login_required
def domains_list(request):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    from django.db.models import Prefetch

    domains = (
        _scoped(EmailDomain.objects, request, account)
        .select_related("account")
        .prefetch_related(
            Prefetch(
                "smtp_credentials",
                queryset=SmtpCredential.objects.filter(is_active=True),
            )
        )
    )
    recent = _scoped(EmailMessage.objects, request, account)[:25]
    api_key = (
        EmailApiKey.objects.filter(account=account, is_active=True).first()
        if account
        else None
    )
    new_api_key_plaintext = request.session.pop("new_api_key", None) if account else None
    new_smtp_secret = request.session.pop("new_smtp_secret", None) if account else None
    from apps.billing.limits import LimitChecker

    email_apis_enabled = admin or (account and LimitChecker(account).has_feature("email_apis"))

    from django.conf import settings

    return render(request, "email/domains.html", {
        "account": account,
        "is_admin": admin,
        "domains": domains,
        "api_key": api_key,
        "new_api_key_plaintext": new_api_key_plaintext,
        "new_smtp_secret": new_smtp_secret,
        "recent": recent,
        "email_apis_enabled": email_apis_enabled,
        "smtp_relay_host": settings.SMTP_RELAY_HOST,
        "smtp_relay_port": settings.SMTP_RELAY_PORT,
        "accounts": Account.objects.order_by("company_name") if admin else None,
    })


@login_required
def domain_detail(request, pk):
    """Full management page for one sending domain: DNS records, verification,
    enable/disable, delete, and SMTP relay credentials."""
    from django.conf import settings

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    record = get_object_or_404(
        _scoped(EmailDomain.objects, request, account).select_related("account"), pk=pk
    )
    return render(request, "email/domain_detail.html", {
        "d": record,
        "account": account,
        "is_admin": admin,
        "email_apis_enabled": _require_email_apis(request, record.account),
        "new_smtp_secret": request.session.pop("new_smtp_secret", None),
        "smtp_relay_host": settings.SMTP_RELAY_HOST,
        "smtp_relay_port": settings.SMTP_RELAY_PORT,
    })


@login_required
@require_POST
def domain_create(request):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    ajax = is_ajax(request)
    domain = (request.POST.get("domain") or "").strip().lower()
    if not domain:
        if ajax:
            return _ajax_error("Domain is required.")
        messages.error(request, "Domain is required.")
        return redirect("email-domains")

    if EmailDomain.objects.filter(domain=domain).exists():
        msg = "That domain is already connected to another account."
        if ajax:
            return _ajax_error(msg)
        messages.error(request, msg)
        return redirect("email-domains")

    if account is None:
        account = Account.objects.filter(pk=request.POST.get("account_id")).first()
        if account is None:
            msg = "Select an account to attach this domain to."
            if ajax:
                return _ajax_error(msg)
            messages.error(request, msg)
            return redirect("email-domains")

    record = EmailDomain.objects.create(account=account, domain=domain)
    record.ensure_verification_token()
    record.save(update_fields=["verify_record_name", "verify_record_value"])

    kind, message = "success", f"{domain} added — add the DNS records on this page, then run the DNS check."
    try:
        DomainService(account, actor=request.user).provision(record)
    except MailProviderError as exc:
        record.status = EmailDomain.Status.FAILED
        record.save(update_fields=["status"])
        logger.error("domain_create: mail server error for %s: %s", domain, exc)
        kind, message = "danger", (
            f"We couldn't finish setting up {domain} — our team has been "
            "notified. You can retry from this page."
        )

    # Adding the first sending domain clears the "domain_setup" onboarding
    # stage; DNS verification / API key remain as checklist items.
    from apps.accounts import onboarding as ob

    ob.advance_onboarding(account)

    detail_url = reverse("email-domain-detail", args=[record.pk])
    messages.add_message(request, _MSG_LEVEL[kind], message)
    if ajax:
        return JsonResponse({"redirect": detail_url})
    return redirect(detail_url)


@login_required
@require_POST
def domain_verify(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    record = get_object_or_404(_scoped(EmailDomain.objects, request, account), pk=pk)
    ajax = is_ajax(request)

    was_verified = record.is_verified
    results = refresh_domain(record)
    newly_verified = record.is_verified and not was_verified

    if record.is_verified:
        missing = [r["label"] for r in record.dns_records() if r["required"] and not r["ok"]]
        if newly_verified:
            kind, message = "success", f"{record.domain} verified — you're ready to send."
        elif missing:
            kind, message = "warning", f"Ownership confirmed, but {', '.join(missing)} isn't live in DNS yet."
        else:
            kind, message = "success", f"{record.domain}: all records look good."
    elif not results["verify"]:
        kind, message = "warning", (
            "We couldn't find your verification record yet. DNS changes can take "
            "a while to propagate — we'll keep checking automatically."
        )
    else:
        kind, message = "success", "DNS status updated."

    if ajax:
        return _toast(_domain_card(request, record), kind, message)
    messages.add_message(request, _MSG_LEVEL[kind], message)
    return redirect("email-domain-detail", pk=record.pk)


@login_required
@require_POST
def domain_toggle(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    record = get_object_or_404(_scoped(EmailDomain.objects, request, account), pk=pk)
    ajax = is_ajax(request)
    new_active = not record.is_active
    try:
        service = DomainService(record.account, actor=request.user)
        if new_active:
            service.enable(record)
        else:
            service.disable(record)
        kind = "success"
        message = f"{record.domain} {'enabled' if new_active else 'disabled'}."
    except MailProviderError as exc:
        kind, message = "danger", f"Could not update {record.domain}: {exc}"

    if ajax:
        return _toast(_domain_card(request, record), kind, message)
    messages.add_message(
        request, messages.SUCCESS if kind == "success" else messages.ERROR, message
    )
    return redirect("email-domain-detail", pk=record.pk)


@login_required
@require_POST
def domain_delete(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    record = get_object_or_404(_scoped(EmailDomain.objects, request, account), pk=pk)
    ajax = is_ajax(request)
    try:
        DomainService(record.account, actor=request.user).deprovision(record)
    except MailProviderError as exc:
        msg = f"Could not delete {record.domain}: {exc}"
        if ajax:
            return _ajax_error(msg)
        messages.error(request, msg)
        return redirect("email-domains")
    domain_name = record.domain
    record.delete()
    messages.success(request, f"{domain_name} deleted.")
    if ajax:
        return JsonResponse({"redirect": reverse("email-domains")})
    return redirect("email-domains")


@login_required
@require_POST
def key_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    EmailApiKey.objects.filter(account=account).update(is_active=False)
    _, raw_key = EmailApiKey.create_for_account(account, name="default")
    request.session["new_api_key"] = raw_key
    messages.success(request, "New API key generated — copy it now, it won't be shown again.")
    return redirect("email-domains")

# --- SMTP relay credentials (now SES-backed) -----------------------------------

def _require_email_apis(request, account) -> bool:
    if _is_admin(request):
        return True
    from apps.billing.limits import LimitChecker
    return LimitChecker(account).has_feature("email_apis")


@login_required
@require_POST
def smtp_create(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    domain = get_object_or_404(_scoped(EmailDomain.objects, request, account), pk=pk)
    _back = redirect("email-domain-detail", pk=domain.pk)
    if not domain.is_verified:
        messages.error(request, f"{domain.domain} must be verified before creating an SMTP relay credential.")
        return _back
    if not _require_email_apis(request, domain.account):
        messages.error(request, "Your plan does not include the email API & SMTP relay. Upgrade to enable it.")
        return _back
    if domain.smtp_credentials.filter(is_active=True).exists():
        messages.error(request, f"{domain.domain} already has an active SMTP relay credential.")
        return _back

    try:
        credential, secret = SmtpCredentialService(domain.account, actor=request.user).provision(domain)
    except Exception as exc:
        messages.error(request, f"Could not create SMTP relay credential: {exc}")
        return _back

    request.session["new_smtp_secret"] = {"credential_id": credential.pk, "secret": secret}
    messages.success(request, "SMTP relay credential created — copy the password now, it won't be shown again.")
    return _back


@login_required
@require_POST
def smtp_rotate(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    credential = get_object_or_404(_scoped(SmtpCredential.objects, request, account), pk=pk)
    detail_url = reverse("email-domain-detail", args=[credential.domain_id])
    try:
        secret = SmtpCredentialService(credential.account, actor=request.user).rotate(credential)
    except Exception as exc:
        messages.error(request, f"Could not rotate SMTP relay credential: {exc}")
    else:
        request.session["new_smtp_secret"] = {"credential_id": credential.pk, "secret": secret}
        messages.success(request, "SMTP relay password rotated — copy it now, it won't be shown again.")

    if is_ajax(request):
        return JsonResponse({"redirect": detail_url})
    return redirect(detail_url)


@login_required
@require_POST
def smtp_revoke(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    credential = get_object_or_404(_scoped(SmtpCredential.objects, request, account), pk=pk)
    detail_url = reverse("email-domain-detail", args=[credential.domain_id])
    try:
        SmtpCredentialService(credential.account, actor=request.user).revoke(credential)
        messages.success(request, f"SMTP relay credential for {credential.username} revoked.")
    except Exception as exc:
        messages.error(request, f"Could not revoke SMTP relay credential: {exc}")

    if is_ajax(request):
        return JsonResponse({"redirect": detail_url})
    return redirect(detail_url)


# --- Webhooks -------------------------------------------------------------------

@login_required
def webhooks_list(request):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    endpoints = _scoped(WebhookEndpoint.objects, request, account)
    deliveries = WebhookDelivery.objects.filter(
        endpoint__in=endpoints
    ).select_related("endpoint")[:50]
    from apps.billing.limits import LimitChecker

    webhooks_enabled = admin or (account and LimitChecker(account).has_feature("outbound_webhooks"))

    return render(request, "email/webhooks.html", {
        "account": account,
        "is_admin": admin,
        "endpoints": endpoints,
        "deliveries": deliveries,
        "webhooks_enabled": webhooks_enabled,
        "event_choices": WebhookEndpoint.EVENT_CHOICES,
    })


@login_required
@require_POST
def webhook_create(request):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    if not admin:
        from apps.billing.limits import LimitChecker
        if not LimitChecker(account).has_feature("outbound_webhooks"):
            messages.error(request, "Your plan does not include outbound webhooks. Upgrade to enable them.")
            return redirect("email-webhooks")

    url = (request.POST.get("url") or "").strip()
    valid_events = {slug for slug, _ in WebhookEndpoint.EVENT_CHOICES}
    event_types = [e for e in request.POST.getlist("event_types") if e in valid_events]

    if not url:
        messages.error(request, "A URL is required.")
        return redirect("email-webhooks")
    if not event_types:
        messages.error(request, "Select at least one event to subscribe to.")
        return redirect("email-webhooks")

    WebhookEndpoint.objects.create(account=account, url=url, event_types=event_types)
    messages.success(request, "Webhook endpoint created.")
    return redirect("email-webhooks")


@login_required
@require_POST
def webhook_delete(request, pk):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    endpoint = get_object_or_404(_scoped(WebhookEndpoint.objects, request, account), pk=pk)
    endpoint.delete()
    messages.success(request, "Webhook endpoint deleted.")
    return redirect("email-webhooks")


@login_required
@require_POST
def webhook_redeliver(request, pk):
    from apps.email.tasks import deliver_webhook

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    delivery = get_object_or_404(
        WebhookDelivery.objects.filter(endpoint__in=_scoped(WebhookEndpoint.objects, request, account)),
        pk=pk,
    )
    delivery.status = WebhookDelivery.Status.PENDING
    delivery.save(update_fields=["status"])
    transaction.on_commit(lambda: deliver_webhook.delay(delivery.pk))
    messages.success(request, "Redelivery queued.")
    next_url = request.POST.get("next") or ""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("email-webhooks")


# --- Insights -----------------------------------------------------------------

def _build_engagement_stats(domain_name: str) -> list[dict]:
    """Return per-day open/click counts for a verified domain."""
    from django.db.models import Count
    from django.db.models.functions import TruncDate

    rows = (
        EmailTrackingEvent.objects
        .filter(message__domain__domain=domain_name)
        .annotate(day=TruncDate("occurred_at"))
        .values("day", "kind")
        .annotate(count=Count("id"))
        .order_by("day")
    )

    # Pivot into [{day, opens, clicks}]
    pivot: dict = {}
    for row in rows:
        day = str(row["day"])
        entry = pivot.setdefault(day, {"day": day, "opens": 0, "clicks": 0})
        if row["kind"] == EmailTrackingEvent.Kind.OPEN:
            entry["opens"] += row["count"]
        else:
            entry["clicks"] += row["count"]
    return list(pivot.values())


@login_required
def insights(request):
    from apps.billing.limits import LimitChecker

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    has_analytics = admin or (account and LimitChecker(account).has_feature("detailed_analytics"))
    domains = list(
        _scoped(EmailDomain.objects, request, account).filter(
            status=EmailDomain.Status.VERIFIED
        )
    )
    domain_names = [d.domain for d in domains]
    selected = request.GET.get("domain") or (domain_names[0] if domain_names else "")

    logs, stats, error, deliverability = [], [], None, None
    if has_analytics and selected and selected in domain_names:
        log_qs = EmailMessage.objects.filter(domain__domain=selected)
        if not admin and account:
            log_qs = log_qs.filter(account=account)
        logs = list(log_qs.order_by("-created_at")[:100])
        stats = _build_engagement_stats(selected)
        if account:
            from apps.email.services.deliverability import compute_score

            sel_domain = next((d for d in domains if d.domain == selected), None)
            try:
                deliverability = compute_score(account, sel_domain).as_dict()
            except Exception:
                logger.exception("insights: deliverability score failed")

    return render(request, "email/insights.html", {
        "account": account,
        "is_admin": admin,
        "has_analytics": has_analytics,
        "domains": domains,
        "selected": selected,
        "logs": logs,
        "stats": stats,
        "error": error,
        "deliverability": deliverability,
    })


# --- Open / click tracking endpoints ------------------------------------------
# These are unauthenticated GET endpoints hit by mail clients, not logged-in
# users. No CSRF needed (GET-only). Fail silently so broken tokens don't 500.

_TRANSPARENT_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
    b"\x00\x00\x02\x02D\x01\x00;"
)


def tracking_open(request, token: str):
    """Record an open event and return a 1×1 transparent GIF."""
    try:
        t = EmailTrackingToken.objects.select_related("message").get(token=token)
        # Dual-write: EmailTrackingEvent still feeds the legacy engagement
        # aggregation; MessageEvent is the new source of truth and owns the
        # outbound-webhook fan-out.
        EmailTrackingEvent.objects.create(
            message=t.message,
            kind=EmailTrackingEvent.Kind.OPEN,
            ip=request.META.get("REMOTE_ADDR"),
            ua=(request.META.get("HTTP_USER_AGENT") or "")[:512],
        )
        from apps.logs.services import record_message_event

        record_message_event(
            t.message,
            "opened",
            source="tracking_pixel",
            data={
                "to": t.recipient,
                "ip": request.META.get("REMOTE_ADDR"),
                "ua": (request.META.get("HTTP_USER_AGENT") or "")[:512],
            },
        )
    except EmailTrackingToken.DoesNotExist:
        pass
    return HttpResponse(_TRANSPARENT_GIF, content_type="image/gif")


def tracking_click(request, token: str):
    """Record a click event and redirect to the original URL."""
    destination = "/"
    try:
        t = EmailTrackingToken.objects.select_related("message").get(token=token)
        destination = t.url or "/"
        EmailTrackingEvent.objects.create(
            message=t.message,
            kind=EmailTrackingEvent.Kind.CLICK,
            url=t.url,
            ip=request.META.get("REMOTE_ADDR"),
            ua=(request.META.get("HTTP_USER_AGENT") or "")[:512],
        )
        from apps.logs.services import record_message_event

        record_message_event(
            t.message,
            "clicked",
            source="tracking_link",
            data={
                "to": t.recipient,
                "url": t.url,
                "ip": request.META.get("REMOTE_ADDR"),
                "ua": (request.META.get("HTTP_USER_AGENT") or "")[:512],
            },
        )
    except EmailTrackingToken.DoesNotExist:
        pass
    return redirect(destination)


@csrf_exempt
def unsubscribe(request, token: str):
    """Process an unsubscribe request from a token link.

    Marks the token as used and adds the recipient to the suppression list.
    Returns a confirmation page.
    """
    from apps.email.services.suppression import record_event

    email = None
    try:
        with transaction.atomic():
            unsub_token = UnsubscribeToken.objects.select_related("account").get(
                token=token, is_used=False
            )
            email = unsub_token.email
            account = unsub_token.account

            # Mark token as used
            unsub_token.is_used = True
            unsub_token.used_at = timezone.now()
            unsub_token.save(update_fields=["is_used", "used_at"])

            # Add to suppression list
            record_event(
                account=account,
                email=email,
                reason="unsubscribe",
            )
            logger.info("Unsubscribed %s via token %s", email, token[:8])
    except UnsubscribeToken.DoesNotExist:
        logger.warning("Unsubscribe token not found or already used: %s", token[:8])
    except Exception:
        logger.exception("Error processing unsubscribe token: %s", token[:8])

    return render(
        request,
        "email/unsubscribe_confirmed.html",
        {"email": email or "unknown", "success": email is not None},
    )


# --- Transactional send API ---------------------------------------------------

def _authenticate(request):
    header = request.headers.get("X-Api-Key") or ""
    if not header:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            header = auth.removeprefix("Bearer ").strip()
    if not header:
        return None
    return EmailApiKey.authenticate(header)


@csrf_exempt
@require_POST
def api_send(request):
    """Deprecated shim for POST /api/v1/messages (apps.api.views.MessageCreateView).

    Kept working so integrations built against the original path don't break
    mid-rollout; shares its actual send logic with the versioned endpoint via
    apps.api.services so the two can never drift out of the same behavior.
    """
    from apps.api.services import UnverifiedDomainError, create_and_queue_message
    from apps.billing.limits import PlanLimitExceeded

    api_key = _authenticate(request)
    if api_key is None:
        return JsonResponse({"error": "Invalid or missing API key"}, status=401)

    account = api_key.account

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    from_email = (body.get("from") or "").strip()
    to_email = (body.get("to") or "").strip()
    subject = body.get("subject") or ""
    text_body = body.get("text") or ""
    html_body = body.get("html") or ""

    if not from_email or not to_email:
        return JsonResponse({"error": "'from' and 'to' are required"}, status=400)

    try:
        msg = create_and_queue_message(
            account=account,
            from_email=from_email,
            to_email=to_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except UnverifiedDomainError as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    except PlanLimitExceeded as exc:
        return JsonResponse({"error": str(exc)}, status=403)

    api_key.touch()
    response = JsonResponse({"id": msg.id, "status": msg.status}, status=202)
    response["Deprecation"] = "true"
    response["Link"] = '</api/v1/messages>; rel="successor-version"'
    return response


# --- Templates ------------------------------------------------------------


@login_required
def templates_list(request):
    from apps.billing.limits import LimitChecker
    from apps.email.models import EmailTemplate
    from apps.email.starter_templates import STARTER_TEMPLATES

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    templates_enabled = admin or (account and LimitChecker(account).has_feature("email_templates"))
    templates = list(_scoped(EmailTemplate.objects, request, account).filter(is_active=True))
    for t in templates:
        t.sample_variables_json = json.dumps(t.sample_variables or {})

    starters = [
        {
            "name": s["name"],
            "subject": s["subject"],
            "text_body": s["text_body"],
            "html_body": s["html_body"],
        }
        for s in STARTER_TEMPLATES
    ]

    return render(request, "email/templates.html", {
        "account": account,
        "is_admin": admin,
        "templates": templates,
        "templates_enabled": templates_enabled,
        "starter_templates_json": json.dumps(starters),
    })


@login_required
@require_POST
def template_create(request):
    from django.utils.text import slugify

    from apps.billing.limits import LimitChecker
    from apps.email.models import EmailTemplate

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    if not admin and not LimitChecker(account).has_feature("email_templates"):
        messages.error(request, "Your plan does not include email templates. Upgrade to enable them.")
        return redirect("email-templates")

    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "A name is required.")
        return redirect("email-templates")

    base_slug = (request.POST.get("slug") or "").strip() or slugify(name) or "template"
    slug = base_slug
    counter = 2
    while EmailTemplate.objects.filter(account=account, slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    EmailTemplate.objects.create(
        account=account,
        name=name,
        slug=slug,
        subject=request.POST.get("subject") or "",
        text_body=request.POST.get("text_body") or "",
        html_body=request.POST.get("html_body") or "",
    )
    messages.success(request, "Template created.")
    return redirect("email-templates")


@login_required
def template_edit_form(request, pk):
    from apps.email.models import EmailTemplate

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    from apps.email.services import flatten_variable_paths

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)
    mode = request.GET.get("mode") or template.builder_mode
    if mode not in (EmailTemplate.BuilderMode.RAW, EmailTemplate.BuilderMode.BLOCKS):
        mode = EmailTemplate.BuilderMode.RAW

    merge_tag_paths = flatten_variable_paths(template.sample_variables) if template.sample_variables else []

    builder_config = json.dumps({
        "html": template.html_body,
        "projectData": template.content_blocks or {},
        "mergeTags": merge_tag_paths,
        "saveUrl": f"/email/templates/{template.pk}/edit/",
        "previewUrl": f"/email/templates/{template.pk}/preview/",
        "sendTestUrl": f"/email/templates/{template.pk}/send-test/",
        "uploadUrl": "/email/templates/assets/upload/",
        "csrfToken": get_token(request),
        "name": template.name,
        "subject": template.subject,
        "sampleVariables": template.sample_variables or {},
        "updatedAt": template.updated_at.isoformat(),
        "mode": mode,
    })

    return render(request, "email/template_edit.html", {
        "account": account,
        "is_admin": admin,
        "template": template,
        "mode": mode,
        "builder_config": builder_config,
        "merge_tag_paths_json": json.dumps(merge_tag_paths),
        "sample_variables_json": json.dumps(template.sample_variables or {}, indent=2),
    })


@login_required
@require_POST
def template_edit(request, pk):
    import json as json_module

    from apps.email.models import EmailTemplate, EmailTemplateVersion

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)

    autosave = request.POST.get("autosave") == "1" or is_ajax(request)

    if not autosave:
        from apps.email.services.versions import snapshot_version

        snapshot_version(
            template,
            created_by=request.user if request.user.is_authenticated else None,
        )

    template.name = (request.POST.get("name") or template.name).strip()
    template.subject = request.POST.get("subject") or ""
    template.text_body = request.POST.get("text_body") or ""
    template.html_body = request.POST.get("html_body") or ""

    update_fields = ["name", "subject", "text_body", "html_body", "updated_at"]

    content_blocks_raw = request.POST.get("content_blocks")
    if content_blocks_raw is not None:
        try:
            template.content_blocks = json_module.loads(content_blocks_raw)
        except json_module.JSONDecodeError:
            pass
        else:
            update_fields.append("content_blocks")

    sample_variables_raw = request.POST.get("sample_variables")
    if sample_variables_raw is not None:
        try:
            template.sample_variables = json_module.loads(sample_variables_raw)
        except json_module.JSONDecodeError:
            pass
        else:
            update_fields.append("sample_variables")

    builder_mode = request.POST.get("builder_mode")
    if builder_mode in (EmailTemplate.BuilderMode.RAW, EmailTemplate.BuilderMode.BLOCKS):
        template.builder_mode = builder_mode
        update_fields.append("builder_mode")

    template.save(update_fields=update_fields)

    if autosave:
        return JsonResponse({"saved": True, "updated_at": template.updated_at.isoformat()})

    messages.success(request, "Template updated.")
    return redirect("email-templates")


@login_required
@require_POST
def template_version_restore(request, pk, version_pk):
    from apps.email.models import EmailTemplate, EmailTemplateVersion

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)
    version = get_object_or_404(EmailTemplateVersion, pk=version_pk, template=template)

    # Snapshot the current state before overwriting so restoring is itself
    # undoable, consistent with how template_edit snapshots before every save.
    from apps.email.services.versions import snapshot_version

    snapshot_version(
        template, created_by=request.user if request.user.is_authenticated else None
    )

    template.subject = version.subject
    template.text_body = version.text_body
    template.html_body = version.html_body
    template.content_blocks = version.content_blocks
    template.save(update_fields=["subject", "text_body", "html_body", "content_blocks", "updated_at"])

    messages.success(request, "Version restored.")
    mode = request.POST.get("mode") or template.builder_mode
    return redirect(f"/email/templates/{template.pk}/?mode={mode}")


@login_required
@require_POST
def template_clone(request, pk):
    from django.utils.text import slugify

    from apps.billing.limits import LimitChecker
    from apps.email.models import EmailTemplate

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    if not admin and not LimitChecker(account).has_feature("email_templates"):
        messages.error(request, "Your plan does not include email templates. Upgrade to enable them.")
        return redirect("email-templates")

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)

    base_slug = slugify(f"{template.slug}-copy")
    slug = base_slug
    counter = 2
    while EmailTemplate.objects.filter(account=template.account, slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    EmailTemplate.objects.create(
        account=template.account,
        name=f"{template.name} (copy)",
        slug=slug,
        subject=template.subject,
        text_body=template.text_body,
        html_body=template.html_body,
        content_blocks=template.content_blocks,
        builder_mode=template.builder_mode,
        sample_variables=template.sample_variables,
    )
    messages.success(request, "Template duplicated.")
    return redirect("email-templates")


@login_required
@require_POST
def template_delete(request, pk):
    from apps.email.models import EmailTemplate

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)
    template.is_active = False
    template.save(update_fields=["is_active"])
    messages.success(request, "Template deleted.")
    return redirect("email-templates")


@login_required
def template_preview(request, pk):
    from types import SimpleNamespace

    from apps.email.models import EmailTemplate
    from apps.email.services import render_template, validate_variables

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return JsonResponse({"error": "Not found"}, status=404)

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)

    if request.method == "POST":
        # Draft preview: render unsaved in-editor content (not the saved DB
        # state) so the live-preview pane reflects what the user is typing.
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            payload = {}
        draft = SimpleNamespace(
            subject=payload.get("subject", template.subject),
            text_body=payload.get("text_body", template.text_body),
            html_body=payload.get("html_body", template.html_body),
        )
        variables = payload.get("variables") or template.sample_variables
        subject, text_body, html_body = render_template(draft, variables)
        missing_variables = validate_variables(draft, variables)
    else:
        try:
            variables = json.loads(request.GET.get("variables") or "{}")
        except json.JSONDecodeError:
            variables = template.sample_variables
        variables = variables or template.sample_variables
        subject, text_body, html_body = render_template(template, variables)
        missing_variables = validate_variables(template, variables)

    return JsonResponse({
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "missing_variables": missing_variables,
    })


@login_required
@require_POST
def template_send_test(request, pk):
    """Send the current draft to the logged-in user's own address.

    to_email is always request.user.email — never client-supplied — so this
    can't become an open spam relay. Reuses create_and_queue_message
    (apps.api.services) for the actual send, but without template_id so it
    sends the pre-rendered draft as-is rather than re-rendering the saved
    template from the DB.
    """
    from types import SimpleNamespace

    from django.core.cache import cache

    from apps.api.services import create_and_queue_message
    from apps.billing.limits import PlanLimitExceeded
    from apps.email.exceptions import UnverifiedDomainError
    from apps.email.models import EmailDomain, EmailTemplate
    from apps.email.services import render_template

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Select an account first."}, status=400)

    template = get_object_or_404(_scoped(EmailTemplate.objects, request, account), pk=pk)

    if not request.user.email:
        return JsonResponse({"error": "Your account has no email address to send a test to."}, status=400)

    cooldown_key = f"send_test_cooldown:{request.user.id}:{template.pk}"
    if cache.get(cooldown_key):
        return JsonResponse({"error": "Please wait a few seconds before sending another test."}, status=429)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}
    draft = SimpleNamespace(
        subject=payload.get("subject", template.subject),
        text_body=payload.get("text_body", template.text_body),
        html_body=payload.get("html_body", template.html_body),
    )
    variables = payload.get("variables") or template.sample_variables
    subject, text_body, html_body = render_template(draft, variables)

    domain = EmailDomain.objects.filter(account=account, status=EmailDomain.Status.VERIFIED).first()
    if domain is None:
        return JsonResponse({"error": "Verify a sending domain first."}, status=400)

    try:
        create_and_queue_message(
            account=account,
            from_email=f"test@{domain.domain}",
            to_email=request.user.email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except UnverifiedDomainError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except PlanLimitExceeded as e:
        return JsonResponse({"error": str(e)}, status=403)

    cache.set(cooldown_key, True, timeout=30)
    return JsonResponse({"status": "queued"}, status=202)


@login_required
@require_POST
def template_asset_upload(request):
    """Image upload endpoint for the GrapesJS builder's asset manager.

    Matches GrapesJS's default upload contract: accepts multipart files under
    the `files` field, returns JSON `{"data": [<url>, ...]}` on success. The
    asset manager's default `multiUpload: true` appends a `[]` suffix to the
    field name (`multiUploadSuffix`), so the actual field GrapesJS posts to is
    `files[]`, not `files` — check that first.
    """
    from apps.billing.limits import LimitChecker
    from apps.email.models import EmailTemplateAsset

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return JsonResponse({"error": "Not found"}, status=404)

    if not admin and not LimitChecker(account).has_feature("email_templates"):
        return JsonResponse({"error": "Your plan does not include email templates."}, status=403)

    uploaded = (
        request.FILES.getlist("files[]")
        or request.FILES.getlist("files")
        or request.FILES.getlist("file")
    )
    if not uploaded:
        return JsonResponse({"error": "No file provided."}, status=400)

    urls = []
    for f in uploaded:
        try:
            from PIL import Image

            f.seek(0)
            Image.open(f).verify()
            f.seek(0)
        except Exception:
            return JsonResponse({"error": f"{f.name} is not a valid image."}, status=400)

        asset = EmailTemplateAsset.objects.create(account=account, file=f)
        urls.append(asset.file.url)

    return JsonResponse({"data": urls})


@login_required
def asset_library(request):
    """Browse/search uploaded template images."""
    from apps.email.models import EmailTemplateAsset

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    assets = _scoped(EmailTemplateAsset.objects, request, account)
    query = (request.GET.get("q") or "").strip()
    if query:
        assets = assets.filter(file__icontains=query)

    return render(request, "email/assets.html", {
        "account": account,
        "is_admin": admin,
        "assets": assets,
        "query": query,
    })


@login_required
@require_POST
def asset_delete(request, pk):
    from apps.email.models import EmailTemplateAsset

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    asset = get_object_or_404(_scoped(EmailTemplateAsset.objects, request, account), pk=pk)
    asset.file.delete(save=False)
    asset.delete()
    messages.success(request, "Image deleted.")
    return redirect("email-assets")


# --- Bulk campaigns ---------------------------------------------------------


def _parse_recipients_csv(uploaded_file) -> list[dict]:
    """Parse a small CSV of recipients: first column `to`, remaining columns become variables."""
    import csv
    import io

    text = uploaded_file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    recipients = []
    for row in reader:
        to_email = (row.pop("to", None) or row.pop("email", None) or "").strip()
        if not to_email:
            continue
        recipients.append({"to": to_email, "variables": {k: v for k, v in row.items() if k}})
    return recipients


def _parse_recipients_text(raw: str) -> list[dict]:
    """Parse a pasted list of bare email addresses (newline / comma / space separated)."""
    import re

    seen: set[str] = set()
    recipients: list[dict] = []
    for token in re.split(r"[\s,;]+", raw or ""):
        email = token.strip().strip("<>").lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        recipients.append({"to": email, "variables": {}})
    return recipients


def _render_campaigns_page(request, account, admin, *, form_data=None, errors=None, status=200):
    """Shared render for the campaigns page — used by the list view and by
    campaign_create when it needs to re-show the form with validation errors
    instead of redirecting (which would throw away everything the user typed)."""
    from apps.billing.limits import LimitChecker
    from apps.email.models import BulkEmailCampaign, EmailTemplate

    bulk_enabled = admin or (account and LimitChecker(account).has_feature("bulk_email"))
    campaigns = _scoped(BulkEmailCampaign.objects, request, account)[:50]
    templates = list(
        _scoped(EmailTemplate.objects, request, account).filter(is_active=True)
    )
    verified_domains = list(
        _scoped(EmailDomain.objects, request, account).filter(
            status=EmailDomain.Status.VERIFIED
        )
    )
    wizard_config = {
        "verifiedDomains": [d.domain for d in verified_domains],
        "templates": [
            {
                "id": t.pk,
                "name": t.name,
                "variables": list((t.sample_variables or {}).keys()),
            }
            for t in templates
        ],
        "sendTestUrl": "/email/campaigns/send-test/",
        "sampleCsvUrl": "/email/campaigns/sample.csv",
        "csrfToken": get_token(request),
        "formData": form_data or {},
        "errors": errors or {},
    }

    return render(request, "email/campaigns.html", {
        "account": account,
        "is_admin": admin,
        "campaigns": campaigns,
        "templates": templates,
        "verified_domains": verified_domains,
        "bulk_enabled": bulk_enabled,
        "errors": errors or {},
        "wizard_config": wizard_config,
    }, status=status)


@login_required
def campaigns_list(request):
    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    return _render_campaigns_page(request, account, admin)


@login_required
@require_POST
def campaign_create(request):
    from apps.api.services import (
        RecipientCapExceededError,
        TemplateMissingContentError,
        create_and_queue_campaign,
    )
    from apps.billing.limits import PlanLimitExceeded
    from apps.email.exceptions import UnverifiedDomainError

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    from_email = (request.POST.get("from_email") or "").strip()
    template_id = request.POST.get("template_id") or None
    subject = request.POST.get("subject") or ""
    text_body = request.POST.get("text_body") or ""
    html_body = request.POST.get("html_body") or ""

    form_data = {
        "mode": request.POST.get("mode") or "text",
        "from_local": request.POST.get("from_local") or "",
        "from_domain": request.POST.get("from_domain") or "",
        "template_id": template_id or "",
        "subject": subject,
        "text_body": text_body,
        "recipients_text": request.POST.get("recipients_text") or "",
        "recipients_json": request.POST.get("recipients_json") or "",
    }
    errors: dict[str, str] = {}

    recipients: list[dict] = []
    uploaded = request.FILES.get("recipients_csv")
    recipients_text = (request.POST.get("recipients_text") or "").strip()
    if uploaded:
        try:
            recipients = _parse_recipients_csv(uploaded)
        except Exception:
            errors["recipients"] = (
                "We couldn't read that CSV. Make sure it's a plain .csv file with a header row."
            )
    elif recipients_text:
        recipients = _parse_recipients_text(recipients_text)
        if not recipients:
            errors["recipients"] = "We couldn't find any email addresses in that list."
    else:
        raw_json = request.POST.get("recipients_json") or "[]"
        try:
            parsed = json.loads(raw_json)
            recipients = [
                {"to": r.get("to"), "variables": r.get("variables") or {}}
                for r in parsed
                if r.get("to")
            ]
        except (json.JSONDecodeError, AttributeError):
            errors["recipients"] = "The advanced recipient list isn't valid JSON."

    if not from_email:
        errors["from_email"] = "Choose an address to send this campaign from."
    if not recipients and "recipients" not in errors:
        errors["recipients"] = "Add at least one recipient."

    if not errors:
        try:
            create_and_queue_campaign(
                account=account,
                from_email=from_email,
                template_id=int(template_id) if template_id else None,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                recipients=recipients,
            )
        except UnverifiedDomainError as exc:
            errors["from_email"] = str(exc)
        except (PlanLimitExceeded, RecipientCapExceededError, TemplateMissingContentError) as exc:
            errors["form"] = str(exc)
        else:
            messages.success(request, "Campaign queued.")
            return redirect("email-campaigns")

    return _render_campaigns_page(
        request, account, admin, form_data=form_data, errors=errors, status=400
    )


@login_required
@require_POST
def campaign_send_test(request):
    """Send a one-off preview of an in-progress campaign to the logged-in user.

    to_email is always request.user.email — never client-supplied — so this
    can't be turned into an open relay. Mirrors template_send_test but also
    accepts an inline (plain-text) draft, which is what the campaign wizard
    sends when the user isn't using a saved template.
    """
    from types import SimpleNamespace

    from django.core.cache import cache

    from apps.api.services import create_and_queue_message
    from apps.billing.limits import PlanLimitExceeded
    from apps.email.exceptions import UnverifiedDomainError
    from apps.email.models import EmailTemplate
    from apps.email.services import render_template

    account = get_current_account(request)
    if account is None:
        return _ajax_error("Select an account first.")
    if not request.user.email:
        return _ajax_error("Your account has no email address to send a test to.")

    cooldown_key = f"campaign_send_test_cooldown:{request.user.id}"
    if cache.get(cooldown_key):
        return _ajax_error("Please wait a few seconds before sending another test.", status=429)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}

    from_email = (payload.get("from_email") or "").strip()
    variables = payload.get("variables") or {}
    template_id = payload.get("template_id")

    if template_id:
        try:
            template = _scoped(EmailTemplate.objects, request, account).get(pk=int(template_id))
        except (EmailTemplate.DoesNotExist, ValueError, TypeError):
            return _ajax_error("Pick a valid template first.")
        variables = variables or template.sample_variables
        subject, text_body, html_body = render_template(template, variables)
    else:
        draft = SimpleNamespace(
            subject=payload.get("subject", ""),
            text_body=payload.get("text_body", ""),
            html_body=payload.get("html_body", ""),
        )
        if not (draft.subject or draft.text_body):
            return _ajax_error("Add a subject and message before sending a test.")
        subject, text_body, html_body = render_template(draft, variables)

    if not from_email:
        domain = EmailDomain.objects.filter(
            account=account, status=EmailDomain.Status.VERIFIED
        ).first()
        if domain is None:
            return _ajax_error("Verify a sending domain first.")
        from_email = f"test@{domain.domain}"

    try:
        create_and_queue_message(
            account=account,
            from_email=from_email,
            to_email=request.user.email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except UnverifiedDomainError as e:
        return _ajax_error(str(e))
    except PlanLimitExceeded as e:
        return _ajax_error(str(e), status=403)

    cache.set(cooldown_key, True, timeout=30)
    return JsonResponse({"status": "queued"}, status=202)


@login_required
def campaign_sample_csv(request):
    """A tiny example recipients file the wizard links to as 'Download sample CSV'."""
    sample = "to,first_name\nada@example.com,Ada\ngrace@example.com,Grace\n"
    response = HttpResponse(sample, content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="campaign-recipients-sample.csv"'
    return response


@login_required
def campaign_detail(request, pk):
    from apps.email.models import BulkEmailCampaign, BulkEmailRecipient

    admin = _is_admin(request)
    account = get_current_account(request)
    if account is None and not admin:
        return redirect("dashboard")

    campaign = get_object_or_404(_scoped(BulkEmailCampaign.objects, request, account), pk=pk)
    recipients = campaign.recipients.select_related("message")[:200]

    if is_ajax(request):
        return JsonResponse({
            "status": campaign.status,
            "recipient_count": campaign.recipient_count,
            "queued_count": campaign.queued_count,
            "sent_count": campaign.sent_count,
            "failed_count": campaign.failed_count,
        })

    return render(request, "email/campaign_detail.html", {
        "account": account,
        "is_admin": admin,
        "campaign": campaign,
        "recipients": recipients,
    })
