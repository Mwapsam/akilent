"""Dashboard: contact list + customer profile (Phase 4.4)."""
from __future__ import annotations

import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts.models import Contact, CustomAttributeDef, Tag
from apps.contacts.services import upsert_contact, upsert_contact_by_phone
from apps.email.models import EmailMessage

_PAGE_SIZE = 50


@login_required
def contact_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    # Contact.Meta already orders by -first_seen; -id is added purely as a
    # tiebreaker so rows sharing a first_seen (a bulk import writing many
    # contacts at once) keep a stable position across pages.
    qs = Contact.objects.filter(account=account).order_by("-first_seen", "-id")
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
    tag_slug = (request.GET.get("tag") or "").strip()
    if tag_slug:
        qs = qs.filter(tags__slug=tag_slug)

    page = Paginator(qs.prefetch_related("tags"), _PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "account": account,
        "page": page,
        "q": q,
        "status": st,
        "tag": tag_slug,
        "tags": Tag.objects.filter(account=account),
        "status_choices": Contact.Status.choices,
        "total": Contact.objects.filter(account=account).count(),
    }
    # HTMX asks for the results on its own; a full navigation gets the page.
    # Same view, same context - only the wrapper differs.
    if request.headers.get("HX-Request"):
        return render(request, "contacts/_list_results.html", context)
    return render(request, "contacts/list.html", context)


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
        "contact_tags": contact.tags.all(),
        "known_tags": Tag.objects.filter(account=account),
    })


def _back_to(request, contact):
    """Where to return after a tag change: the page it was made from, else the profile."""
    from django.utils.http import url_has_allowed_host_and_scheme

    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect("contacts:detail", public_id=contact.public_id)


def _change_tag(request, public_id: str, action: str):
    from apps.contacts import tags as contact_tags

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)
    try:
        getattr(contact_tags, action)(contact, request.POST.get("tag", ""))
    except contact_tags.TagError as exc:
        messages.error(request, str(exc))
    return _back_to(request, contact)


@login_required
@require_POST
def contact_tag_add(request, public_id: str):
    return _change_tag(request, public_id, "add_tag")


@login_required
@require_POST
def contact_tag_remove(request, public_id: str):
    return _change_tag(request, public_id, "remove_tag")


# --- Add / edit a customer by hand. Until now contacts could only arrive via
# the API, a CSV import or an inbound message, so a business owner couldn't add
# someone they met offline or fix a misspelled name. --------------------------

def _clean_identity(request) -> tuple[str, str]:
    """Return ``(email, phone)`` from the POST, normalized, or raise ValueError."""
    from apps.whatsapp.models.contact import normalize_phone

    email = (request.POST.get("email") or "").strip().lower()
    phone_raw = (request.POST.get("phone") or "").strip()
    if not email and not phone_raw:
        raise ValueError("Add a phone number or an email address — we need one to reach them.")

    phone = ""
    if phone_raw:
        try:
            phone = normalize_phone(phone_raw)
        except Exception as exc:
            raise ValueError(
                "That phone number doesn't look right. Include the country code, e.g. +260971234567."
            ) from exc
    return email, phone


@login_required
@require_POST
def contact_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    try:
        email, phone = _clean_identity(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("contacts:list")

    fields = {
        "first_name": (request.POST.get("first_name") or "").strip(),
        "last_name": (request.POST.get("last_name") or "").strip(),
    }
    # Phone is the identity for a WhatsApp-first business; email only leads when
    # that's all we were given.
    if phone:
        contact, created = upsert_contact_by_phone(account, phone, email=email or None, **fields)
    else:
        contact, created = upsert_contact(account, email, **fields)

    label = contact.full_name or str(contact)
    messages.success(
        request,
        f"{label} added." if created else f"{label} already existed — details updated.",
    )
    return redirect("contacts:detail", public_id=contact.public_id)


@login_required
@require_POST
def contact_edit(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        contact = Contact.objects.get(account=account, public_id=public_id)
    except Contact.DoesNotExist:
        return render(request, "contacts/not_found.html", status=404)

    try:
        email, phone = _clean_identity(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("contacts:detail", public_id=public_id)

    contact.first_name = (request.POST.get("first_name") or "").strip()
    contact.last_name = (request.POST.get("last_name") or "").strip()
    # Null, not "", so the partial unique constraints keep ignoring empty values.
    contact.email = email or None
    contact.phone = phone or None
    try:
        # Savepoint: a unique-constraint failure otherwise poisons the whole
        # request transaction and nothing after this point can query.
        with transaction.atomic():
            contact.save(update_fields=["first_name", "last_name", "email", "phone", "updated_at"])
    except IntegrityError:
        messages.error(
            request, "Another customer already has that phone number or email address."
        )
        return redirect("contacts:detail", public_id=public_id)

    messages.success(request, "Customer updated.")
    return redirect("contacts:detail", public_id=public_id)


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
