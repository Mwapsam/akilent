"""Commerce service layer: create an Order, request payment for it via
Flutterwave (the same gateway already integrated for platform billing —
Phase 3's "one real provider"), and record the outcome — emitting durable
Events and enrolling Workflows exactly like ``apps.crm.services``.
"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from apps.commerce.models import Order, OrderItem, Payment, Product
from apps.conversations.services import emit_event

logger = logging.getLogger(__name__)


def create_order(account, contact, items: list[dict], *, currency: str = "USD",
                 conversation_id: str = "") -> Order:
    """Create an Order from a list of ``{"product": Product | None, "name": str,
    "unit_price": Decimal, "quantity": int}`` line items (product optional —
    a WhatsApp-taken order may not map to a catalog entry)."""
    if not items:
        raise ValueError("create_order requires at least one item")

    from apps.conversations.attribution import resolve_conversation

    with transaction.atomic():
        order = Order.objects.create(
            account=account, contact=contact, currency=currency,
            conversation=resolve_conversation(account, contact, public_id=conversation_id),
        )
        for item in items:
            OrderItem.objects.create(
                order=order,
                product=item.get("product"),
                name=item.get("name") or (item["product"].name if item.get("product") else "Item"),
                unit_price=item["unit_price"],
                quantity=item.get("quantity", 1),
            )
        order.recompute_total()

    emit_event(
        account=account, type="order.created", occurred_at=order.created_at,
        source="commerce", subject_type="order", subject_id=order.public_id,
        payload={"contact_id": contact.public_id, "total": str(order.total), "currency": order.currency},
    )
    _enroll_workflows(account, "order.created", contact, {"order_id": order.public_id},
                      subject_key=f"order:{order.public_id}")
    return order


def request_payment(order: Order, *, redirect_url: str) -> Payment:
    """Create a pending Payment and a Flutterwave hosted checkout link for it.

    Mirrors ``apps.billing``'s Flutterwave flow (same client, same
    verify-then-trust webhook pattern) but tagged with ``order_id`` in
    ``meta`` so the shared webhook can route commerce charges separately
    from subscription charges.
    """
    from apps.billing.flutterwave import FlutterwaveError, get_fw_client

    payment = Payment.objects.create(
        account=order.account, order=order, amount=order.total, currency=order.currency,
    )
    order.status = Order.Status.AWAITING_PAYMENT
    order.save(update_fields=["status", "updated_at"])

    try:
        link = get_fw_client().initialize_payment(
            tx_ref=f"order-{order.public_id}-{uuid.uuid4().hex[:8]}",
            amount=order.total, currency=order.currency,
            customer_email=order.contact.email or "customer@example.com",
            customer_name=order.contact.full_name or str(order.contact),
            redirect_url=redirect_url,
            meta={"order_id": order.public_id, "payment_id": payment.public_id},
        )
        payment.checkout_url = link
        payment.save(update_fields=["checkout_url", "updated_at"])
    except FlutterwaveError:
        logger.exception("request_payment: Flutterwave initialization failed for order=%s", order.pk)

    return payment


def mark_paid(payment: Payment, *, transaction_id: str, raw_payload: dict | None = None) -> Order:
    """Idempotently mark a Payment succeeded and its Order paid.

    Safe to call twice for the same payment (e.g. a replayed webhook that
    slipped past the outer ``ProcessedWebhookEvent`` dedupe) — a
    already-succeeded Payment is a no-op.
    """
    if payment.status == Payment.Status.SUCCEEDED:
        return payment.order

    order = payment.order
    payment.status = Payment.Status.SUCCEEDED
    payment.transaction_id = transaction_id
    payment.raw_payload = raw_payload or {}
    payment.save(update_fields=["status", "transaction_id", "raw_payload", "updated_at"])

    order.status = Order.Status.PAID
    order.paid_at = timezone.now()
    order.save(update_fields=["status", "paid_at", "updated_at"])

    emit_event(
        account=order.account, type="payment.succeeded", occurred_at=order.paid_at,
        source="commerce", source_event_id=transaction_id, subject_type="payment", subject_id=payment.public_id,
        payload={"order_id": order.public_id, "amount": str(payment.amount)},
    )
    emit_event(
        account=order.account, type="order.paid", occurred_at=order.paid_at,
        source="commerce", subject_type="order", subject_id=order.public_id,
        payload={"contact_id": order.contact.public_id, "total": str(order.total)},
    )
    _enroll_workflows(order.account, "order.paid", order.contact, {"order_id": order.public_id},
                      subject_key=f"order:{order.public_id}")
    _note_in_conversation(
        order, f"Payment received — {order.currency} {order.total}",
        {"kind": "payment.succeeded", "order_id": order.public_id},
    )
    return order


def _note_in_conversation(order: Order, body: str, metadata: dict) -> None:
    """Mirror a commercial outcome into the customer's conversation, so the
    thread stays the whole story rather than the business having to go looking
    in Orders. Best-effort: a missing conversation or a failure here must never
    cost us the payment record."""
    from apps.conversations.services import record_system_message

    try:
        record_system_message(
            account=order.account, contact=order.contact, body=body, metadata=metadata,
        )
    except Exception:
        logger.exception("could not mirror order %s into a conversation", order.public_id)


def mark_failed(payment: Payment, *, error: str = "") -> Order:
    order = payment.order
    payment.status = Payment.Status.FAILED
    payment.raw_payload = {**payment.raw_payload, "error": error}
    payment.save(update_fields=["status", "raw_payload", "updated_at"])

    order.status = Order.Status.FAILED
    order.save(update_fields=["status", "updated_at"])

    emit_event(
        account=order.account, type="payment.failed", occurred_at=timezone.now(),
        source="commerce", subject_type="payment", subject_id=payment.public_id,
        payload={"order_id": order.public_id, "error": error},
    )
    return order


def _enroll_workflows(account, trigger_type: str, contact, context: dict, subject_key: str = "") -> None:
    try:
        from apps.automation.workflow_engine import enroll_for_trigger

        enroll_for_trigger(account.id, trigger_type, contact, context=context, subject_key=subject_key)
    except Exception:
        logger.exception("_enroll_workflows failed for trigger=%s", trigger_type)
