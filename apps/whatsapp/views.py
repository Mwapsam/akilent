import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts.models import ContactList, CustomAttributeDef
from apps.core.module_gate import module_required
from apps.whatsapp.campaigns import CampaignError, create_and_queue_campaign
from apps.whatsapp.models import MessageTemplate, MessageTemplateAsset, WebhookEventLog, WhatsAppCampaign
from apps.whatsapp.providers import WhatsAppProviderError, get_whatsapp_provider
from apps.whatsapp.starter_templates import STARTER_CATEGORIES
from apps.whatsapp.tasks import process_whatsapp_event, sync_templates_for_account
from apps.whatsapp.template_builder import TemplateBuilderError, create_and_submit_template

logger = logging.getLogger(__name__)


# --- Templates: a manual "Sync from Meta" action. Meta is the source of
# truth for approval/status (see docs/plans); Akilent never lets a business
# edit a template's content locally, since that would silently desync from
# what Meta actually approved. Creating a new template has to happen in Meta
# Business Manager (or a future "submit for approval" API call, not built —
# no evidence yet that a pilot needs it in-app). The list itself lives on the
# WhatsApp tab of /email/templates/ (apps.email.views.templates_list) — one
# "Templates" page, two channels, same pattern as Campaigns. -------------

@login_required
@require_POST
def templates_sync(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    result = sync_templates_for_account(account)
    if result["errors"]:
        messages.error(
            request,
            "Couldn't reach WhatsApp for one or more numbers — check your connection under Connections.",
        )
    else:
        messages.success(request, f"Synced {result['synced']} template(s) from WhatsApp.")
    return redirect("/email/templates/?channel=whatsapp")


# --- One-time codes sent by the business's own app (apps.whatsapp.verification_codes). The page
# gives a WhatsApp-only business what it needs to connect: an approved Authentication template,
# an API key and a filled-in example. ------------------------------------------------------------

@login_required
@module_required("verification_codes")
def codes_setup(request):
    from apps.accounts.api import is_account_admin
    from apps.email.models import EmailApiKey
    from apps.whatsapp import verification_codes
    from apps.whatsapp.models import OutboundMessage

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    recent = (OutboundMessage.objects.filter(account=account, payload__kind=verification_codes.KIND)
              .select_related("contact", "message_log").order_by("-created_at")[:20])
    return render(request, "whatsapp/codes.html", {
        "templates": verification_codes.approved_templates(account),
        "api_key": EmailApiKey.objects.filter(account=account, is_active=True).first(),
        "new_key": request.session.pop("new_api_key", None),
        "can_manage_key": is_account_admin(request.user, account),
        "endpoint": request.build_absolute_uri("/api/v1/whatsapp/verification-codes"),
        "recent": [verification_codes.status_of(m) for m in recent],
    })


@login_required
@module_required("verification_codes")
@require_POST
def codes_key_create(request):
    from apps.accounts.api import is_account_admin
    from apps.email.models import EmailApiKey

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not is_account_admin(request.user, account):
        messages.error(request, "Only an owner or admin can create the API key.")
        return redirect("whatsapp-codes")
    # One key per business, as on the email Domains page: a new key replaces the old one.
    EmailApiKey.objects.filter(account=account).update(is_active=False)
    _, raw_key = EmailApiKey.create_for_account(account, name="default")
    request.session["new_api_key"] = raw_key
    messages.success(request, "New API key created. Copy it now: it won't be shown again.")
    return redirect("whatsapp-codes")


# --- Template creation (amendment to R1.5c follow-up, 2026-09-22): Meta's own
# UI is too complex for a non-technical owner, so Akilent offers a simplified
# builder in front of it. Akilent validates and submits to Meta; Meta stays
# the approval source of truth (apps.whatsapp.template_builder). ------------

def _data_fields_json(account) -> str:
    """Akilent data-field picker ("insert Contact/Order/Payment field") data
    for the variable-row/button/preview JS, grouped the same way a business
    owner already thinks about a message: who it's for (Contact — including
    any custom fields they've defined, e.g. "Membership number"), what it's
    about (Order), how they pay (Payment).

    Client-side autofill only, keyed as "<group>.<field>" (e.g.
    "contact.first_name", "contact.membership_number", "order.number",
    "payment.link") — a custom field's group ("contact_custom") carries an
    explicit "prefix" of "contact" since it's still a Contact field, just
    shown under its own picker heading. Picking one just fills in a
    variable/button row's label/example text with a realistic sample, it
    doesn't bind the template to that field for sending. Runtime
    substitution with the real value still happens at campaign send time via
    apps.whatsapp.campaigns.variable_mapping, which has no UI yet — a
    deliberately separate, not-in-scope gap.
    """
    built_in_contact_fields = [
        {"key": "first_name", "label": "First name", "sample": "Ada"},
        {"key": "last_name", "label": "Last name", "sample": "Smith"},
        {"key": "email", "label": "Email", "sample": "ada@example.com"},
        {"key": "phone", "label": "Phone", "sample": "+15551234567"},
    ]
    custom_contact_fields = [
        {"key": a.key, "label": a.label or a.key, "sample": a.sample_value}
        for a in CustomAttributeDef.objects.filter(account=account).order_by("key")
    ]
    return json.dumps({
        "groups": [
            {"key": "contact", "label": "Contact", "icon": "user", "fields": built_in_contact_fields},
            {
                "key": "contact_custom", "label": "Contact → Custom fields", "icon": "sliders",
                "prefix": "contact", "fields": custom_contact_fields,
            },
            {"key": "order", "label": "Order", "icon": "building", "fields": [
                {"key": "number", "label": "Order number", "sample": "1029"},
                {"key": "total", "label": "Order total", "sample": "49.99"},
            ]},
            {"key": "payment", "label": "Payment", "icon": "card", "fields": [
                {"key": "link", "label": "Payment link", "sample": "https://pay.example.com/1029"},
                {"key": "amount", "label": "Payment amount", "sample": "49.99"},
            ]},
        ],
        "create_custom_field_url": reverse("contacts:create_custom_field"),
        "custom_field_types": list(CustomAttributeDef.Type.choices),
    })


_HEADER_MEDIA_LIMITS = {
    "image": ({"image/jpeg", "image/png"}, 5 * 1024 * 1024),
    "video": ({"video/mp4"}, 16 * 1024 * 1024),
    "document": ({"application/pdf"}, 100 * 1024 * 1024),
}


@login_required
@require_POST
def template_media_upload(request):
    """Uploads a header media file (image/video/document) for the template
    builder: stores it locally (for a preview URL) then immediately uploads
    it to Meta's app-scoped resumable-upload API to get the handle the
    template payload's `example.header_handle` needs. Client-side JSON
    endpoint — the builder form itself never uploads a file directly.
    """
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)

    header_format = request.POST.get("header_format", "")
    limits = _HEADER_MEDIA_LIMITS.get(header_format)
    if limits is None:
        return JsonResponse({"error": "Choose a header type of image, video or document."}, status=400)
    allowed_types, max_bytes = limits

    f = request.FILES.get("file")
    if f is None:
        return JsonResponse({"error": "No file provided."}, status=400)
    if f.content_type not in allowed_types:
        return JsonResponse({"error": f"\"{f.content_type}\" isn't a supported {header_format} type."}, status=400)
    if f.size > max_bytes:
        return JsonResponse({"error": f"File is too large for a {header_format} header."}, status=400)

    asset = MessageTemplateAsset.objects.create(account=account, file=f, content_type=f.content_type)
    try:
        provider = get_whatsapp_provider(account)
        result = provider.upload_template_media(f.read(), f.content_type, filename=f.name)
    except (WhatsAppProviderError, NotImplementedError) as exc:
        asset.delete()
        return JsonResponse({"error": f"Couldn't upload to WhatsApp: {exc}"}, status=400)

    asset.meta_handle = result.handle
    asset.save(update_fields=["meta_handle"])
    return JsonResponse({
        "asset_id": asset.id, "url": asset.file.url, "handle": asset.meta_handle,
        "content_type": asset.content_type,
    })


@login_required
def template_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        labels = [v.strip() for v in request.POST.getlist("variable_label") if v.strip()]
        examples = [v.strip() for v in request.POST.getlist("variable_example") if v.strip()]

        button_type = request.POST.get("button_type", "url").strip().lower()
        button_text = request.POST.get("button_text", "").strip()
        buttons = []
        if button_text or request.POST.get("button_url") or request.POST.get("button_phone_number"):
            button = {"type": button_type, "text": button_text}
            if button_type == "url":
                button["url"] = request.POST.get("button_url", "").strip()
                button["example"] = request.POST.get("button_url_example", "").strip()
            elif button_type in ("phone_number", "voice_call"):
                button["phone_number"] = request.POST.get("button_phone_number", "").strip()
            elif button_type == "copy_code":
                button["example"] = request.POST.get("button_code_example", "").strip()
            buttons = [button]

        header = request.POST.get("header", "")
        header_format = request.POST.get("header_format", "text").strip().lower() or "text"
        if header_format == "none":  # the form's "None" option: a text header left empty
            header_format, header = "text", ""
        header_media = None
        asset_id = request.POST.get("header_media_asset_id", "").strip()
        if header_format != "text" and asset_id.isdigit():
            header_media = MessageTemplateAsset.objects.filter(account=account, pk=int(asset_id)).first()

        try:
            if request.POST.get("category") == "authentication":
                from apps.whatsapp.template_builder import create_and_submit_auth_template

                create_and_submit_auth_template(
                    account, name=request.POST.get("name", "").strip().lower(),
                    language=request.POST.get("language", "en"),
                    security_recommendation=request.POST.get("auth_security") == "on",
                    expiry_minutes=request.POST.get("auth_expiry", "").strip(),
                    button_text=request.POST.get("auth_button_text", ""),
                )
                messages.success(request, "Template submitted to WhatsApp for approval.")
                return redirect("/email/templates/?channel=whatsapp")
            create_and_submit_template(
                account,
                name=request.POST.get("name", "").strip().lower(),
                category=request.POST.get("category", ""),
                language=request.POST.get("language", "en"),
                body=request.POST.get("body", ""),
                header=header,
                footer=request.POST.get("footer", ""),
                variable_labels=labels,
                variable_examples=examples,
                buttons=buttons,
                header_format=header_format,
                header_media=header_media,
            )
        except TemplateBuilderError as exc:
            messages.error(request, str(exc))
            from apps.ai import api as ai_api

            return render(request, "whatsapp/template_create.html", {
                "account": account, "categories": MessageTemplate.Category.choices,
                "languages": _languages(request.POST.get("language")), "ai_on": ai_api.is_available(account),
                "form": request.POST, "starters_json": json.dumps(STARTER_CATEGORIES),
                "initial_variables_json": json.dumps(list(zip(labels, examples))),
                "data_fields_json": _data_fields_json(account),
                "custom_field_types": CustomAttributeDef.Type.choices,
                "media_upload_url": reverse("whatsapp-template-media-upload"),
            })
        messages.success(request, "Template submitted to WhatsApp for approval.")
        return redirect("/email/templates/?channel=whatsapp")

    from apps.ai import api as ai_api

    form, variables, ai_draft = {}, [], None
    draft = ai_api.get_draft(account, request.GET.get("draft"), kind="template") if request.GET.get("draft") else None
    if draft is not None and draft.status in ("ready", "used"):
        fields = draft.result or {}
        form = {k: fields.get(k, "") for k in ("name", "category", "language", "header", "body", "footer")}
        form["header_format"] = "text" if form["header"] else "none"
        variables = [[v.get("label", ""), v.get("example", "")] for v in fields.get("variables") or []]
        ai_draft = {"reasons": fields.get("reasons") or [], "warnings": draft.warnings or []}
        ai_api.mark_draft_used(draft)
    elif request.GET.get("category") in MessageTemplate.Category.values:
        form = {"category": request.GET["category"]}  # e.g. from the One-time codes page
    return render(request, "whatsapp/template_create.html", {
        "account": account, "categories": MessageTemplate.Category.choices, "form": form,
        "starters_json": json.dumps(STARTER_CATEGORIES),
        "initial_variables_json": json.dumps(variables),
        "data_fields_json": _data_fields_json(account),
        "custom_field_types": CustomAttributeDef.Type.choices,
        "media_upload_url": reverse("whatsapp-template-media-upload"),
        "languages": _languages(form.get("language")), "ai_on": ai_api.is_available(account), "ai_draft": ai_draft,
    })


_LANGUAGES = [("en", "English"), ("es", "Spanish"), ("fr", "French"), ("pt_PT", "Portuguese"), ("sw", "Swahili")]


def _languages(current: str | None) -> list:
    """The language menu, plus the draft's language if it's another one Meta supports."""
    from apps.whatsapp.template_lint import META_LANGUAGES

    out = list(_LANGUAGES)
    if current and current not in dict(out) and current in META_LANGUAGES:
        out.append((current, current))
    return out


@login_required
@require_POST
def template_lint(request):
    """Live "Meta may reject this" warnings for the template form. JSON."""
    from apps.whatsapp.template_lint import lint

    warnings = lint(category=request.POST.get("category", ""), language=request.POST.get("language", ""),
                    body=request.POST.get("body", ""), header=request.POST.get("header", ""),
                    footer=request.POST.get("footer", ""))
    return JsonResponse({"ok": True, "warnings": warnings})


# --- Campaigns (R1.5c): create + list + detail. Sending logic lives in
# apps.whatsapp.campaigns, same split as apps.whatsapp.numbers (setup UI) vs
# apps.whatsapp.tasks (sending). This file is the module boundary rule's
# exempted "views.py", so the ContactList import above is allowed here and
# nowhere else in this app. ------------------------------------------------

@login_required
@module_required("whatsapp_campaigns")
def campaign_new(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        contact_list = ContactList.objects.filter(
            account=account, pk=request.POST.get("contact_list")
        ).first()
        if contact_list is None:
            messages.error(request, "Choose a customer list.")
            return redirect("whatsapp-campaign-new")
        try:
            campaign = create_and_queue_campaign(
                account=account, name=request.POST.get("name") or "",
                contact_list=contact_list, template_id=request.POST.get("template_id"),
                created_by=request.user,
            )
        except CampaignError as exc:
            messages.error(request, str(exc))
            return redirect("whatsapp-campaign-new")
        messages.success(request, "Campaign queued — it's sending now.")
        return redirect("whatsapp-campaign-detail", pk=campaign.pk)

    return render(request, "whatsapp/campaign_new.html", {
        "account": account,
        "contact_lists": ContactList.objects.filter(account=account).order_by("name"),
        "templates": MessageTemplate.objects.filter(
            account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        ).order_by("name"),
    })


@login_required
@module_required("whatsapp_campaigns")
def campaign_detail(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    campaign = get_object_or_404(WhatsAppCampaign, account=account, pk=pk)
    return render(request, "whatsapp/campaign_detail.html", {
        "account": account, "campaign": campaign,
    })


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
