"""Dashboard: contact list + customer profile (Phase 4.4)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.contacts.models import Contact
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

    events = contact.events.all()[:100]
    messages_to = (
        EmailMessage.objects.filter(account=account, to_email__iexact=contact.email)
        .order_by("-created_at")[:50]
    )
    return render(request, "contacts/detail.html", {
        "account": account,
        "contact": contact,
        "events": events,
        "messages_to": messages_to,
        "lists": contact.lists.all(),
    })
