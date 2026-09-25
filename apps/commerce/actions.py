"""Commerce actions, registered into the shared Action Registry (``apps.core.actions``)."""
from __future__ import annotations

from apps.core.actions import Action, ActionError, register


class CreateOrderAction(Action):
    name = "create_order"
    module = "commerce"
    scope_kwarg = "account"

    def input_schema(self) -> dict:
        return {"required": ["account", "contact", "items"], "optional": ["currency", "conversation_id"]}

    def execute(self, context: dict, *, account, contact, items: list, currency: str = "USD",
                conversation_id: str = "") -> dict:
        from apps.commerce.services import create_order

        if not items:
            raise ActionError("create_order requires at least one item")
        order = create_order(account, contact, items, currency=currency, conversation_id=conversation_id)
        return {"order_id": order.public_id, "total": str(order.total)}


class RequestPaymentAction(Action):
    name = "request_payment"
    module = "commerce"
    scope_kwarg = "order"

    def input_schema(self) -> dict:
        return {"required": ["order", "redirect_url"]}

    def execute(self, context: dict, *, order, redirect_url: str) -> dict:
        from apps.commerce.services import request_payment

        payment = request_payment(order, redirect_url=redirect_url)
        return {"payment_id": payment.public_id, "checkout_url": payment.checkout_url}


class LookupProductsAction(Action):
    """Read-only: active catalog products whose name matches the words in ``query`` (at most 5)."""

    name = "lookup_products"
    module = "commerce"
    scope_kwarg = "account"
    LIMIT = 5

    def input_schema(self) -> dict:
        return {"required": ["account", "query"]}

    def execute(self, context: dict, *, account, query: str) -> dict:
        from django.db.models import Q

        from apps.commerce.models import Product

        words = [w for w in str(query or "").split() if len(w) > 1][:6]
        if not words:
            return {"products": []}
        match = Q()
        for word in words:
            match |= Q(name__icontains=word)
        products = Product.objects.filter(account=account, is_active=True).filter(match)[: self.LIMIT]
        return {"products": [{"name": p.name, "price": str(p.price), "currency": p.currency} for p in products]}


register(CreateOrderAction())
register(RequestPaymentAction())
register(LookupProductsAction())
