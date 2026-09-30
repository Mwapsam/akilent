"""Dashboard: contact list + customer profile (Phase 4.4)."""

from __future__ import annotations

import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts.models import (
    Contact,
    ContactList,
    ContactListMembership,
    CustomAttributeDef,
    Tag,
)
from apps.contacts.services import (
    ContactLimitReached,
    ensure_room_for_contact,
    upsert_contact,
    upsert_contact_by_phone,
)
from apps.core.htmx import is_background
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
            Q(email__icontains=q)
            | Q(phone__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )
    if st:
        qs = qs.filter(status=st)
    tag_slug = (request.GET.get("tag") or "").strip()
    if tag_slug:
        qs = qs.filter(tags__slug=tag_slug)

    page = Paginator(qs.prefetch_related("tags"), _PAGE_SIZE).get_page(
        request.GET.get("page")
    )
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
    if is_background(request):
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
        e.label = event_label(e.type)  # type: ignore[attr-defined]
        e.detail = event_detail(e.type, e.data)  # type: ignore[attr-defined]

    messages_to = EmailMessage.objects.filter(
        account=account, to_email__iexact=contact.email
    ).order_by("-created_at")[:50]
    # Readable (label, value) pairs instead of a raw attributes dict — R1: no
    # JSON on a page a business user works from (docs/plans amendment).
    attribute_rows = sorted(
        (k.replace("_", " ").capitalize(), v)
        for k, v in (contact.attributes or {}).items()
        if v not in (None, "")
    )
    return render(
        request,
        "contacts/detail.html",
        {
            "account": account,
            "contact": contact,
            "events": events,
            "messages_to": messages_to,
            "lists": contact.lists.all(),
            "attribute_rows": attribute_rows,
            "contact_tags": contact.tags.all(),
            "known_tags": Tag.objects.filter(account=account),
        },
    )


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
        raise ValueError(
            "Add a phone number or an email address — we need one to reach them."
        )

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
    try:
        ensure_room_for_contact(account, email=email, phone=phone)
    except ContactLimitReached as exc:
        messages.error(request, str(exc))
        return redirect("contacts:list")
    # Phone is the identity for a WhatsApp-first business; email only leads when
    # that's all we were given.
    if phone:
        contact, created = upsert_contact_by_phone(
            account,
            phone,
            email=email or None,
            **fields,  # type: ignore[arg-type]
        )
    else:
        contact, created = upsert_contact(account, email, **fields)  # type: ignore[arg-type]

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
            contact.save(
                update_fields=[
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "updated_at",
                ]
            )
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
            account=account,
            key=key,
            type=field_type,
            label=label,
        )

    return JsonResponse(
        {
            "key": attribute.key,
            "label": attribute.label or attribute.key,
            "type": attribute.type,
            "sample": attribute.sample_value,
        }
    )


# --- Customer lists: the audience a WhatsApp/email campaign sends to. Until
# now a ContactList could only be created through the public API — nothing in
# the dashboard offered a way to make one, so campaign creation always found
# an empty list of lists regardless of how many contacts existed. This is the
# minimal path: name a list, then add contacts to it by tag or one at a time.
# CSV import / preview / segments-as-lists are a later, bigger piece of work. -


_LIST_PAGE_SIZE = 50


@login_required
def list_index(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    lists = (
        ContactList.objects.filter(account=account)
        .annotate(contact_count=Count("contacts"))
        .order_by("name")
    )
    return render(
        request,
        "contacts/lists.html",
        {
            "account": account,
            "lists": lists,
            "tags": Tag.objects.filter(account=account),
        },
    )


@login_required
@require_POST
def list_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Give the list a name.")
        return redirect("contacts:lists")

    slug = slugify(name)[:160]
    if ContactList.objects.filter(account=account, slug=slug).exists():
        messages.error(request, f'A list called "{name}" already exists.')
        return redirect("contacts:lists")

    contact_list = ContactList.objects.create(account=account, name=name)

    seed_tag_slug = (request.POST.get("seed_tag") or "").strip()
    added = 0
    if seed_tag_slug:
        tag = Tag.objects.filter(account=account, slug=seed_tag_slug).first()
        if tag is not None:
            contacts = Contact.objects.filter(account=account, tags=tag)
            ContactListMembership.objects.bulk_create(
                [
                    ContactListMembership(contact_list=contact_list, contact=c)
                    for c in contacts
                ],
                ignore_conflicts=True,
            )
            added = contacts.count()

    messages.success(
        request,
        f'"{name}" created with {added} customer{"" if added == 1 else "s"}.'
        if seed_tag_slug
        else f'"{name}" created — add customers to it below.',
    )
    return redirect("contacts:list_detail", pk=contact_list.pk)


@login_required
def list_detail(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)

    all_members = contact_list.contacts.order_by("-first_seen", "-id")
    member_count = all_members.count()
    members = all_members[:_LIST_PAGE_SIZE]

    cq = (request.GET.get("cq") or "").strip()
    candidates = []
    if cq:
        candidates = list(
            Contact.objects.filter(account=account)
            .exclude(lists=contact_list)
            .filter(
                Q(email__icontains=cq)
                | Q(phone__icontains=cq)
                | Q(first_name__icontains=cq)
                | Q(last_name__icontains=cq)
            )[:20]
        )

    return render(
        request,
        "contacts/list_detail.html",
        {
            "account": account,
            "contact_list": contact_list,
            "members": members,
            "member_count": member_count,
            "tags": Tag.objects.filter(account=account),
            "cq": cq,
            "candidates": candidates,
        },
    )


@login_required
@require_POST
def list_add_by_tag(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)

    tag_slug = (request.POST.get("tag") or "").strip()
    tag = Tag.objects.filter(account=account, slug=tag_slug).first()
    if tag is None:
        messages.error(request, "Choose a tag.")
        return redirect("contacts:list_detail", pk=pk)

    contacts = Contact.objects.filter(account=account, tags=tag).exclude(
        lists=contact_list
    )
    ContactListMembership.objects.bulk_create(
        [ContactListMembership(contact_list=contact_list, contact=c) for c in contacts],
        ignore_conflicts=True,
    )
    count = contacts.count()
    messages.success(
        request,
        f'Added {count} customer{"" if count == 1 else "s"} tagged "{tag.name}".'
        if count
        else f'Everyone tagged "{tag.name}" is already on this list.',
    )
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_add_contact(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    contact = get_object_or_404(
        Contact, account=account, public_id=request.POST.get("public_id")
    )
    ContactListMembership.objects.get_or_create(
        contact_list=contact_list, contact=contact
    )
    messages.success(request, f"{contact.full_name or contact} added to the list.")
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_remove_contact(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    ContactListMembership.objects.filter(
        contact_list=contact_list, contact__public_id=request.POST.get("public_id")
    ).delete()
    return redirect("contacts:list_detail", pk=pk)
