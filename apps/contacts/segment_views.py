"""Saved segments: a rule-based audience ("opted in, active in the last 30 days")
that is re-evaluated whenever it's used — unlike a list, whose members are fixed.

Builder rules are one AND/OR group of conditions (``apps.contacts.segments``
compiles them); the screens show a live count and a sample as the owner edits.
"""

from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts import reachability, segments
from apps.contacts.models import Contact, ContactList, ContactListMembership, Segment

_SAMPLE = 8


def _members(definition: dict, account):
    """Matching customers as a plain (non-distinct) queryset, safe to annotate and count."""
    matched = segments.contacts_for(definition, account)
    return Contact.objects.filter(account=account, pk__in=matched.values("pk"))


def _definition_from_post(request) -> tuple[dict, str]:
    """``(definition, error)``: the builder's JSON, validated by compiling it."""
    raw = request.POST.get("definition") or "{}"
    try:
        definition = json.loads(raw)
    except ValueError:
        return {}, "Those rules couldn't be read. Try again."
    if not isinstance(definition, dict):
        return {}, "Those rules couldn't be read. Try again."
    conditions = [c for c in definition.get("conditions") or [] if c]
    definition = {"op": definition.get("op", "and"), "conditions": conditions}
    if not conditions:
        return definition, "Add at least one rule."
    try:
        segments.build_q(definition)
    except segments.SegmentError as exc:
        return definition, f"One of the rules isn't complete: {exc}."
    return definition, ""


@login_required
def segment_index(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rows = []
    for seg in Segment.objects.filter(account=account).order_by("name"):
        try:
            reach = reachability.counts(_members(seg.definition or {}, account))
            broken = False
        except segments.SegmentError:
            reach, broken = {"total": 0}, True
        rows.append({"segment": seg, "reach": reach, "broken": broken})
    return render(
        request,
        "contacts/segments.html",
        {"account": account, "rows": rows},
    )


def _render_form(request, account, segment, definition, name, error="", status=200):
    return render(
        request,
        "contacts/segment_form.html",
        {
            "account": account,
            "segment": segment,
            "name": name,
            "definition": definition or {"op": "and", "conditions": []},
            "fields": segments.builder_fields(account),
            "error": error,
            "used_by": segment.whatsapp_campaigns.count() if segment else 0,
        },
        status=status,
    )


@login_required
def segment_form(request, pk: int | None = None):
    """Create or edit a segment."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    segment = (
        get_object_or_404(Segment, account=account, pk=pk) if pk is not None else None
    )
    if request.method != "POST":
        definition = segment.definition if segment else None
        if segment is None and request.GET.get("from") == "filter":
            definition = segments.from_contact_filters(request.GET, account)
        return _render_form(
            request, account, segment, definition, segment.name if segment else ""
        )

    name = (request.POST.get("name") or "").strip()[:150]
    definition, error = _definition_from_post(request)
    if not error and not name:
        error = "Give the segment a name."
    if not error:
        slug = slugify(name)[:160] or "segment"
        clash = Segment.objects.filter(account=account, slug=slug)
        if segment is not None:
            clash = clash.exclude(pk=segment.pk)
        if clash.exists():
            error = f"You already have a segment called “{name}”."
    if error:
        return _render_form(
            request, account, segment, definition, name, error=error, status=400
        )
    try:
        with transaction.atomic():
            if segment is None:
                segment = Segment.objects.create(
                    account=account, name=name, definition=definition
                )
            else:
                segment.name = name
                segment.slug = slugify(name)[:160]
                segment.definition = definition
                segment.save()
    except IntegrityError:
        return _render_form(
            request,
            account,
            segment,
            definition,
            name,
            error=f"You already have a segment called “{name}”.",
            status=400,
        )
    messages.success(request, f"Saved “{segment.name}”.")
    return redirect("contacts:segments")


@login_required
@require_POST
def segment_preview(request):
    """HTMX: how many match the rules being edited, who can be reached, and a sample."""
    account = get_current_account(request)
    if account is None:
        return render(request, "contacts/_segment_preview.html", {"error": ""})
    definition, error = _definition_from_post(request)
    if error:
        return render(request, "contacts/_segment_preview.html", {"error": error})
    members = _members(definition, account)
    return render(
        request,
        "contacts/_segment_preview.html",
        {
            "reach": reachability.counts(members),
            "sample": list(
                reachability.annotate(members).order_by("-last_engaged_at", "-pk")[
                    :_SAMPLE
                ]
            ),
        },
    )


@login_required
@require_POST
def segment_delete(request, pk: int):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    segment = get_object_or_404(Segment, account=account, pk=pk)
    if segment.whatsapp_campaigns.exists():
        messages.error(
            request,
            f"“{segment.name}” is used by a campaign, so it's kept to show who that "
            "campaign went to.",
        )
        return redirect("contacts:segments")
    name = segment.name
    segment.delete()
    messages.success(request, f"Deleted “{name}”. No customers were removed.")
    return redirect("contacts:segments")


@login_required
@require_POST
def segment_to_list(request, pk: int):
    """Freeze who matches now into a new list (members won't change after this)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    segment = get_object_or_404(Segment, account=account, pk=pk)
    try:
        ids = list(
            _members(segment.definition or {}, account).values_list("pk", flat=True)
        )
    except segments.SegmentError:
        messages.error(request, "Fix this segment's rules first.")
        return redirect("contacts:segment_edit", pk=pk)
    base = f"{segment.name} ({timezone.localdate():%d %b %Y})"[:140]
    name, n = base, 2
    while ContactList.objects.filter(
        account=account, slug=slugify(name)[:160]
    ).exists():
        name, n = f"{base} {n}", n + 1
    with transaction.atomic():
        clist = ContactList.objects.create(account=account, name=name)
        ContactListMembership.objects.bulk_create(
            [ContactListMembership(contact_list=clist, contact_id=cid) for cid in ids],
            ignore_conflicts=True,
            batch_size=1000,
        )
    messages.success(
        request,
        f"Made the list “{clist.name}” with {len(ids):,} customer{'s' if len(ids) != 1 else ''}.",
    )
    return redirect("contacts:list_detail", pk=clist.pk)
