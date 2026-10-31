"""Orders: a thin projection over Order, following the same UX
rules as Inbox/Sales — one primary action per screen, teaching empty states.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.utils import get_current_account
from apps.commerce.models import Order
from apps.commerce.services import (
    PaidOnline,
    mark_order_paid,
    mark_order_unpaid,
    paid_online,
    payment_method_choices,
    payment_method_label,
    refund_due,
)
from apps.contacts.models import Contact
from apps.conversations import attribution
from apps.core.actions import ActionError, run_action
from apps.core.module_gate import module_required

ORDERS_PER_PAGE = 50


def default_currency(account) -> str:
    """What a new order's currency starts as: the business's latest order's, else USD.

    Still shown and editable on every order form, so the currency is always explicit."""
    latest = (
        Order.objects.filter(account=account)
        .exclude(currency="")
        .order_by("-created_at", "-pk")
        .values_list("currency", flat=True)
        .first()
    )
    return latest or "USD"


@login_required
@module_required("orders")
def orders(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    view = (request.GET.get("view") or "needs_attention").strip()
    qs = (
        Order.objects.filter(account=account)
        .select_related("contact", "conversation")
        .prefetch_related("items")
        .order_by("-created_at", "-pk")
    )
    # ?contact=<public_id>: one customer's orders (the profile's "Orders" count links here).
    # An id that isn't this business's customer is ignored rather than showing an empty list.
    for_contact = (
        Contact.objects.filter(
            account=account, public_id=request.GET.get("contact") or ""
        ).first()
        if request.GET.get("contact")
        else None
    )
    if for_contact is not None:
        qs = qs.filter(contact=for_contact)
    if view == "all":
        pass
    else:
        view = "needs_attention"
        qs = qs.filter(status__in=[Order.Status.PENDING, Order.Status.AWAITING_PAYMENT])

    return render(
        request,
        "commerce/orders.html",
        {
            "account": account,
            "page": Paginator(qs, ORDERS_PER_PAGE).get_page(request.GET.get("page")),
            "view": view,
            "for_contact": for_contact,
            "needs_attention_count": Order.objects.filter(
                account=account,
                status__in=[Order.Status.PENDING, Order.Status.AWAITING_PAYMENT],
            ).count(),
            "all_count": Order.objects.filter(account=account).count(),
            "recent_conversations": attribution.recent_choices(account),
            "default_currency": default_currency(account),
        },
    )


@login_required
@module_required("orders")
def create_order_view(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        # See apps.crm.views.create_lead_view — same "return to the conversation" support.
        next_url = request.POST.get("next") or None
        # Only ever return to a page on this site, never an address a form was handed.
        if next_url and not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = None

        # From a lead's page ("Ready to buy? Create their order"): the order is for that
        # customer and their chat, and the lead is marked bought once it exists.
        lead = None
        if request.POST.get("lead"):
            from apps.crm.models import Lead

            lead = Lead.objects.filter(
                account=account, public_id=request.POST["lead"]
            ).first()
            if lead is None:
                messages.error(request, "That interested customer wasn't found.")
                return redirect(next_url or "commerce:orders")

        contact: Contact | None
        if lead is not None:
            contact = lead.contact
            conversation = lead.conversation
        else:
            query = (request.POST.get("contact") or "").strip()
            contact = (
                Contact.objects.filter(account=account)
                .filter(Q(phone=query) | Q(email__iexact=query))
                .first()
                if query
                else None
            )
            try:
                conversation, contact = attribution.picked_conversation(
                    account, contact, request.POST.get("conversation", "")
                )
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
                "create_order",
                {"account": account},
                account=account,
                contact=contact,
                items=[{"name": name, "unit_price": unit_price, "quantity": quantity}],
                currency=(request.POST.get("currency") or "USD").strip().upper(),
                conversation_id=conversation.public_id if conversation else "",
            )
        except ActionError as exc:
            messages.error(request, str(exc))
            return redirect(next_url or "commerce:orders")

        if lead is not None:
            from apps.crm.services import mark_lead_bought

            order = Order.objects.get(account=account, public_id=result["order_id"])
            mark_lead_bought(lead, order, actor=request.user)
        messages.success(request, "Order created.")
        if next_url:
            return redirect(next_url)
        return redirect("commerce:detail", public_id=result["order_id"])

    return redirect("commerce:orders")


@login_required
@module_required("orders")
def order_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    order = get_object_or_404(Order, account=account, public_id=public_id)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_paid":
            if mark_order_paid(
                order,
                method=request.POST.get("method", ""),
                reference=request.POST.get("reference", ""),
                actor=request.user,
            ):
                messages.success(request, "Order marked paid.")
            else:
                messages.info(
                    request,
                    f"This order is already {order.get_status_display().lower()}.",
                )
        elif action == "mark_unpaid":
            try:
                if mark_order_unpaid(order, actor=request.user):
                    messages.success(request, "Order marked as not paid.")
            except PaidOnline as exc:
                messages.error(request, str(exc))
        return redirect("commerce:detail", public_id=public_id)

    # The interested customer this order was created from (mark_lead_bought).
    from apps.crm.models import Lead

    from_lead = (
        Lead.objects.filter(account=account, attributes__order=order.public_id)
        .select_related("contact")
        .first()
    )

    origin_insight = None
    try:
        from apps.insights.actions import insight_for_order

        origin_insight = insight_for_order(order)
    except Exception:
        pass  # insights app unavailable — panel simply won't show attribution

    return render(
        request,
        "commerce/order_detail.html",
        {
            "account": account,
            "order": order,
            "items": order.items.all(),
            # A payment taken through a retired payment link, shown as history; such an
            # order can't be marked unpaid (mark_order_unpaid refuses it too).
            "online_payment": paid_online(order),
            "refund_payment": refund_due(order),
            "method_choices": payment_method_choices(account),
            "paid_method_label": payment_method_label(order.paid_method),
            "origin_insight": origin_insight,
            "from_lead": from_lead,
        },
    )
