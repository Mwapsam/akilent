import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts.models import ContactList
from apps.core.module_gate import module_required
from apps.whatsapp.campaigns import CampaignError, create_and_queue_campaign
from apps.whatsapp.models import MessageTemplate, WebhookEventLog, WhatsAppCampaign
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
@module_required("whatsapp")
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


# --- Template creation (amendment to R1.5c follow-up, 2026-09-22): Meta's own
# UI is too complex for a non-technical owner, so Akilent offers a simplified
# builder in front of it. Akilent validates and submits to Meta; Meta stays
# the approval source of truth (apps.whatsapp.template_builder). ------------

@login_required
@module_required("whatsapp")
def template_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        labels = [v.strip() for v in request.POST.getlist("variable_label") if v.strip()]
        examples = [v.strip() for v in request.POST.getlist("variable_example") if v.strip()]
        try:
            create_and_submit_template(
                account,
                name=request.POST.get("name", "").strip().lower(),
                category=request.POST.get("category", ""),
                language=request.POST.get("language", "en"),
                body=request.POST.get("body", ""),
                header=request.POST.get("header", ""),
                footer=request.POST.get("footer", ""),
                variable_labels=labels,
                variable_examples=examples,
            )
        except TemplateBuilderError as exc:
            messages.error(request, str(exc))
            rows = list(zip(labels, examples)) or [("", "")]
            return render(request, "whatsapp/template_create.html", {
                "account": account, "categories": MessageTemplate.Category.choices,
                "form": request.POST, "starters_json": json.dumps(STARTER_CATEGORIES),
                "variable_rows": rows,
            })
        messages.success(request, "Template submitted to WhatsApp for approval.")
        return redirect("/email/templates/?channel=whatsapp")

    return render(request, "whatsapp/template_create.html", {
        "account": account, "categories": MessageTemplate.Category.choices, "form": {},
        "starters_json": json.dumps(STARTER_CATEGORIES),
    })


# --- Campaigns (R1.5c): create + list + detail. Sending logic lives in
# apps.whatsapp.campaigns, same split as apps.whatsapp.numbers (setup UI) vs
# apps.whatsapp.tasks (sending). This file is the module boundary rule's
# exempted "views.py", so the ContactList import above is allowed here and
# nowhere else in this app. ------------------------------------------------

@login_required
@module_required("whatsapp")
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
@module_required("whatsapp")
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