"""Dashboard: contact list + customer profile (Phase 4.4)."""
from __future__ import annotations

import json
import re

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts.models import Contact, CustomAttributeDef
from apps.email.models import EmailMessage

_PAGE_SIZE = 50


@login_required
def contact_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs = Contact.objects.filter(account=account)
    q = (request.GET.get("q") or "").strip()
    st = (request.GET.get("status") or "").strip()
    if q:
        # Phone-only contacts (e.g. WhatsApp-first customers) have no email.
        qs = qs.filter(
            Q(email__icontains=q) | Q(phone__icontains=q)
            | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        )
    if st:
        qs = qs.filter(status=st)

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "contacts/list.html", {
        "account": account,
        "page": page,
        "q": q,
        "status": st,
        "status_choices": Contact.Status.choices,
        "total": Contact.objects.filter(account=account).count(),
    })


@login_required
def contact_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        contact = Contact.objects.get(account=account, public_id=public_id)
    except Contact.DoesNotExist:
        return render(request, "contacts/not_found.html", status=404)

    from apps.contacts.event_labels import event_detail, event_label

    events = list(contact.events.all()[:100])
    for e in events:
        e.label = event_label(e.type)
        e.detail = event_detail(e.type, e.data)

    messages_to = (
        EmailMessage.objects.filter(account=account, to_email__iexact=contact.email)
        .order_by("-created_at")[:50]
    )
    # Readable (label, value) pairs instead of a raw attributes dict — R1: no
    # JSON on a page a business user works from (docs/plans amendment).
    attribute_rows = sorted(
        (k.replace("_", " ").capitalize(), v)
        for k, v in (contact.attributes or {}).items()
        if v not in (None, "")
    )
    return render(request, "contacts/detail.html", {
        "account": account,
        "contact": contact,
        "events": events,
        "messages_to": messages_to,
        "lists": contact.lists.all(),
        "attribute_rows": attribute_rows,
    })


def _unique_attribute_key(account, base_key: str) -> str:
    key = base_key
    suffix = 2
    while CustomAttributeDef.objects.filter(account=account, key=key).exists():
        key = f"{base_key}_{suffix}"
        suffix += 1
    return key


# --- Custom contact fields: created inline from wherever a business owner is
# picking a variable to personalize a message with (today: the WhatsApp
# template builder, apps.whatsapp.views.template_create) — the field itself
# belongs to Contact, not to whichever feature happened to prompt its
# creation, so it lives here rather than in that feature's app. ------------

@login_required
@require_POST
def create_custom_field(request):
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}

    label = (payload.get("label") or "").strip()
    field_type = (payload.get("type") or CustomAttributeDef.Type.STRING).strip()
    if not label:
        return JsonResponse({"error": "Give the field a name."}, status=400)
    if field_type not in {t[0] for t in CustomAttributeDef.Type.choices}:
        return JsonResponse({"error": "Choose a valid field type."}, status=400)

    base_key = re.sub(r"-+", "_", slugify(label))[:64] or "field"
    existing = CustomAttributeDef.objects.filter(account=account, key=base_key).first()
    if existing is not None and existing.label == label and existing.type == field_type:
        attribute = existing
    else:
        key = base_key if existing is None else _unique_attribute_key(account, base_key)
        attribute = CustomAttributeDef.objects.create(
            account=account, key=key, type=field_type, label=label,
        )

    return JsonResponse({
        "key": attribute.key, "label": attribute.label or attribute.key,
        "type": attribute.type, "sample": attribute.sample_value,
    })
