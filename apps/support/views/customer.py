import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.utils import get_current_account
from apps.support.models import SupportTicket
from apps.support.services import ticket as ticket_service

logger = logging.getLogger(__name__)


@login_required
def portal(request):
    account = get_current_account(request)
    tickets = (
        SupportTicket.objects.filter(account=account)
        .select_related("category", "queue")
        .order_by("-created_at")
    )
    return render(request, "support/portal.html", {"tickets": tickets})


@login_required
def portal_create(request):
    from apps.support.models import SupportCategory

    account = get_current_account(request)
    categories = SupportCategory.objects.filter(parent__isnull=False).order_by("name")

    if request.method == "POST":
        subject = request.POST.get("subject", "").strip()
        description = request.POST.get("description", "").strip()
        category_slug = request.POST.get("category") or None
        priority = request.POST.get("priority", "p3")

        if not subject or not description:
            return render(
                request,
                "support/portal_create.html",
                {
                    "categories": categories,
                    "error": "Subject and description are required.",
                    "post": request.POST,
                },
            )

        ticket = ticket_service.create_ticket(
            account=account,
            submitted_by=request.user,
            subject=subject,
            description=description,
            category_slug=category_slug,
            priority=priority,
        )

        # Attach a reference if context params were provided
        payment_id = request.GET.get("payment")
        if payment_id:
            try:
                from apps.payments.models import Payment

                payment = Payment.objects.filter(account=account, pk=payment_id).first()
                if payment:
                    ticket_service.add_reference(
                        ticket=ticket, obj=payment, label="Payment"
                    )
            except Exception:
                logger.exception("portal_create: failed to attach payment reference")

        return redirect("support:portal-ticket", ticket_number=ticket.ticket_number)

    return render(
        request,
        "support/portal_create.html",
        {
            "categories": categories,
            "post": {},
        },
    )


@login_required
def portal_ticket(request, ticket_number: str):
    account = get_current_account(request)
    ticket = get_object_or_404(
        SupportTicket, ticket_number=ticket_number, account=account
    )

    messages = (
        ticket.messages.filter(is_from_customer=True)
        .union(ticket.messages.filter(is_from_customer=False))
        .order_by("created_at")
    )

    if request.method == "POST":
        body = request.POST.get("body", "").strip()
        if body:
            ticket_service.add_message(
                ticket=ticket,
                body=body,
                author=request.user,
                is_from_customer=True,
            )
        return redirect("support:portal-ticket", ticket_number=ticket.ticket_number)

    messages = ticket.messages.order_by("created_at")
    return render(
        request,
        "support/portal_ticket.html",
        {
            "ticket": ticket,
            "messages": messages,
        },
    )
