"""Commerce service layer: create an Order and record when the business marks it paid.

Akilent never takes payments for a business. An order is a record of what a customer bought;
the business marks it paid once the customer has paid them, however they paid. Durable Events
are emitted and Workflows enrolled exactly like ``apps.crm.services``.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.commerce.models import Order, OrderItem
from apps.conversations.services import emit_event

logger = logging.getLogger(__name__)


def create_order(
    account,
    contact,
    items: list[dict],
    *,
    currency: str = "USD",
    conversation_id: str = "",
) -> Order:
    """Create an Order from a list of ``{"product": Product | None, "name": str,
    "unit_price": Decimal, "quantity": int}`` line items (product optional —
    a WhatsApp-taken order may not map to a catalog entry)."""
    if not items:
        raise ValueError("create_order requires at least one item")

    from apps.conversations import attribution

    conversation, method = attribution.decide(
        account, contact, public_id=conversation_id
    )
    with transaction.atomic():
        order = Order.objects.create(
            account=account,
            contact=contact,
            currency=currency,
            conversation=conversation,
        )
        attributed = attribution.record(order, conversation, method)
        if attributed is not None and attributed.originated_from_id:
            from apps.insights.actions import record_outcome_signal

            record_outcome_signal(attributed.originated_from, "order_created_at")
        for item in items:
            OrderItem.objects.create(
                order=order,
                product=item.get("product"),
                name=item.get("name")
                or (item["product"].name if item.get("product") else "Item"),
                unit_price=item["unit_price"],
                quantity=item.get("quantity", 1),
            )
        order.recompute_total()

    emit_event(
        account=account,
        type="order.created",
        occurred_at=order.created_at,
        source="commerce",
        subject_type="order",
        subject_id=order.public_id,
        payload={
            "contact_id": contact.public_id,
            "total": str(order.total),
            "currency": order.currency,
        },
    )
    _enroll_workflows(
        account,
        "order.created",
        contact,
        {"order_id": order.public_id},
        subject_key=f"order:{order.public_id}",
    )
    return order


PAYMENT_METHODS_OTHER = "other"

# Paid, refunded or cancelled: the business has already settled this order, so marking it
# paid again must not re-run what being paid sets off (workflows, the conversation note).
_SETTLED = (Order.Status.PAID, Order.Status.REFUNDED, Order.Status.CANCELLED)


class PaidOnline(ValueError):
    """The money for this order really was taken online (a retired payment link), so the
    business can't mark it unpaid."""


def _custom_method(account) -> str:
    """The business's own way to be paid, from its profile ("Zanaco bank transfer"), or ""."""
    from apps.accounts import profile

    return (getattr(profile.get_profile(account), "payment_other", "") or "").strip()[
        :40
    ]


def payment_method_choices(account) -> list[tuple[str, str]]:
    """How this business's customers pay, for the "Mark as paid" form: the methods in its
    business profile, its own method if it named one, then "Other". Every built-in method
    when the profile lists none at all."""
    from apps.accounts import profile

    listed = set(getattr(profile.get_profile(account), "payment_methods", None) or [])
    custom = _custom_method(account)
    keys = [k for k in profile.PAYMENT_METHODS if k in listed]
    if not keys and not custom:
        keys = list(profile.PAYMENT_METHODS)
    choices = [(k, profile.PAYMENT_METHODS[k]) for k in keys]
    if custom:
        choices.append((custom, custom))
    return choices + [(PAYMENT_METHODS_OTHER, "Other")]


def payment_method_label(method: str) -> str:
    """What ``Order.paid_method`` says: a built-in method's name, "Other", or the business's
    own method, stored as typed so a later profile change doesn't rewrite the order."""
    from apps.accounts import profile

    if method == PAYMENT_METHODS_OTHER:
        return "Other"
    return profile.PAYMENT_METHODS.get(method, method)


def paid_online(order: Order):
    """The payment that paid this order through a payment link before they were retired, or
    None. A late charge on a retired link isn't one: it's being refunded (refund_due)."""
    from apps.commerce.models import Payment

    return (
        order.payments.filter(status=Payment.Status.SUCCEEDED)
        .exclude(raw_payload__has_key="refund_due")
        .first()
    )


def refund_due(order: Order):
    """A late charge on a retired payment link that Akilent must refund, or None."""
    from apps.commerce.models import Payment

    return order.payments.filter(
        status=Payment.Status.SUCCEEDED, raw_payload__has_key="refund_due"
    ).first()


def _set_paid_state(
    order: Order, locked: Order, *, paid: bool, method="", reference=""
):
    """Write the paid fields on the locked row and copy them to the caller's object."""
    locked.status = Order.Status.PAID if paid else Order.Status.PENDING
    locked.paid_at = timezone.now() if paid else None
    locked.paid_method = method
    locked.paid_reference = reference
    locked.save(
        update_fields=[
            "status",
            "paid_at",
            "paid_method",
            "paid_reference",
            "updated_at",
        ]
    )
    for field in ("status", "paid_at", "paid_method", "paid_reference"):
        setattr(order, field, getattr(locked, field))


def mark_order_paid(
    order: Order, *, method: str = "", reference: str = "", actor=None
) -> bool:
    """Record that the customer has paid the business. Akilent never takes the money: the
    business tells us, however the customer paid (cash, mobile money, a bank transfer...).

    True when this call marked the order paid. Changed under a row lock, so a double click
    or two teammates at once mark it paid exactly once, and an order that is already paid,
    refunded or cancelled is left as it is. Marking it paid again after "Mark as not paid"
    restores the sale but doesn't re-run workflows or repeat the conversation note.
    """
    from apps.accounts import profile

    allowed = {
        *profile.PAYMENT_METHODS,
        PAYMENT_METHODS_OTHER,
        _custom_method(order.account),
    }
    method = method if method in allowed - {""} else ""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status in _SETTLED:
            order.status, order.paid_at = locked.status, locked.paid_at
            return False
        first_time = not _was_paid_before(locked)
        _set_paid_state(
            order,
            locked,
            paid=True,
            method=method,
            reference=(reference or "").strip()[:120],
        )
    if first_time:
        _after_order_paid(locked, actor=actor)
    else:
        _record_order_paid_outcome_signal(locked)
    return True


def mark_order_unpaid(order: Order, *, actor=None) -> bool:
    """Undo "Mark as paid" when it was a mistake: the order is open again and leaves revenue,
    including an insight's attributed revenue. Workflows that already ran on ``order.paid``
    aren't undone. True when it changed; raises PaidOnline when the money was really taken
    online through a retired payment link."""
    if paid_online(order) is not None:
        raise PaidOnline("This order was paid online, so it can't be marked unpaid.")
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.PAID:
            return False
        _set_paid_state(order, locked, paid=False)
    emit_event(
        account=locked.account,
        type="order.unpaid",
        occurred_at=timezone.now(),
        source="commerce",
        actor=getattr(actor, "username", "") or "",
        subject_type="order",
        subject_id=locked.public_id,
        payload={"contact_id": locked.contact.public_id},
    )
    _retract_order_paid_outcome_signal(locked)
    return True


def record_retired_link_charge(
    payment, *, transaction_id: str, raw_payload: dict | None = None
) -> bool:
    """A customer paid an order payment link sent before links were retired, so Akilent holds
    money that belongs to the business. The charge is recorded on its Payment (the money was
    taken) and flagged in the customer's conversation and the error log for a refund. The
    order is left as it is: the business collects payment itself. True the first time."""
    from apps.commerce.models import Payment

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked.status == Payment.Status.SUCCEEDED:
            return False
        locked.status = Payment.Status.SUCCEEDED
        locked.transaction_id = transaction_id
        locked.raw_payload = {**(raw_payload or {}), "refund_due": True}
        locked.save(
            update_fields=["status", "transaction_id", "raw_payload", "updated_at"]
        )
    order = locked.order
    logger.error(
        "commerce: payment %s (%s %s) arrived through a retired link for order %s; "
        "refund it to the customer",
        locked.public_id,
        locked.currency,
        locked.amount,
        order.public_id,
    )
    _note_in_conversation(
        order,
        f"A payment of {locked.currency} {locked.amount} arrived through an old Akilent "
        "payment link. Akilent no longer takes payments, so it will be refunded to the "
        "customer. Ask them to pay you directly, then mark the order paid.",
        {
            "kind": "payment.retired_link",
            "order_id": order.public_id,
            "payment_id": locked.public_id,
        },
    )
    return True


def _was_paid_before(order: Order) -> bool:
    from apps.conversations.models import Event

    return Event.objects.filter(
        account_id=order.account_id,
        type="order.paid",
        subject_type="order",
        subject_id=order.public_id,
    ).exists()


def _after_order_paid(order: Order, *, actor=None) -> None:
    """Everything an order being paid for the first time sets off: the event, ``order.paid``
    workflows, a note in the customer's conversation, and the insight outcome it may
    complete."""
    emit_event(
        account=order.account,
        type="order.paid",
        occurred_at=order.paid_at or timezone.now(),
        source="commerce",
        actor=getattr(actor, "username", "") or "",
        subject_type="order",
        subject_id=order.public_id,
        payload={
            "contact_id": order.contact.public_id,
            "total": str(order.total),
            "method": order.paid_method,
        },
    )
    _enroll_workflows(
        order.account,
        "order.paid",
        order.contact,
        {"order_id": order.public_id},
        subject_key=f"order:{order.public_id}",
    )
    how = payment_method_label(order.paid_method)
    _note_in_conversation(
        order,
        f"Order marked paid — {order.currency} {order.total}"
        + (f" ({how})" if how else ""),
        {"kind": "order.paid", "order_id": order.public_id},
    )
    _record_order_paid_outcome_signal(order)


def _note_in_conversation(order: Order, body: str, metadata: dict) -> None:
    """Mirror a commercial outcome into the customer's conversation, so the
    thread stays the whole story rather than the business having to go looking
    in Orders. Best-effort: a missing conversation or a failure here must never
    cost us the order record."""
    from apps.conversations.services import record_system_message

    try:
        record_system_message(
            account=order.account,
            contact=order.contact,
            body=body,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "could not mirror order %s into a conversation", order.public_id
        )


def _order_rec_log(order):
    """The accepted recommendation this order is attributed to, through the canonical
    ConversationAttribution provenance chain (``insight_for_order``, the traversal the UI
    uses), or None."""
    from apps.insights.actions import insight_for_order
    from apps.insights.models import RecommendationLog

    insight = insight_for_order(order)
    if insight is None:
        return None
    return (
        RecommendationLog.objects.filter(
            insight=insight, status=RecommendationLog.Status.ACCEPTED
        )
        .order_by("-acted_at")
        .first()
    )


def _record_order_paid_outcome_signal(order) -> None:
    """Record order_paid_at, with the revenue, on the recommendation this order is attributed
    to. Safe to call more than once: record_outcome_signal() is idempotent.

    order_created_at is recorded in create_order, at creation time, not here: timestamping
    it now would be wrong, and a later call couldn't fix it.
    """
    from apps.insights.actions import record_outcome_signal

    try:
        rec_log = _order_rec_log(order)
        if rec_log is not None:
            record_outcome_signal(
                rec_log,
                "order_paid_at",
                revenue=str(order.total),
                currency=order.currency or "",
            )
    except Exception:
        logger.exception(
            "_record_order_paid_outcome_signal failed for order=%s", order.pk
        )


def _retract_order_paid_outcome_signal(order) -> None:
    """An order marked paid by mistake no longer counts as a paid conversion."""
    from apps.insights.actions import retract_outcome_signal

    try:
        rec_log = _order_rec_log(order)
        if rec_log is not None:
            retract_outcome_signal(rec_log, "order_paid_at")
    except Exception:
        logger.exception(
            "_retract_order_paid_outcome_signal failed for order=%s", order.pk
        )


def _enroll_workflows(
    account, trigger_type: str, contact, context: dict, subject_key: str = ""
) -> None:
    try:
        from apps.automation.workflow_engine import enroll_for_trigger

        enroll_for_trigger(
            account.id, trigger_type, contact, context=context, subject_key=subject_key
        )
    except Exception:
        logger.exception("_enroll_workflows failed for trigger=%s", trigger_type)
