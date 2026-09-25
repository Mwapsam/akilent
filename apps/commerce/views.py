"""Orders: a thin projection over Order/Payment, following the same UX
rules as Inbox/Sales — one primary action per screen, teaching empty states.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.utils import get_current_account
from apps.commerce.models import Order
from apps.contacts.models import Contact
from apps.conversations import attribution
from apps.core.actions import ActionError, run_action
from apps.core.module_gate import module_required


@login_required
@module_required("commerce")
def orders(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    view = (request.GET.get("view") or "needs_attention").strip()
    qs = Order.objects.filter(account=account).select_related("contact")
    if view == "all":
        pass
    else:
        view = "needs_attention"
        qs = qs.filter(status__in=[Order.Status.PENDING, Order.Status.AWAITING_PAYMENT])

    return render(request, "commerce/orders.html", {
        "account": account, "page": qs, "view": view,
        "needs_attention_count": Order.objects.filter(
            account=account, status__in=[Order.Status.PENDING, Order.Status.AWAITING_PAYMENT],
        ).count(),
        "all_count": Order.objects.filter(account=account).count(),
        "recent_conversations": attribution.recent_choices(account),
    })


@login_required
@module_required("commerce")
def create_order_view(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        # See apps.crm.views.create_lead_view — same "return to the conversation" support.
        next_url = request.POST.get("next") or None

        query = (request.POST.get("contact") or "").strip()
        contact = Contact.objects.filter(account=account).filter(
            Q(phone=query) | Q(email__iexact=query)
        ).first() if query else None
        try:
            conversation, contact = attribution.picked_conversation(
                account, contact, request.POST.get("conversation", ""))
        except attribution.PickerError as exc:
            messages.error(request, str(exc))
            return redirect(next_url or "commerce:orders")
        if contact is None:
            messages.error(
                request,
                "No customer found with that phone or email. Check the number/email, "
                "or add them from Contacts first.",
            )
            return redirect(next_url or "commerce:orders")

        name = (request.POST.get("name") or "").strip() or "Item"
        try:
            unit_price = Decimal(request.POST.get("unit_price") or "0")
            quantity = int(request.POST.get("quantity") or "1")
        except (InvalidOperation, ValueError):
            messages.error(request, "Enter a valid price and quantity.")
            return redirect(next_url or "commerce:orders")

        try:
            result = run_action(
                "create_order", {"account": account}, account=account, contact=contact,
                items=[{"name": name, "unit_price": unit_price, "quantity": quantity}],
                currency=(request.POST.get("currency") or "USD").strip().upper(),
                conversation_id=conversation.public_id if conversation else "",
            )
        except ActionError as exc:
            messages.error(request, str(exc))
            return redirect(next_url or "commerce:orders")

        messages.success(request, "Order created.")
        if next_url:
            return redirect(next_url)
        return redirect("commerce:detail", public_id=result["order_id"])

    return redirect("commerce:orders")


@login_required
@module_required("commerce")
def order_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    order = get_object_or_404(Order, account=account, public_id=public_id)

    if request.method == "POST" and request.POST.get("action") == "request_payment":
        try:
            redirect_url = request.build_absolute_uri(
                reverse("commerce:detail", args=[order.public_id])
            )
            run_action("request_payment", {"account": account}, order=order, redirect_url=redirect_url)
            messages.success(request, "Payment link created.")
        except ActionError as exc:
            messages.error(request, str(exc))
        return redirect("commerce:detail", public_id=public_id)

    return render(request, "commerce/order_detail.html", {
        "account": account, "order": order,
        "items": order.items.all(),
        "payment": order.payments.order_by("-created_at").first(),
    })
