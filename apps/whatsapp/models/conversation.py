from datetime import timedelta

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .contact import CrmBinding, WhatsAppContact


class Conversation(models.Model):
    SESSION_GAP = timedelta(hours=24)
    SESSION_CLOSE_GRACE = timedelta(hours=24)

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)
    contact = models.ForeignKey(
        WhatsAppContact, on_delete=models.CASCADE, related_name="conversations"
    )

    crm_binding = models.ForeignKey(
        CrmBinding, on_delete=models.SET_NULL, blank=True, null=True
    )

    is_open = models.BooleanField(default=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    window_expires_at = models.DateTimeField(blank=True, null=True)

    last_message_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["contact", "is_open"]),
            models.Index(fields=["is_open", "window_expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "contact"],
                condition=models.Q(is_open=True),
                name="one_open_conversation_per_contact",
            ),
        ]

    @property
    def window_is_open(self) -> bool:
        return bool(self.window_expires_at and self.window_expires_at > timezone.now())

    def register_inbound(self, at):
        self.last_message_at = at
        self.window_expires_at = at + self.SESSION_GAP
        self.save(update_fields=["last_message_at", "window_expires_at"])

    def register_outbound(self, at):
        """Record a business-initiated message.

        Per Meta's rules an agent-sent message does NOT extend the 24h
        customer-service window — only an inbound message does — so this only
        advances ``last_message_at`` for activity tracking.
        """
        if self.last_message_at is None or at > self.last_message_at:
            self.last_message_at = at
            self.save(update_fields=["last_message_at"])

    def close(self):
        self.is_open = False
        self.closed_at = timezone.now()
        self.save(update_fields=["is_open", "closed_at"])

    @classmethod
    def get_or_open(cls, contact: "WhatsAppContact") -> "Conversation":
        with transaction.atomic():
            convo = (
                cls.objects.select_for_update()
                .filter(contact=contact, is_open=True)
                .order_by("-created_at")
                .first()
            )
            if convo:
                return convo
            try:
                with transaction.atomic():
                    return cls.objects.create(
                        account=contact.account,
                        contact=contact,
                        crm_binding=contact.primary_binding,
                    )
            except IntegrityError:
                return cls.objects.get(contact=contact, is_open=True)
