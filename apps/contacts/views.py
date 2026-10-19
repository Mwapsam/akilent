"""Dashboard: contact list + customer profile (Phase 4.4)."""

from __future__ import annotations

import json
import logging
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
    ContactImport,
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

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50


def _contact_recommendations(contact):
    """Return recent RecommendationLogs involving this contact (via their conversations).

    Labelled 'recommendations involving this contact' — not 'insights' — because
    these are derived through the RecommendationLog relationship, not directly
    from a contact-scoped Insight.
    """
    try:
        from apps.insights.models import RecommendationLog

        return list(
            RecommendationLog.objects.filter(
                conversation__contact=contact,
            )
            .select_related("insight")
            .order_by("-recommended_at")[:3]
        )
    except Exception:
        logger.debug("_contact_recommendations unavailable for contact=%s", contact.pk)
        return []


def _filtered_contacts(request, account, params=None):
    """The Customers page's filter, as a queryset + the values it used.

    Shared with the bulk actions, so "select all N matching this filter"
    re-applies exactly the filter the owner was looking at — server-side, never
    a list of ids the browser happened to have loaded.
    """
    from apps.contacts import reachability

    params = params if params is not None else request.GET
    # Contact.Meta already orders by -first_seen; -id is added purely as a
    # tiebreaker so rows sharing a first_seen (a bulk import writing many
    # contacts at once) keep a stable position across pages.
    qs = reachability.annotate(
        Contact.objects.filter(account=account).order_by("-first_seen", "-id")
    )
    filters = {
        "q": (params.get("q") or "").strip(),
        "wa": (params.get("wa") or "").strip(),
        "tag": (params.get("tag") or "").strip(),
        "list": (params.get("list") or "").strip(),
    }
    if filters["q"]:
        # Phone-only contacts (e.g. WhatsApp-first customers) have no email.
        q = filters["q"]
        qs = qs.filter(
            Q(email__icontains=q)
            | Q(phone__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )
    if filters["wa"] in reachability.STATES:
        qs = reachability.filter_state(qs, filters["wa"])
    else:
        filters["wa"] = ""
    if filters["tag"]:
        qs = qs.filter(tags__slug=filters["tag"])
    if filters["list"].isdigit():
        qs = qs.filter(lists__pk=int(filters["list"]))
    else:
        filters["list"] = ""
    return qs, filters


@login_required
def contact_list(request):
    from apps.contacts import reachability

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs, filters = _filtered_contacts(request, account)
    page = Paginator(qs.prefetch_related("tags"), _PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    filtered = any(filters.values())
    context = {
        "account": account,
        "page": page,
        **filters,
        "filtered": filtered,
        "matching": page.paginator.count,
        "tags": Tag.objects.filter(account=account),
        "contact_lists": ContactList.objects.filter(account=account).order_by("name"),
        "wa_choices": [(s, reachability.LABELS[s]) for s in reachability.STATES],
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
    contact_recs = _contact_recommendations(contact)

    from apps.contacts import reachability

    member_of = list(contact.lists.order_by("name"))
    conversations = list(
        contact.conversations.order_by("-last_message_at", "-created_at").only(
            "public_id", "channel", "last_message_at"
        )[:3]
    )
    return render(
        request,
        "contacts/detail.html",
        {
            "account": account,
            "contact": contact,
            "events": events,
            "messages_to": messages_to,
            "lists": member_of,
            "other_lists": ContactList.objects.filter(account=account)
            .exclude(pk__in=[cl.pk for cl in member_of])
            .order_by("name"),
            "wa_state": reachability.state_for(contact),
            "wa_identity": reachability.primary_identity(contact),
            "conversations": conversations,
            "attribute_rows": attribute_rows,
            "contact_tags": contact.tags.all(),
            "known_tags": Tag.objects.filter(account=account),
            "contact_recs": contact_recs,
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
    # that's all we were given. An email already on a *different* customer would
    # break the one-email-per-customer rule: say whose it is instead of failing.
    if phone and email:
        owner = (
            Contact.objects.filter(account=account, email__iexact=email)
            .exclude(phone=phone)
            .first()
        )
        if owner is not None:
            messages.error(
                request,
                f"{owner.full_name or owner.email} already has the email {email}. "
                "Open their profile to update them, or leave the email out.",
            )
            return redirect("contacts:list")
    try:
        with transaction.atomic():
            if phone:
                contact, created = upsert_contact_by_phone(
                    account,
                    phone,
                    email=email or None,
                    **fields,  # type: ignore[arg-type]
                )
            else:
                contact, created = upsert_contact(account, email, **fields)  # type: ignore[arg-type]
    except IntegrityError:
        messages.error(
            request, "Another customer already has that phone number or email address."
        )
        return redirect("contacts:list")

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
    while CustomAttributeDef.objects.filter(
        account=account, entity=CustomAttributeDef.Entity.CONTACT, key=key
    ).exists():
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
    existing = CustomAttributeDef.objects.filter(
        account=account, entity=CustomAttributeDef.Entity.CONTACT, key=base_key
    ).first()
    if existing is not None and existing.label == label and existing.type == field_type:
        attribute = existing
    else:
        key = base_key if existing is None else _unique_attribute_key(account, base_key)
        attribute = CustomAttributeDef.objects.create(
            account=account,
            entity=CustomAttributeDef.Entity.CONTACT,
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


# --- Customer lists: the audience a WhatsApp campaign sends to. Every way of
# filling one (paste, CSV, a selection, everyone who messaged us, a tag, one
# person) goes through apps.contacts.list_building, so they all end the same
# way: real customers, reachable on WhatsApp, consent recorded when attested.


_LIST_PAGE_SIZE = 50


def _create_list(request, account, name: str):
    """A new list, or None (with the reason flashed) when the name is taken/empty."""
    name = (name or "").strip()[:150]
    if not name:
        messages.error(request, "Give the list a name.")
        return None
    slug = slugify(name)[:160]
    if not slug or ContactList.objects.filter(account=account, slug=slug).exists():
        messages.error(
            request, f'A list called "{name}" already exists — choose another name.'
        )
        return None
    return ContactList.objects.create(account=account, name=name)


def _list_from_post(request, account, *, allow_none=False):
    """The list a form picked: an existing one, a new one, or (if allowed) none.

    Returns ``(list_or_None, ok)``.
    """
    choice = (request.POST.get("list") or "").strip()
    if choice == "new":
        contact_list = _create_list(request, account, request.POST.get("new_name"))
        return contact_list, contact_list is not None
    if not choice and allow_none:
        return None, True
    contact_list = (
        ContactList.objects.filter(account=account, pk=choice).first()
        if choice.isdigit()
        else None
    )
    if contact_list is None:
        messages.error(request, "Choose a list.")
        return None, False
    return contact_list, True


def _summary(result, contact_list=None) -> str:
    parts = []
    if contact_list is not None:
        parts.append(f'{result.added:,} added to "{contact_list.name}"')
        if result.already_on_list:
            parts.append(f"{result.already_on_list:,} already on it")
    else:
        parts.append(
            f"{result.created:,} new customer{'' if result.created == 1 else 's'}"
        )
    if result.opted_in:
        parts.append(f"{result.opted_in:,} opted in")
    if result.invalid:
        parts.append(f"{len(result.invalid):,} need fixing")
    if result.over_limit:
        parts.append(f"{result.over_limit:,} over your plan's customer limit")
    return " · ".join(parts) + "."


@login_required
@require_POST
def contact_import(request):
    """Paste or CSV: add many customers at once, optionally to a list (spec S6)."""
    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list, ok = _list_from_post(request, account, allow_none=True)
    back = (
        redirect("contacts:list_detail", pk=contact_list.pk)
        if contact_list is not None
        else redirect("contacts:list")
    )
    if not ok:
        return back

    mode = request.POST.get("mode") or "paste"
    filename = ""
    try:
        if mode == "csv":
            upload = request.FILES.get("file")
            if upload is None:
                raise lb.ListBuildError("Choose a CSV file to upload.")
            if upload.size > lb.MAX_CSV_BYTES:
                raise lb.ListBuildError(
                    "That file is larger than 2 MB. Split it into smaller files."
                )
            filename = upload.name
            rows = lb.parse_csv(upload.read())
        else:
            rows = lb.parse_pasted(request.POST.get("numbers") or "")
    except lb.ListBuildError as exc:
        messages.error(request, str(exc))
        return back

    attested = request.POST.get("attested") == "1"
    result = lb.add_people_to_list(
        account,
        contact_list,
        rows,
        attested_by=request.user if attested else None,
        source="csv_import" if mode == "csv" else "pasted",
        filename=filename,
    )
    imp = ContactImport.objects.filter(account=account).order_by("-pk").first()
    if imp is not None and result.invalid:
        imp.mapping = {**(imp.mapping or {}), "invalid": result.invalid[:1000]}
        imp.save(update_fields=["mapping"])
    return render(
        request,
        "contacts/import_result.html",
        {
            "account": account,
            "result": result,
            "contact_list": contact_list,
            "attested": attested,
            "import_record": imp,
            "mode": mode,
            "invalid_text": "\n".join(
                raw for _line, raw, _why in result.invalid if raw
            ),
        },
    )


@login_required
def contact_import_rejected(request, pk: int):
    """The rows an import couldn't use, as a CSV to fix and upload again."""
    from django.http import HttpResponse

    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    imp = get_object_or_404(ContactImport, account=account, pk=pk)
    result = lb.ListAddResult(
        invalid=[tuple(r) for r in (imp.mapping or {}).get("invalid", [])]
    )
    response = HttpResponse(lb.invalid_rows_csv(result), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="numbers-to-fix.csv"'
    return response


@login_required
@require_POST
def bulk_add_to_list(request):
    """Customers page selection → a list. "All matching" re-applies the filter."""
    from django.http import QueryDict

    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.POST.get("all_matching") == "1":
        params = QueryDict(request.POST.get("filter") or "")
        contacts, _filters = _filtered_contacts(request, account, params)
    else:
        ids = request.POST.getlist("ids")[:5000]
        contacts = Contact.objects.filter(account=account, public_id__in=ids)
    if not contacts.exists():
        messages.error(request, "Select at least one customer.")
        return redirect("contacts:list")

    contact_list, ok = _list_from_post(request, account)
    if not ok:
        return redirect("contacts:list")
    result = lb.add_contacts_to_list(account, contact_list, contacts.order_by())
    messages.success(request, _summary(result, contact_list))
    return redirect("contacts:list_detail", pk=contact_list.pk)


def _list_readiness(contact_list) -> dict:
    from apps.contacts import reachability

    counts = reachability.counts(contact_list.contacts.all())
    total = counts["total"] or 0
    counts["pct"] = {
        s: (round(counts[s] * 100 / total, 1) if total else 0)
        for s in reachability.STATES
    }
    return counts


def _can_send_campaigns(account) -> bool:
    try:
        from apps.billing import api as billing_api
        from apps.core.channels import is_enabled

        return is_enabled("whatsapp") and billing_api.usable(
            account, "whatsapp_campaigns"
        )
    except Exception:
        logger.debug("campaign availability check failed", exc_info=True)
        return False


def _messaged_us(account):
    """Customers with a WhatsApp conversation who didn't opt out."""
    from apps.whatsapp.models import WhatsAppContact

    return (
        Contact.objects.filter(
            account=account, whatsapp_contacts__last_message_at__isnull=False
        )
        .exclude(whatsapp_contacts__opt_in_status=WhatsAppContact.OptInStatus.OPTED_OUT)
        .distinct()
    )


@login_required
def list_index(request):
    from django.db.models import Max

    from apps.contacts import reachability

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    lists = list(
        ContactList.objects.filter(account=account)
        .annotate(
            contact_count=Count("contacts", distinct=True),
            last_campaign_at=Max("whatsapp_campaigns__created_at"),
        )
        .order_by("name")
    )
    for cl in lists:
        c = reachability.counts(cl.contacts.all())
        cl.ready_marketing = c["opted_in"]  # type: ignore[attr-defined]
        cl.reachable = c["opted_in"] + c["updates"]  # type: ignore[attr-defined]
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
    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = _create_list(request, account, request.POST.get("name"))
    if contact_list is None:
        return redirect("contacts:lists")

    seed_tag_slug = (request.POST.get("seed_tag") or "").strip()
    if seed_tag_slug:
        tag = Tag.objects.filter(account=account, slug=seed_tag_slug).first()
        if tag is not None:
            result = lb.add_contacts_to_list(
                account, contact_list, Contact.objects.filter(account=account, tags=tag)
            )
            messages.success(
                request,
                f'"{contact_list.name}" created with {result.added:,} customer'
                f'{"" if result.added == 1 else "s"} tagged "{tag.name}".',
            )
            return redirect("contacts:list_detail", pk=contact_list.pk)
    messages.success(request, f'"{contact_list.name}" created — now add people to it.')
    return redirect("contacts:list_detail", pk=contact_list.pk)


@login_required
def list_detail(request, pk: int):
    from apps.contacts import reachability

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)

    members = reachability.annotate(
        contact_list.contacts.filter(account=account).order_by("-first_seen", "-id")
    )
    mq = (request.GET.get("mq") or "").strip()
    wa = (request.GET.get("wa") or "").strip()
    if mq:
        members = members.filter(
            Q(email__icontains=mq)
            | Q(phone__icontains=mq)
            | Q(first_name__icontains=mq)
            | Q(last_name__icontains=mq)
        )
    if wa in reachability.STATES:
        members = reachability.filter_state(members, wa)
    else:
        wa = ""
    page = Paginator(members, _LIST_PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "account": account,
        "contact_list": contact_list,
        "page": page,
        "mq": mq,
        "wa": wa,
        "readiness": _list_readiness(contact_list),
        "wa_choices": [(s, reachability.LABELS[s]) for s in reachability.STATES],
        "tags": Tag.objects.filter(account=account),
        "campaign_count": contact_list.whatsapp_campaigns.count(),
        "can_send": _can_send_campaigns(account),
        "messaged_count": _messaged_us(account).exclude(lists=contact_list).count(),
    }
    if is_background(request):
        return render(request, "contacts/_list_members.html", context)
    return render(request, "contacts/list_detail.html", context)


@login_required
def list_picker(request, pk: int):
    """Customers not yet on the list, for the "From your customers" picker (HTMX)."""
    from apps.contacts import reachability

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    cq = (request.GET.get("cq") or "").strip()
    candidates = reachability.annotate(
        Contact.objects.filter(account=account).exclude(lists=contact_list)
    ).order_by("-last_engaged_at", "-first_seen", "-id")
    if cq:
        candidates = candidates.filter(
            Q(email__icontains=cq)
            | Q(phone__icontains=cq)
            | Q(first_name__icontains=cq)
            | Q(last_name__icontains=cq)
        )
    return render(
        request,
        "contacts/_list_picker_results.html",
        {"contact_list": contact_list, "candidates": candidates[:30], "cq": cq},
    )


@login_required
@require_POST
def list_add_selected(request, pk: int):
    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    ids = request.POST.getlist("ids")[:1000]
    contacts = Contact.objects.filter(account=account, public_id__in=ids)
    if not ids or not contacts.exists():
        messages.error(request, "Choose at least one customer.")
        return redirect("contacts:list_detail", pk=pk)
    result = lb.add_contacts_to_list(account, contact_list, contacts)
    messages.success(request, _summary(result, contact_list))
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_add_messaged(request, pk: int):
    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    result = lb.add_contacts_to_list(account, contact_list, _messaged_us(account))
    messages.success(
        request,
        _summary(result, contact_list)
        if result.added or result.already_on_list
        else "Nobody has messaged you on WhatsApp yet.",
    )
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_add_by_tag(request, pk: int):
    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)

    tag_slug = (request.POST.get("tag") or "").strip()
    tag = Tag.objects.filter(account=account, slug=tag_slug).first()
    if tag is None:
        messages.error(request, "Choose a tag.")
        return redirect("contacts:list_detail", pk=pk)

    result = lb.add_contacts_to_list(
        account, contact_list, Contact.objects.filter(account=account, tags=tag)
    )
    messages.success(
        request,
        f'Added {result.added:,} customer{"" if result.added == 1 else "s"} tagged "{tag.name}".'
        if result.added
        else f'Everyone tagged "{tag.name}" is already on this list.',
    )
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_add_contact(request, pk: int):
    from apps.contacts import list_building as lb

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    contact = get_object_or_404(
        Contact, account=account, public_id=request.POST.get("public_id")
    )
    lb.add_contacts_to_list(
        account, contact_list, Contact.objects.filter(pk=contact.pk)
    )
    messages.success(
        request, f'{contact.full_name or contact} added to "{contact_list.name}".'
    )
    if request.POST.get("next"):
        return _back_to(request, contact)
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_remove_contact(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    contact = Contact.objects.filter(
        account=account, public_id=request.POST.get("public_id")
    ).first()
    if contact is not None:
        ContactListMembership.objects.filter(
            contact_list=contact_list, contact=contact
        ).delete()
        messages.success(
            request,
            f'{contact.full_name or contact} removed from "{contact_list.name}". '
            "They're still in your customers.",
        )
        if request.POST.get("next"):
            return _back_to(request, contact)
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_rename(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    name = (request.POST.get("name") or "").strip()[:150]
    slug = slugify(name)[:160]
    if not name or not slug:
        messages.error(request, "Give the list a name.")
    elif (
        ContactList.objects.filter(account=account, slug=slug)
        .exclude(pk=contact_list.pk)
        .exists()
    ):
        messages.error(request, f'A list called "{name}" already exists.')
    else:
        contact_list.name = name
        contact_list.slug = slug
        contact_list.save(update_fields=["name", "slug"])
        messages.success(request, "List renamed.")
    return redirect("contacts:list_detail", pk=pk)


@login_required
@require_POST
def list_delete(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact_list = get_object_or_404(ContactList, account=account, pk=pk)
    used = contact_list.whatsapp_campaigns.count()
    if used:
        messages.error(
            request,
            f'"{contact_list.name}" was used by {used} campaign{"" if used == 1 else "s"}, '
            "so it's kept for their history. Rename it or remove people instead.",
        )
        return redirect("contacts:list_detail", pk=pk)
    name = contact_list.name
    contact_list.delete()
    messages.success(
        request, f'"{name}" deleted. The people on it are still in your customers.'
    )
    return redirect("contacts:lists")


# --- WhatsApp consent on a customer's profile ------------------------------


@login_required
@require_POST
def contact_whatsapp_consent(request, public_id: str):
    """Mark a customer opted in (with the owner's attestation) or opted out."""
    from apps.contacts import reachability
    from apps.contacts.list_building import attestation_source
    from apps.whatsapp.services.contacts import ensure_whatsapp_identity

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)
    action = request.POST.get("action")
    identity = reachability.primary_identity(contact) or ensure_whatsapp_identity(
        contact
    )
    if identity is None:
        messages.error(request, "Add a WhatsApp number for this customer first.")
    elif action == "opt_in":
        if request.POST.get("attested") != "1":
            messages.error(request, "Confirm that they agreed to hear from you.")
        else:
            identity.record_opt_in(attestation_source(request.user))
            messages.success(request, f"{contact.full_name or contact} is opted in.")
    elif action == "opt_out":
        identity.record_opt_out(f"marked by user {request.user.pk}")
        messages.success(
            request,
            f"{contact.full_name or contact} is opted out. Campaigns will skip them.",
        )
    return redirect("contacts:detail", public_id=public_id)
