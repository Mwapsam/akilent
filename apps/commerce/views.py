"""Orders: a thin projection over Order/Payment, following the same UX
rules as Inbox/Sales — one primary action per screen, teaching empty states.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.utils import get_current_account
from apps.commerce.models import Order
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
    })


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
