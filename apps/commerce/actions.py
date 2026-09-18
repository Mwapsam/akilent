"""Commerce actions, registered into the shared Action Registry (``apps.core.actions``)."""
from __future__ import annotations

from apps.core.actions import Action, ActionError, register


class CreateOrderAction(Action):
    name = "create_order"

    def input_schema(self) -> dict:
        return {"required": ["account", "contact", "items"], "optional": ["currency"]}

    def execute(self, context: dict, *, account, contact, items: list, currency: str = "USD") -> dict:
        from apps.commerce.services import create_order

        if not items:
            raise ActionError("create_order requires at least one item")
        order = create_order(account, contact, items, currency=currency)
        return {"order_id": order.public_id, "total": str(order.total)}


class RequestPaymentAction(Action):
    name = "request_payment"

    def input_schema(self) -> dict:
        return {"required": ["order", "redirect_url"]}

    def execute(self, context: dict, *, order, redirect_url: str) -> dict:
        from apps.commerce.services import request_payment

        payment = request_payment(order, redirect_url=redirect_url)
        return {"payment_id": payment.public_id, "checkout_url": payment.checkout_url}


register(CreateOrderAction())
register(RequestPaymentAction())
