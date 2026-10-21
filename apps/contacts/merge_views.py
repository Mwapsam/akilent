"""Merge two customer records (spec S7 merge banner + "Merge with…" flow)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.contacts import merge, reachability
from apps.contacts.models import Contact

_COMPARE_FIELDS = (
    ("full_name", "Name"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("source", "Came from"),
    ("first_seen", "First seen"),
    ("last_engaged_at", "Last active"),
)


def _counts_line(contact: Contact) -> str:
    """ "3 conversations · 1 order · 2 lists" — only what they have."""
    parts = []
    for n, one, many in (
        (contact.conversations.count(), "conversation", "conversations"),
        (contact.orders.count(), "order", "orders"),
        (contact.leads.count(), "lead", "leads"),
        (contact.lists.count(), "list", "lists"),
        (contact.tags.count(), "tag", "tags"),
    ):
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    return " · ".join(parts)


def _summary(contact: Contact) -> dict:
    """What each side brings, so the owner can see nothing is lost."""
    return {
        "contact": contact,
        "wa_state": reachability.state_for(contact),
        "rows": [(label, getattr(contact, f)) for f, label in _COMPARE_FIELDS],
        "summary": _counts_line(contact),
        "instagram": [
            u
            for u in contact.instagram_contacts.values_list("username", flat=True)
            if u
        ],
    }


@login_required
def contact_merge(request, public_id: str):
    """Choose who to merge with (search + suggestions), then compare and confirm."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)

    other = None
    if request.GET.get("with"):
        other = get_object_or_404(
            Contact.objects.exclude(pk=contact.pk),
            account=account,
            public_id=request.GET["with"],
        )

    if request.method == "POST":
        other = get_object_or_404(
            Contact.objects.exclude(pk=contact.pk),
            account=account,
            public_id=request.POST.get("other", ""),
        )
        keep_this = request.POST.get("keep") != "other"
        primary, duplicate = (contact, other) if keep_this else (other, contact)
        try:
            result = merge.merge_contacts(primary, duplicate, actor=request.user)
        except merge.MergeError as exc:
            messages.error(request, str(exc))
            return redirect(f"{request.path}?with={other.public_id}")
        note = ""
        if result.closed_leads:
            note = " Their second open lead was closed as lost."
        messages.success(
            request,
            f"Merged into one customer. Everything from both records is here.{note}",
        )
        return redirect("contacts:detail", public_id=primary.public_id)

    results = []
    q = (request.GET.get("q") or "").strip()
    if q and other is None:
        results = list(
            Contact.objects.filter(account=account)
            .exclude(pk=contact.pk)
            .filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
            )[:20]
        )
    return render(
        request,
        "contacts/merge.html",
        {
            "account": account,
            "contact": contact,
            "other": other,
            "q": q,
            "results": results,
            "suggestions": merge.suggested_duplicates(contact) if other is None else [],
            "left": _summary(contact) if other else None,
            "right": _summary(other) if other else None,
        },
    )


@login_required
@require_POST
def contact_not_duplicate(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(Contact, account=account, public_id=public_id)
    other = get_object_or_404(
        Contact, account=account, public_id=request.POST.get("other", "")
    )
    merge.dismiss_suggestion(contact, other)
    messages.success(request, "Got it — we won't suggest these two again.")
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nxt)
    return redirect("contacts:detail", public_id=public_id)


@login_required
def duplicates(request):
    """Every likely duplicate in the business, to tidy up in one place."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    seen: set[int] = set()
    pairs = []
    for c in Contact.objects.filter(account=account).order_by(
        "-last_engaged_at", "-pk"
    )[:2000]:
        if c.pk in seen:
            continue
        for other in merge.suggested_duplicates(c, limit=3):
            if other.pk in seen:
                continue
            pairs.append((c, other))
            seen.update({c.pk, other.pk})
            break
        if len(pairs) >= 50:
            break
    return render(
        request, "contacts/duplicates.html", {"account": account, "pairs": pairs}
    )
