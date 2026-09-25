"""Phase 3 minimal Commerce: Product catalog + Order + payment status tracking.

Deliberately thin per the plan: no inventory, variants, multi-warehouse, or
tax rules yet. ``OrderItem`` snapshots ``name``/``unit_price`` at order time
so a later Product price change never rewrites history. Every object carries
an explicit ``account`` FK and links to the canonical ``apps.contacts.Contact``.
"""
from __future__ import annotations

import secrets

from django.db import models


def _order_public_id() -> str:
    return "ord_" + secrets.token_hex(12)


def _payment_public_id() -> str:
    return "pay_" + secrets.token_hex(12)


class Product(models.Model):
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="USD")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "slug"], name="unique_product_slug_per_account"),
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    public_id = models.CharField(max_length=40, unique=True, default=_order_public_id, editable=False)
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="orders")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="orders")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, default="USD")
    # The conversation this came from, fixed when it was created so a later reply never
    # rewrites history. Null when nothing in the inbox led here (walk-in, manual entry).
    conversation = models.ForeignKey(
        "conversations.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "contact"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order {self.public_id} ({self.get_status_display()})"

    def recompute_total(self) -> None:
        self.total = sum((item.line_total for item in self.items.all()), 0)
        self.save(update_fields=["total", "updated_at"])


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_items")

    # Snapshotted at order time — a later Product price/name change must
    # never rewrite an already-placed order's history.
    name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    @property
    def line_total(self):
        return self.unit_price * self.quantity

    def __str__(self):
        return f"{self.quantity} x {self.name}"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    public_id = models.CharField(max_length=40, unique=True, default=_payment_public_id, editable=False)
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="payments")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")

    provider = models.CharField(max_length=30, default="flutterwave")
    transaction_id = models.CharField(max_length=100, blank=True, default="")
    checkout_url = models.URLField(blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="USD")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    raw_payload = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["provider", "transaction_id"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Payment {self.public_id} ({self.get_status_display()})"
