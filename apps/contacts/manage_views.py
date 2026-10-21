"""Looking after customer records (audit 2026-10-19, Phase 9): notes, custom
fields, export, bulk tag/delete and self-service deletion.

Deleting and exporting are owner/admin only: one removes data for good, the
other takes every customer's details out of Akilent.
"""

from __future__ import annotations

import csv
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts.models import Contact, ContactNote, CustomAttributeDef
from apps.core.csv_safe import safe_cell

_BULK_MAX = 5000


def _is_admin(request, account) -> bool:
    from apps.accounts.api import is_account_admin

    return is_account_admin(request.user, account)


def _audit(request, account, action: str, contact_id: str = "", **meta) -> None:
    from apps.core.audit import platform_record

    platform_record(
        action=action,
        account=account,
        actor=request.user,
        resource_type="contact",
        resource_id=contact_id,
        metadata=meta,
    )


def _selection(request, account):
    """The Customers page selection: ticked ids, or "all matching" the filter."""
    from apps.contacts.views import _filtered_contacts

    if request.POST.get("all_matching") == "1":
        params = QueryDict(request.POST.get("filter") or "")
        qs, _filters = _filtered_contacts(request, account, params)
        return qs.order_by()
    ids = request.POST.getlist("ids")[:_BULK_MAX]
    return Contact.objects.filter(account=account, public_id__in=ids)


# --- Delete --------------------------------------------------------------------


@login_required
def contact_delete(request, public_id: str):
    """Confirm screen (what will be removed), then delete for good."""
    from apps.contacts import erasure

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)
    if not _is_admin(request, account):
        messages.error(
            request, "Only the business owner or an admin can delete customers."
        )
        return redirect("contacts:detail", public_id=public_id)
    if request.method == "POST":
        if request.POST.get("confirm") != "delete":
            messages.error(request, "Type delete to confirm.")
            return redirect("contacts:delete", public_id=public_id)
        name = contact.full_name or contact.phone or contact.email or "The customer"
        erasure.delete_customer(contact, actor=request.user)
        messages.success(request, f"{name} and everything about them was deleted.")
        return redirect("contacts:list")
    return render(
        request,
        "contacts/delete_confirm.html",
        {"account": account, "contact": contact, "removes": erasure.preview(contact)},
    )


@login_required
@require_POST
def bulk_delete(request):
    from apps.contacts import erasure

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not _is_admin(request, account):
        messages.error(
            request, "Only the business owner or an admin can delete customers."
        )
        return redirect("contacts:list")
    if request.POST.get("confirm") != "delete":
        messages.error(request, "Type delete to confirm.")
        return redirect("contacts:list")
    n = 0
    for contact in _selection(request, account)[:_BULK_MAX].iterator(chunk_size=200):
        erasure.delete_customer(contact, actor=request.user, source="bulk")
        n += 1
    messages.success(
        request,
        f"Deleted {n:,} customer{'s' if n != 1 else ''} and everything about them.",
    )
    return redirect("contacts:list")


# --- Bulk tags -------------------------------------------------------------------


@login_required
@require_POST
def bulk_tag(request):
    from apps.contacts import tags as contact_tags

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    name = (request.POST.get("tag") or "").strip()[:40]
    remove = request.POST.get("action") == "remove"
    if not name:
        messages.error(request, "Type a tag.")
        return redirect("contacts:list")
    n = 0
    for contact in _selection(request, account)[:_BULK_MAX].iterator(chunk_size=500):
        if remove:
            contact_tags.remove_tag(contact, name)
        else:
            contact_tags.add_tag(contact, name)
        n += 1
    verb = "Removed" if remove else "Added"
    prep = "from" if remove else "to"
    messages.success(
        request, f"{verb} “{name}” {prep} {n:,} customer{'s' if n != 1 else ''}."
    )
    return redirect(f"/contacts/?{request.POST.get('filter') or ''}")


# --- Export ----------------------------------------------------------------------


EXPORT_HEADER = [
    "first_name",
    "last_name",
    "phone",
    "email",
    "whatsapp",
    "tags",
    "lists",
    "source",
    "first_seen",
    "last_active",
]


@login_required
def contacts_export(request):
    """Every customer matching the Customers page filter, as a formula-safe CSV."""
    from apps.contacts import reachability
    from apps.contacts.views import _filtered_contacts

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not _is_admin(request, account):
        messages.error(
            request, "Only the business owner or an admin can export customers."
        )
        return redirect("contacts:list")
    qs, filters = _filtered_contacts(request, account)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="customers-{timezone.localdate():%Y-%m-%d}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(EXPORT_HEADER)
    for c in qs.prefetch_related("tags", "lists").iterator(chunk_size=500):
        writer.writerow(
            [
                safe_cell(c.first_name),
                safe_cell(c.last_name),
                safe_cell(c.phone or ""),
                safe_cell(c.email or ""),
                reachability.LABELS.get(c.wa_state, ""),
                safe_cell(";".join(t.name for t in c.tags.all())),
                safe_cell(";".join(cl.name for cl in c.lists.all())),
                safe_cell(c.source),
                c.first_seen.date().isoformat() if c.first_seen else "",
                c.last_engaged_at.date().isoformat() if c.last_engaged_at else "",
            ]
        )
    _audit(
        request,
        account,
        "contact.export",
        filters={k: v for k, v in filters.items() if v},
    )
    return response


# --- Notes -----------------------------------------------------------------------


@login_required
@require_POST
def note_add(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)
    body = (request.POST.get("body") or "").strip()[:5000]
    if body:
        ContactNote.objects.create(
            account=account, contact=contact, author=request.user, body=body
        )
        messages.success(request, "Note added.")
    else:
        messages.error(request, "Write something first.")
    return redirect("contacts:detail", public_id=public_id)


@login_required
@require_POST
def note_delete(request, public_id: str, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    note = get_object_or_404(
        ContactNote, account=account, contact__public_id=public_id, pk=pk
    )
    if note.author_id != request.user.pk and not _is_admin(request, account):
        messages.error(
            request, "Only the person who wrote it, or an admin, can delete a note."
        )
    else:
        note.delete()
        messages.success(request, "Note deleted.")
    return redirect("contacts:detail", public_id=public_id)


# --- Custom fields ---------------------------------------------------------------


def contact_field_defs(account):
    return list(
        CustomAttributeDef.objects.filter(
            account=account,
            entity=CustomAttributeDef.Entity.CONTACT,
            archived_at__isnull=True,
        ).order_by("label", "key")
    )


def field_rows(contact, defs) -> list[dict]:
    """Each custom field with its label and the value as a person reads it."""
    attrs = contact.attributes or {}
    rows = []
    for d in defs:
        raw = attrs.get(d.key)
        shown = raw
        if d.type == CustomAttributeDef.Type.CHOICE:
            shown = next(
                (o.get("label") for o in d.options or [] if o.get("key") == raw), raw
            )
        elif d.type == CustomAttributeDef.Type.BOOLEAN and raw is not None:
            shown = "Yes" if raw else "No"
        rows.append(
            {
                "def": d,
                "value": "" if raw is None else raw,
                "shown": "" if shown in (None, "") else shown,
            }
        )
    return rows


@login_required
@require_POST
def contact_fields_save(request, public_id: str):
    from apps.contacts.attributes import set_attributes

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)
    values = {}
    for d in contact_field_defs(account):
        name = f"attr__{d.key}"
        if name not in request.POST:
            continue
        raw = request.POST.get(name, "").strip()
        values[d.key] = raw if raw != "" else None
    try:
        changed = set_attributes(
            contact, values, actor=f"user:{request.user.pk}", source="ui"
        )
    except ValueError:
        messages.error(
            request,
            "One of those values isn't the right kind (for example, letters in a number field).",
        )
        return redirect("contacts:detail", public_id=public_id)
    if changed:
        messages.success(request, "Saved.")
    return redirect("contacts:detail", public_id=public_id)


@login_required
def field_index(request):
    """Settings for the customer details you keep (custom fields)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    can_manage = _is_admin(request, account)
    error = ""
    if request.method == "POST":
        if not can_manage:
            messages.error(
                request, "Only the business owner or an admin can change fields."
            )
            return redirect("contacts:fields")
        error = _save_field(request, account)
        if not error:
            return redirect("contacts:fields")
    defs = CustomAttributeDef.objects.filter(
        account=account, entity=CustomAttributeDef.Entity.CONTACT
    ).order_by("archived_at", "label", "key")
    return render(
        request,
        "contacts/fields.html",
        {
            "account": account,
            "active": [d for d in defs if d.archived_at is None],
            "archived": [d for d in defs if d.archived_at is not None],
            "types": CustomAttributeDef.Type.choices,
            "can_manage": can_manage,
            "error": error,
        },
        status=400 if error else 200,
    )


def _options_from(text: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        label = line.strip()[:100]
        if not label:
            continue
        key = re.sub(r"-+", "_", slugify(label))[:64] or f"option_{len(out) + 1}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "label": label})
    return out


def _save_field(request, account) -> str:
    """Create or edit one field from the settings form. Returns an error or ""."""
    pk = request.POST.get("pk")
    label = (request.POST.get("label") or "").strip()[:150]
    if not label:
        return "Give the field a name."
    if pk:
        d = get_object_or_404(
            CustomAttributeDef,
            account=account,
            entity=CustomAttributeDef.Entity.CONTACT,
            pk=pk,
        )
        d.label = label
        update = ["label"]
        if d.type == CustomAttributeDef.Type.CHOICE:
            opts = _options_from(request.POST.get("options", ""))
            if not opts:
                return "A choice field needs at least one option (one per line)."
            # Keep options already used: removing one would orphan saved values.
            kept = {o["key"] for o in opts}
            for o in d.options or []:
                if (
                    o["key"] not in kept
                    and Contact.objects.filter(
                        account=account, **{f"attributes__{d.key}": o["key"]}
                    ).exists()
                ):
                    opts.append(o)
            d.options = opts
            update.append("options")
        d.save(update_fields=update)
        messages.success(request, f"Saved “{label}”.")
        return ""
    field_type = request.POST.get("type") or CustomAttributeDef.Type.STRING
    if field_type not in CustomAttributeDef.Type.values:
        return "Choose a valid field type."
    options = []
    if field_type == CustomAttributeDef.Type.CHOICE:
        options = _options_from(request.POST.get("options", ""))
        if not options:
            return "A choice field needs at least one option (one per line)."
    from apps.contacts.views import _unique_attribute_key

    base = re.sub(r"-+", "_", slugify(label))[:64] or "field"
    key = _unique_attribute_key(account, base)
    CustomAttributeDef.objects.create(
        account=account,
        entity=CustomAttributeDef.Entity.CONTACT,
        key=key,
        type=field_type,
        label=label,
        options=options,
    )
    messages.success(
        request, f"Added “{label}”. You'll see it on every customer's profile."
    )
    return ""


@login_required
@require_POST
def field_archive(request, pk: int):
    from apps.contacts.attributes import archive_attribute_def

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not _is_admin(request, account):
        messages.error(
            request, "Only the business owner or an admin can change fields."
        )
        return redirect("contacts:fields")
    d = get_object_or_404(
        CustomAttributeDef,
        account=account,
        entity=CustomAttributeDef.Entity.CONTACT,
        pk=pk,
    )
    if request.POST.get("restore") == "1":
        d.archived_at = None
        d.save(update_fields=["archived_at"])
        messages.success(request, f"“{d.label or d.key}” is back.")
        return redirect("contacts:fields")
    try:
        archive_attribute_def(d, actor=f"user:{request.user.pk}")
    except ValueError:
        messages.error(
            request,
            f"“{d.label or d.key}” is used by an automation. Change that automation first.",
        )
    else:
        messages.success(
            request,
            f"Archived “{d.label or d.key}”. Saved values are kept; you can bring it back.",
        )
    return redirect("contacts:fields")
