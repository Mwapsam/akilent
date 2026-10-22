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
from apps.contacts.models import ContactList, CustomAttributeDef, Segment
from apps.core.module_gate import module_required
from apps.whatsapp.campaigns import (
    CampaignError,
    campaign_delivery_stats,
    create_and_queue_campaign,
)
from apps.whatsapp.models import (
    MessageTemplate,
    MessageTemplateAsset,
    WebhookEventLog,
    WhatsAppCampaign,
)
from apps.whatsapp.providers import WhatsAppProviderError, get_whatsapp_provider
from apps.whatsapp.starter_templates import STARTER_CATEGORIES
from apps.whatsapp.tasks import process_whatsapp_event, sync_templates_for_account
from apps.whatsapp.template_builder import (
    TemplateBuilderError,
    create_and_submit_template,
)

logger = logging.getLogger(__name__)


# --- Templates: a manual "Sync from Meta" action. Meta is the source of
# truth for approval/status (see docs/plans); Akilent never lets a business
# edit a template's content locally, since that would silently desync from
# what Meta actually approved. A new template is written here and submitted to
# Meta for approval (template_create → apps.whatsapp.template_builder); it can't
# be edited afterwards, only replaced. The list itself lives on the
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
        messages.success(
            request, f"Synced {result['synced']} template(s) from WhatsApp."
        )
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
    recent = (
        OutboundMessage.objects.filter(
            account=account, payload__kind=verification_codes.KIND
        )
        .select_related("contact", "message_log")
        .order_by("-created_at")[:20]
    )
    return render(
        request,
        "whatsapp/codes.html",
        {
            "templates": verification_codes.approved_templates(account),
            "api_key": EmailApiKey.usable().filter(account=account).first(),
            "new_key": request.session.pop("new_api_key", None),
            "can_manage_key": is_account_admin(request.user, account),
            "endpoint": request.build_absolute_uri(
                "/api/v1/whatsapp/verification-codes"
            ),
            "recent": [verification_codes.status_of(m) for m in recent],
        },
    )


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
    # This page only makes the first key; naming, scopes and revoking live in Settings.
    if EmailApiKey.usable().filter(account=account).exists():
        return redirect("settings-api-keys")
    _, raw_key = EmailApiKey.create_for_account(account, name="One-time codes")
    request.session["new_api_key"] = raw_key
    messages.success(
        request, "New API key created. Copy it now: it won't be shown again."
    )
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
        for a in CustomAttributeDef.objects.filter(
            account=account, entity=CustomAttributeDef.Entity.CONTACT
        ).order_by("key")
    ]
    return json.dumps(
        {
            "groups": [
                {
                    "key": "contact",
                    "label": "Contact",
                    "icon": "user",
                    "fields": built_in_contact_fields,
                },
                {
                    "key": "contact_custom",
                    "label": "Contact → Custom fields",
                    "icon": "sliders",
                    "prefix": "contact",
                    "fields": custom_contact_fields,
                },
                {
                    "key": "order",
                    "label": "Order",
                    "icon": "building",
                    "fields": [
                        {"key": "number", "label": "Order number", "sample": "1029"},
                        {"key": "total", "label": "Order total", "sample": "49.99"},
                    ],
                },
                {
                    "key": "payment",
                    "label": "Payment",
                    "icon": "card",
                    "fields": [
                        {
                            "key": "link",
                            "label": "Payment link",
                            "sample": "https://pay.example.com/1029",
                        },
                        {"key": "amount", "label": "Payment amount", "sample": "49.99"},
                    ],
                },
            ],
            "create_custom_field_url": reverse("contacts:create_custom_field"),
            "custom_field_types": list(CustomAttributeDef.Type.choices),
        }
    )


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
        return JsonResponse(
            {"error": "Choose a header type of image, video or document."}, status=400
        )
    allowed_types, max_bytes = limits

    f = request.FILES.get("file")
    if f is None:
        return JsonResponse({"error": "No file provided."}, status=400)
    if f.content_type not in allowed_types:
        return JsonResponse(
            {"error": f'"{f.content_type}" isn\'t a supported {header_format} type.'},
            status=400,
        )
    if f.size > max_bytes:
        return JsonResponse(
            {"error": f"File is too large for a {header_format} header."}, status=400
        )

    # Read before saving: saving the asset consumes the upload stream, which
    # would leave Meta an empty file.
    content = f.read()
    f.seek(0)
    asset = MessageTemplateAsset.objects.create(
        account=account, file=f, content_type=f.content_type
    )
    try:
        provider = get_whatsapp_provider(account)
        result = provider.upload_template_media(
            content, f.content_type, filename=f.name
        )
    except (WhatsAppProviderError, NotImplementedError) as exc:
        asset.delete()
        return JsonResponse(
            {"error": f"Couldn't upload to WhatsApp: {exc}"}, status=400
        )

    asset.meta_handle = result.handle
    asset.save(update_fields=["meta_handle"])
    return JsonResponse(
        {
            "asset_id": asset.id,
            "url": asset.file.url,
            "handle": asset.meta_handle,
            "content_type": asset.content_type,
        }
    )


@login_required
def template_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        labels = [
            v.strip() for v in request.POST.getlist("variable_label") if v.strip()
        ]
        examples = [
            v.strip() for v in request.POST.getlist("variable_example") if v.strip()
        ]

        button_type = request.POST.get("button_type", "url").strip().lower()
        button_text = request.POST.get("button_text", "").strip()
        buttons = []
        if (
            button_text
            or request.POST.get("button_url")
            or request.POST.get("button_phone_number")
        ):
            button = {"type": button_type, "text": button_text}
            if button_type == "url":
                button["url"] = request.POST.get("button_url", "").strip()
                button["example"] = request.POST.get("button_url_example", "").strip()
            elif button_type == "phone_number":
                button["phone_number"] = request.POST.get(
                    "button_phone_number", ""
                ).strip()
            elif button_type == "copy_code":
                button["example"] = request.POST.get("button_code_example", "").strip()
            buttons = [button]

        header = request.POST.get("header", "")
        header_format = (
            request.POST.get("header_format", "text").strip().lower() or "text"
        )
        if (
            header_format == "none"
        ):  # the form's "None" option: a text header left empty
            header_format, header = "text", ""
        header_media = None
        asset_id = request.POST.get("header_media_asset_id", "").strip()
        if header_format != "text" and asset_id.isdigit():
            header_media = MessageTemplateAsset.objects.filter(
                account=account, pk=int(asset_id)
            ).first()

        try:
            if request.POST.get("category") == "authentication":
                from apps.whatsapp.template_builder import (
                    create_and_submit_auth_template,
                )

                create_and_submit_auth_template(
                    account,
                    name=request.POST.get("name", "").strip().lower(),
                    language=request.POST.get("language", "en"),
                    security_recommendation=request.POST.get("auth_security") == "on",
                    expiry_minutes=request.POST.get("auth_expiry", "").strip(),
                    button_text=request.POST.get("auth_button_text", ""),
                )
                messages.success(
                    request, "Template submitted to WhatsApp for approval."
                )
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
            logger.warning(
                "WhatsApp template %r rejected for account %s: %s",
                request.POST.get("name", ""),
                account.slug,
                exc,
            )
            messages.error(request, str(exc))
            from apps.ai import api as ai_api

            return render(
                request,
                "whatsapp/template_create.html",
                {
                    "account": account,
                    "categories": MessageTemplate.Category.choices,
                    "languages": _languages(request.POST.get("language")),
                    "ai_on": ai_api.is_available(account),
                    "form": request.POST,
                    "starters_json": json.dumps(STARTER_CATEGORIES),
                    "initial_variables_json": json.dumps(list(zip(labels, examples))),
                    "data_fields_json": _data_fields_json(account),
                    "custom_field_types": CustomAttributeDef.Type.choices,
                    "media_upload_url": reverse("whatsapp-template-media-upload"),
                },
            )
        messages.success(request, "Template submitted to WhatsApp for approval.")
        return redirect("/email/templates/?channel=whatsapp")

    from apps.ai import api as ai_api

    form, variables, ai_draft = {}, [], None
    draft = (
        ai_api.get_draft(account, request.GET.get("draft"), kind="template")
        if request.GET.get("draft")
        else None
    )
    if draft is not None and draft.status in ("ready", "used"):
        fields = draft.result or {}
        form = {
            k: fields.get(k, "")
            for k in ("name", "category", "language", "header", "body", "footer")
        }
        form["header_format"] = "text" if form["header"] else "none"
        variables = [
            [v.get("label", ""), v.get("example", "")]
            for v in fields.get("variables") or []
        ]
        ai_draft = {
            "reasons": fields.get("reasons") or [],
            "warnings": draft.warnings or [],
        }
        ai_api.mark_draft_used(draft)
    elif request.GET.get("category") in MessageTemplate.Category.values:
        form = {
            "category": request.GET["category"]
        }  # e.g. from the One-time codes page
    return render(
        request,
        "whatsapp/template_create.html",
        {
            "account": account,
            "categories": MessageTemplate.Category.choices,
            "form": form,
            "starters_json": json.dumps(STARTER_CATEGORIES),
            "initial_variables_json": json.dumps(variables),
            "data_fields_json": _data_fields_json(account),
            "custom_field_types": CustomAttributeDef.Type.choices,
            "media_upload_url": reverse("whatsapp-template-media-upload"),
            "languages": _languages(form.get("language")),
            "ai_on": ai_api.is_available(account),
            "ai_draft": ai_draft,
        },
    )


_LANGUAGES = [
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("pt_PT", "Portuguese"),
    ("sw", "Swahili"),
]


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

    warnings = lint(
        category=request.POST.get("category", ""),
        language=request.POST.get("language", ""),
        body=request.POST.get("body", ""),
        header=request.POST.get("header", ""),
        footer=request.POST.get("footer", ""),
    )
    return JsonResponse({"ok": True, "warnings": warnings})


# --- Campaigns (R1.5c): create + list + detail. Sending logic lives in
# apps.whatsapp.campaigns, same split as apps.whatsapp.numbers (setup UI) vs
# apps.whatsapp.tasks (sending). This file is the module boundary rule's
# exempted "views.py", so the ContactList import above is allowed here and
# nowhere else in this app. ------------------------------------------------


def _campaign_variable_mapping_from_post(request, template) -> tuple[dict, dict, list]:
    """Build (variable_mapping, variable_fallbacks, missing_blanks) from a
    campaign_new.html submission, using the same var__/lit__/fb__ convention
    as automation/views.py::starter_install (templates/automation/workflow_list.html)."""
    from apps.automation import variables as wa_variables

    mapping, fallbacks, missing = {}, {}, []
    for var in template.variables or []:
        key = f"{template.pk}__{var}"
        source, fallback = wa_variables.entry_from_choice(
            request.POST.get(f"var__{key}") or "",
            request.POST.get(f"lit__{key}") or "",
            request.POST.get(f"fb__{key}") or "",
        )
        if source:
            mapping[var] = source
            if fallback:
                fallbacks[var] = fallback
        else:
            missing.append(var)
    return mapping, fallbacks, missing


def _audience_from(src, account):
    """``(key, contact_list, segment)`` for the composer's audience choice.

    The radio is ``audience=list:<pk>`` / ``segment:<pk>``; ``contact_list`` and
    ``?list=`` (deep links from a list page) still work.
    """
    raw = src.get("audience") or ""
    if not raw and (src.get("contact_list") or src.get("list")):
        raw = f"list:{src.get('contact_list') or src.get('list')}"
    if not raw and src.get("segment"):
        raw = f"segment:{src.get('segment')}"
    kind, _, pk = raw.partition(":")
    if not pk.isdigit():
        return "", None, None
    if kind == "list":
        found = ContactList.objects.filter(account=account, pk=pk).first()
        return (raw, found, None) if found else ("", None, None)
    if kind == "segment":
        seg = Segment.objects.filter(account=account, pk=pk).first()
        return (raw, None, seg) if seg else ("", None, None)
    return "", None, None


def _send_time(src, account):
    """``(send_at_utc | None, tz, error)`` from the composer's "When" step."""
    from datetime import datetime, timedelta

    from django.utils import timezone as dj_tz

    from apps.accounts.business_hours import workspace_timezone
    from apps.core.scheduling import InvalidTimezone, to_utc

    if (src.get("when") or "now") != "later":
        return None, "", ""
    tz = src.get("send_tz") or workspace_timezone(account)
    try:
        local = datetime.strptime(
            f"{src.get('send_date', '')} {src.get('send_time', '')}", "%Y-%m-%d %H:%M"
        )
        send_at = to_utc(local, tz)
    except (ValueError, InvalidTimezone):
        return None, tz, "Choose the day and time to send it."
    now = dj_tz.now()
    if send_at <= now + timedelta(minutes=2):
        return None, tz, "That time has already passed. Choose a time in the future."
    if send_at > now + timedelta(days=90):
        return None, tz, "You can schedule up to 90 days ahead."
    return send_at, tz, ""


def _draft_src(campaign) -> dict:
    """A draft's saved choices in the composer's own field names."""
    from zoneinfo import ZoneInfo

    from apps.automation import variables as wa_variables

    by_source = {source: key for key, source, _label, _fb in wa_variables.CHOICES}
    src = {
        "audience": f"segment:{campaign.segment_id}"
        if campaign.segment_id
        else f"list:{campaign.contact_list_id}",
        "template_id": str(campaign.template_id),
        "name": campaign.name,
        "draft_id": str(campaign.pk),
    }
    for var, source in (campaign.variable_mapping or {}).items():
        key = f"{campaign.template_id}__{var}"
        if isinstance(source, str) and source in by_source:
            src[f"var__{key}"] = by_source[source]
            src[f"fb__{key}"] = (campaign.variable_fallbacks or {}).get(var, "")
        else:
            src[f"var__{key}"] = "literal"
            src[f"lit__{key}"] = str(source)
    if campaign.scheduled_at:
        local = campaign.scheduled_at.astimezone(ZoneInfo(campaign.timezone or "UTC"))
        src.update(
            when="later",
            send_date=local.strftime("%Y-%m-%d"),
            send_time=local.strftime("%H:%M"),
            send_tz=campaign.timezone or "UTC",
        )
    return src


def _composer_state(request, account, templates, src=None):
    """The composer's choices, from the POST (re-shown on error), a draft, or the URL."""
    import secrets

    from apps.accounts.business_hours import workspace_timezone
    from apps.automation import variables as wa_variables

    if src is None:
        src = request.POST if request.method == "POST" else request.GET
    picks, lits, fbs = {}, {}, {}
    for t in templates:
        for b in t.blanks:  # type: ignore[attr-defined]
            key = f"{t.pk}__{b['name']}"
            picks[key] = src.get(f"var__{key}") or b["default"]
            lits[key] = src.get(f"lit__{key}") or ""
            fbs[key] = src.get(f"fb__{key}") or ""
    audience, _list, _segment = _audience_from(src, account)
    return {
        "audience": audience,
        "template": src.get("template_id") or "",
        "name": src.get("name") or "",
        "draft_id": src.get("draft_id") or "",
        "when": src.get("when") or "now",
        "send_date": src.get("send_date") or "",
        "send_time": src.get("send_time") or "09:00",
        "send_tz": src.get("send_tz") or workspace_timezone(account),
        "picks": picks,
        "lits": lits,
        "fbs": fbs,
        "submission_key": request.POST.get("submission_key")
        or secrets.token_urlsafe(24),
        "default_choices": {
            f"{t.pk}__{b['name']}": wa_variables.default_choice(b["name"])
            for t in templates
            for b in t.blanks  # type: ignore[attr-defined]
        },
    }


def _composer_templates(account):
    from apps.automation import variables as wa_variables
    from apps.whatsapp.campaign_ui import template_options

    templates = template_options(account)
    for t in templates:
        t.blanks = [  # type: ignore[attr-defined]
            {"name": v, "default": wa_variables.default_choice(v)}
            for v in (t.variables or [])
        ]
    return templates


def _timezone_options(current: str) -> list[str]:
    from apps.accounts.business_hours import COMMON_TIMEZONES

    zones = list(COMMON_TIMEZONES)
    if current and current not in zones:
        zones.insert(0, current)
    return zones


_SEND_NOT_ALLOWED = (
    "Only the business owner or an admin can send campaigns. Save it as a draft "
    "and ask one of them to send it."
)


@login_required
@module_required("whatsapp_campaigns")
def campaign_new(request):
    """The stepped composer (spec S2): who → what → when → review and send."""
    from apps.accounts.api import is_account_admin
    from apps.automation import variables as wa_variables
    from apps.whatsapp.campaign_ui import list_options, readiness, segment_options
    from apps.whatsapp.campaigns import save_draft

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    can_send = is_account_admin(request.user, account)
    templates = _composer_templates(account)
    error = ""
    step = 1
    src = None
    draft = None
    draft_pk = (
        request.POST.get("draft_id")
        if request.method == "POST"
        else request.GET.get("draft")
    )
    if draft_pk and str(draft_pk).isdigit():
        draft = WhatsAppCampaign.objects.filter(
            account=account, pk=draft_pk, status__in=WhatsAppCampaign.EDITABLE
        ).first()
        if draft is None and request.method == "GET":
            messages.info(request, "That campaign has already been sent.")
            return redirect("whatsapp-campaign-detail", pk=draft_pk)
        if draft is not None and request.method == "GET":
            src = _draft_src(draft)
            step = 4

    if request.method == "POST":
        action = request.POST.get("action") or "send"
        _key, contact_list, segment = _audience_from(request.POST, account)
        template = next(
            (
                t
                for t in templates
                if str(t.pk) == request.POST.get("template_id") and t.sendable  # type: ignore[attr-defined]
            ),
            None,
        )
        if contact_list is None and segment is None:
            error = "Choose who to send to."
        elif template is None:
            error, step = "Choose a message to send.", 2
        else:
            variable_mapping, variable_fallbacks, missing = (
                _campaign_variable_mapping_from_post(request, template)
            )
            if missing and action != "draft":
                error, step = "Fill in what goes in every blank of the message.", 2
            elif action == "draft":
                try:
                    campaign = save_draft(
                        account=account,
                        name=request.POST.get("name") or "",
                        template_id=template.pk,
                        contact_list=contact_list,
                        segment=segment,
                        variable_mapping=variable_mapping,
                        variable_fallbacks=variable_fallbacks,
                        created_by=request.user,
                        draft=draft,
                    )
                except CampaignError as exc:
                    error, step = str(exc), 4
                else:
                    messages.success(
                        request,
                        "Draft saved. Nothing has been sent."
                        if can_send
                        else "Draft saved. An owner or admin can review and send it.",
                    )
                    return redirect("whatsapp-campaign-detail", pk=campaign.pk)
            elif not can_send:
                error, step = _SEND_NOT_ALLOWED, 4
            else:
                send_at, tz, when_error = _send_time(request.POST, account)
                if when_error:
                    error, step = when_error, 3
                else:
                    try:
                        campaign = create_and_queue_campaign(
                            account=account,
                            name=request.POST.get("name") or "",
                            contact_list=contact_list,
                            segment=segment,
                            template_id=template.pk,
                            variable_mapping=variable_mapping,
                            variable_fallbacks=variable_fallbacks,
                            created_by=request.user,
                            submission_key=request.POST.get("submission_key") or "",
                            send_at=send_at,
                            tz=tz,
                            draft=draft,
                        )
                    except CampaignError as exc:
                        error, step = str(exc), 4
                    else:
                        _audit_campaign(
                            request,
                            account,
                            campaign,
                            "schedule" if send_at else "send",
                        )
                        if campaign.status == WhatsAppCampaign.Status.SCHEDULED:
                            messages.success(
                                request,
                                "Scheduled. It goes out at the time you chose — you can "
                                "change or cancel it until then.",
                            )
                        else:
                            messages.success(
                                request,
                                "Your campaign is on its way. Results appear below as they arrive.",
                            )
                        return redirect("whatsapp-campaign-detail", pk=campaign.pk)

    state = _composer_state(request, account, templates, src=src)
    if request.method == "GET" and src is None and state["audience"]:
        step = 2 if not state["template"] else 4
    return render(
        request,
        "whatsapp/campaign_new.html",
        {
            "account": account,
            "lists": list_options(account),
            "segments": segment_options(account),
            "templates": templates,
            "blank_choices": [
                {"key": key, "label": label}
                for key, _source, label, _fallback in wa_variables.CHOICES
            ],
            "state": state,
            "draft": draft,
            "error": error,
            "step": step,
            "can_send": can_send,
            "timezones": _timezone_options(state["send_tz"]),
            "test_phone": account.phone,
            "readiness": readiness(account),
        },
        status=400 if error else 200,
    )


def _audit_campaign(request, account, campaign, verb: str, **extra) -> None:
    from apps.core.audit import platform_record

    platform_record(
        action=f"whatsapp_campaign.{verb}",
        account=account,
        actor=request.user,
        resource_type="whatsapp_campaign",
        resource_id=str(campaign.pk),
        metadata={
            "list": campaign.contact_list_id,
            "segment": campaign.segment_id,
            "template": campaign.template_id,
            "recipients": campaign.recipient_count,
            **extra,
        },
    )


class _GetAsPost:
    """_campaign_variable_mapping_from_post reads request.POST; the estimate is a GET."""

    def __init__(self, data):
        self.POST = data


@login_required
@module_required("whatsapp_campaigns")
def campaign_estimate(request):
    """HTMX: who the composer's current choices would reach, plus the preview."""
    from apps.whatsapp.campaign_ui import estimate
    from apps.whatsapp.campaigns import audience_contacts

    account = get_current_account(request)
    if account is None:
        return HttpResponse(status=204)
    _key, contact_list, segment = _audience_from(request.GET, account)
    template = MessageTemplate.objects.filter(
        account=account,
        pk=request.GET.get("template_id") or 0,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    ).first()
    if (contact_list is None and segment is None) or template is None:
        return render(request, "whatsapp/_campaign_estimate.html", {"est": None})

    mapping, fallbacks, missing = _campaign_variable_mapping_from_post(
        _GetAsPost(request.GET), template
    )
    try:
        audience = audience_contacts(
            account, contact_list=contact_list, segment=segment
        )
        est = estimate(account, audience, template, mapping, fallbacks)
    except CampaignError as exc:
        return render(
            request,
            "whatsapp/_campaign_estimate.html",
            {"est": None, "audience_error": str(exc)},
        )
    from apps.whatsapp.delivery import forecast

    send_at, tz, when_error = _send_time(request.GET, account)
    return render(
        request,
        "whatsapp/_campaign_estimate.html",
        {
            "est": est,
            "pace": forecast(account, est.will_receive, now=send_at),
            "template": template,
            "contact_list": contact_list,
            "segment": segment,
            "audience_name": segment.name if segment else contact_list.name,  # type: ignore[union-attr]
            "blanks_missing": missing,
            "is_marketing": template.category == MessageTemplate.Category.MARKETING,
            "send_at": send_at,
            "send_tz": tz,
            "when_error": when_error,
            "can_send": _can_send(request, account),
        },
    )


def _can_send(request, account) -> bool:
    from apps.accounts.api import is_account_admin

    return is_account_admin(request.user, account)


@login_required
@module_required("whatsapp_campaigns")
@require_POST
def campaign_test(request):
    """HTMX: send the composer's message once to the owner's own number."""
    from apps.whatsapp.campaigns import send_test

    account = get_current_account(request)
    if account is None:
        return HttpResponse(status=204)
    template = MessageTemplate.objects.filter(
        account=account,
        pk=request.POST.get("template_id") or 0,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    ).first()
    if template is None:
        result = {"ok": False, "message": "Choose a message first."}
    else:
        mapping, fallbacks, _missing = _campaign_variable_mapping_from_post(
            request, template
        )
        try:
            send_test(
                account=account,
                template=template,
                mapping=mapping,
                fallbacks=fallbacks,
                phone=request.POST.get("test_phone") or "",
                user=request.user,
            )
        except CampaignError as exc:
            result = {"ok": False, "message": str(exc)}
        else:
            result = {
                "ok": True,
                "message": "Test sent. It should arrive on your WhatsApp within a minute. "
                "It isn't counted as a campaign.",
            }
    return render(request, "whatsapp/_campaign_test_result.html", result)


@login_required
@module_required("whatsapp_campaigns")
def campaign_detail(request, pk: int):
    """Spec S3: where the campaign is, what reached people, who didn't get it and why."""
    from django.core.paginator import Paginator
    from django.db.models import Count

    from apps.whatsapp import campaigns as svc
    from apps.whatsapp.campaign_ui import (
        SKIP_REASON_COPY,
        estimate,
        lifecycle_state,
        local_label,
        timeline,
    )
    from apps.whatsapp.models import WhatsAppCampaignRecipient

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    campaign = get_object_or_404(
        WhatsAppCampaign.objects.select_related(
            "template", "contact_list", "segment", "created_by", "retry_of"
        ),
        account=account,
        pk=pk,
    )
    _S = WhatsAppCampaign.Status
    can_send = _can_send(request, account)
    editable = campaign.status in WhatsAppCampaign.EDITABLE

    preview = None
    if editable:
        # Nothing chosen yet: show who it would reach if it went now.
        try:
            audience = (
                None
                if campaign.retry_of_id
                else svc.audience_contacts(
                    account,
                    contact_list=campaign.contact_list,
                    segment=campaign.segment,
                )
            )
            if audience is not None:
                preview = estimate(
                    account,
                    audience,
                    campaign.template,
                    campaign.variable_mapping,
                    campaign.variable_fallbacks,
                )
        except CampaignError:
            preview = None

    processed = campaign.queued_count + campaign.skipped_count
    remaining = max(campaign.recipient_count - processed, 0)
    skip_breakdown = [
        {
            "key": row["skip_reason"],
            "n": row["n"],
            "label": SKIP_REASON_COPY.get(row["skip_reason"], (row["skip_reason"], ""))[
                0
            ],
            "why": SKIP_REASON_COPY.get(row["skip_reason"], ("", ""))[1],
        }
        for row in campaign.recipients.exclude(skip_reason="")
        .values("skip_reason")
        .annotate(n=Count("id"))
        .order_by("-n")
    ]
    delivery = campaign_delivery_stats(campaign)

    outcome = request.GET.get("outcome") or ""
    recipients = campaign.recipients.select_related("contact").order_by("pk")
    _R = WhatsAppCampaignRecipient.Status
    if outcome == "failed":
        recipients = recipients.filter(status=_R.FAILED)
    elif outcome == "not_sent":
        recipients = recipients.filter(status=_R.SKIPPED)
    elif outcome == "sent":
        recipients = recipients.filter(status=_R.QUEUED)
    else:
        outcome = ""
    page = Paginator(recipients, 50).get_page(request.GET.get("page"))

    finished = campaign.status in WhatsAppCampaign.FINISHED
    context = {
        "account": account,
        "campaign": campaign,
        "state": lifecycle_state(campaign),
        "editable": editable,
        "preview": preview,
        "remaining": remaining,
        "progress_pct": round(processed * 100 / campaign.recipient_count)
        if campaign.recipient_count
        else 100,
        "delivery": delivery,
        "reached": delivery["sent"] + delivery["delivered"] + delivery["read"],
        "skip_breakdown": skip_breakdown,
        "skip_copy": SKIP_REASON_COPY,
        "page": page,
        "outcome": outcome,
        "timeline": timeline(campaign),
        "scheduled_local": local_label(campaign.scheduled_at, campaign.timezone),
        "can_send": can_send,
        "can_pause": can_send and svc.can_pause(campaign),
        "can_resume": can_send and campaign.status == _S.PAUSED,
        "can_cancel": can_send and svc.can_cancel(campaign),
        "failed_n": svc.failed_count(campaign) if finished else 0,
        "in_progress": campaign.status in WhatsAppCampaign.ACTIVE
        or (campaign.status == _S.COMPLETED and delivery["pending"] > 0),
    }
    if request.headers.get("HX-Request") and request.GET.get("part") == "live":
        return render(request, "whatsapp/_campaign_live.html", context)
    return render(request, "whatsapp/campaign_detail.html", context)


@login_required
@module_required("whatsapp_campaigns")
def campaign_settings(request):
    """Quiet hours: when campaign messages may arrive (owner/admin)."""
    from datetime import time

    from apps.accounts.business_hours import workspace_timezone
    from apps.whatsapp.delivery import daily_limit, send_window, used_last_24h
    from apps.whatsapp.models import CampaignSendWindow

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    window = send_window(account)
    error = ""
    if request.method == "POST":
        if not _can_send(request, account):
            messages.error(
                request, "Only the business owner or an admin can change this."
            )
            return redirect("whatsapp-campaign-settings")
        try:
            start = time.fromisoformat(request.POST.get("start") or "08:00")
            end = time.fromisoformat(request.POST.get("end") or "20:00")
        except ValueError:
            error = "Choose a start and an end time."
        else:
            if start == end and request.POST.get("enabled") == "on":
                error = "The start and end can't be the same time."
            else:
                CampaignSendWindow.objects.update_or_create(
                    account=account,
                    defaults={
                        "enabled": request.POST.get("enabled") == "on",
                        "start": start,
                        "end": end,
                    },
                )
                messages.success(
                    request, "Saved. New campaign messages follow these hours."
                )
                return redirect("whatsapp-campaign-settings")
    return render(
        request,
        "whatsapp/campaign_settings.html",
        {
            "account": account,
            "window": window,
            "tz": workspace_timezone(account),
            "limit": daily_limit(account),
            "used": used_last_24h(account),
            "can_send": _can_send(request, account),
            "error": error,
        },
        status=400 if error else 200,
    )


_ACTION_DONE = {
    "pause": "Paused. Messages not sent yet are held back until you resume.",
    "resume": "Resumed. The rest of the messages are going out.",
    "duplicate": "Copied as a new draft. Change anything you like, then send it.",
}


@login_required
@module_required("whatsapp_campaigns")
@require_POST
def campaign_action(request, pk: int, action: str):
    """Pause · Resume · Cancel · Send now · Duplicate · Retry failed (spec S3)."""
    from apps.whatsapp import campaigns as svc

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    campaign = get_object_or_404(WhatsAppCampaign, account=account, pk=pk)
    if action != "duplicate" and not _can_send(request, account):
        messages.error(request, "Only the business owner or an admin can do that.")
        return redirect("whatsapp-campaign-detail", pk=pk)

    target = campaign
    try:
        if action == "pause":
            if not svc.pause_campaign(campaign, user=request.user):
                raise CampaignError(
                    "It has already finished, so there's nothing to pause."
                )
            messages.success(request, _ACTION_DONE["pause"])
        elif action == "resume":
            if not svc.resume_campaign(campaign, user=request.user):
                raise CampaignError("This campaign isn't paused.")
            messages.success(request, _ACTION_DONE["resume"])
        elif action == "cancel":
            was_scheduled = campaign.status == WhatsAppCampaign.Status.SCHEDULED
            if not svc.can_cancel(campaign):
                raise CampaignError(
                    "It has already finished, so there's nothing to cancel."
                )
            n = svc.cancel_campaign(campaign, user=request.user)
            messages.success(
                request,
                "Cancelled. Nothing was sent."
                if was_scheduled
                else f"Cancelled. {n:,} customer{'s' if n != 1 else ''} won't get it.",
            )
        elif action == "send_now":
            target = svc.start_campaign(campaign.pk, user=request.user)
            messages.success(
                request,
                "Your campaign is on its way. Results appear below as they arrive.",
            )
        elif action == "duplicate":
            target = svc.duplicate_campaign(campaign, user=request.user)
            messages.success(request, _ACTION_DONE["duplicate"])
            return redirect(f"{reverse('whatsapp-campaign-new')}?draft={target.pk}")
        elif action == "retry":
            target = svc.retry_failed_campaign(campaign, user=request.user)
            messages.success(
                request,
                f"Sending again to the {target.recipient_count:,} customer"
                f"{'s' if target.recipient_count != 1 else ''} whose message failed.",
            )
        else:
            return HttpResponse(status=404)
    except CampaignError as exc:
        messages.error(request, str(exc))
        return redirect("whatsapp-campaign-detail", pk=pk)
    _audit_campaign(request, account, target, action, source=campaign.pk)
    return redirect("whatsapp-campaign-detail", pk=target.pk)


@login_required
@module_required("whatsapp_campaigns")
def campaign_export(request, pk: int):
    """Every recipient and what happened, as a spreadsheet (formula-safe)."""
    import csv

    from apps.core.csv_safe import safe_cell
    from apps.whatsapp.campaigns import recipients_with_outcome

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    campaign = get_object_or_404(WhatsAppCampaign, account=account, pk=pk)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="campaign-{campaign.pk}-recipients.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(["name", "phone", "email", "result", "details"])
    for r in recipients_with_outcome(campaign).iterator(chunk_size=1000):
        details = r.get_skip_reason_display() if r.skip_reason else (r.error or "")
        writer.writerow(
            [
                safe_cell(r.contact.full_name),
                safe_cell(r.contact.phone or ""),
                safe_cell(r.contact.email or ""),
                r.outcome,
                safe_cell(details),
            ]
        )
    return response


def _verify_meta_signature(request) -> bool:
    from apps.core.meta_signature import verify_meta_signature

    return verify_meta_signature(request, [settings.WHATSAPP_APP_SECRET])


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

        verify_token = settings.WHATSAPP_VERIFY_TOKEN
        if (
            mode == "subscribe"
            and verify_token
            and token
            and hmac.compare_digest(token, verify_token)
        ):
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse(status=403)

    def post(self, request):
        if not settings.WHATSAPP_APP_SECRET:
            logger.error(
                "WhatsApp webhook: WHATSAPP_APP_SECRET is not configured — rejecting every POST"
            )
            return HttpResponse(status=403)
        if not _verify_meta_signature(request):
            from apps.core.meta_signature import log_signature_failure

            log_signature_failure(request, "WhatsApp")
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

        from django.db import IntegrityError

        from apps.core.channels import channel_state

        # Stored before anything is processed, so a 200 never loses the event. While the
        # channel is off (or its settings can't be read) it stays held until replay.
        # A backlog of held events goes first: a new event joins the queue behind them.
        held = channel_state("whatsapp") != "enabled" or (
            WebhookEventLog.objects.filter(
                source=WebhookEventLog.Source.WHATSAPP, held=True
            ).exists()
        )
        fingerprint = hashlib.sha256(request.body).hexdigest()
        try:
            with transaction.atomic():
                event = WebhookEventLog.objects.create(
                    source=WebhookEventLog.Source.WHATSAPP,
                    event_type=_classify_event(payload),
                    payload=payload,
                    held=held,
                    fingerprint=fingerprint,
                )
        except IntegrityError:
            logger.debug("WhatsApp webhook: duplicate delivery %s", fingerprint[:12])
            return HttpResponse(status=200)

        if not held:
            transaction.on_commit(lambda: process_whatsapp_event.delay(event.id))

        return HttpResponse(status=200)
