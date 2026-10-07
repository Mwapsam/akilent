from datetime import timedelta

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .contact import InstagramContact


class InstagramConversation(models.Model):
    """
    A DM thread between the business's Instagram account and one InstagramContact.

    This is the channel-specific record; apps.conversations.Conversation wraps
    it via a OneToOne FK (instagram_conversation) once the spine is bridged.
    """

    instagram_account = models.ForeignKey(
        "instagram.InstagramBusinessAccount",
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    instagram_contact = models.ForeignKey(
        InstagramContact,
        on_delete=models.CASCADE,
        related_name="conversations",
    )

    # Instagram's standard messaging window: free-text replies are allowed for
    # 24 hours after the customer's last message.
    WINDOW = timedelta(hours=24)

    is_open = models.BooleanField(default=True)
    last_message_at = models.DateTimeField(blank=True, null=True)
    # The customer's last message — what the 24h reply window is measured from.
    last_inbound_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["instagram_account", "instagram_contact"],
                condition=models.Q(is_open=True),
                name="one_open_instagram_conversation_per_contact",
            ),
        ]
        indexes = [
            models.Index(fields=["instagram_contact", "is_open"]),
        ]

    @classmethod
    def get_or_open(
        cls, instagram_contact: InstagramContact, instagram_account=None
    ) -> "InstagramConversation":
        """The contact's open DM thread with ``instagram_account``, opening one if needed.

        ``instagram_account`` should always be passed: a business with more than one
        connected Instagram account would otherwise get a thread on whichever account
        happens to be first.
        """
        if instagram_account is None:
            instagram_account = instagram_contact.account.instagram_accounts.filter(
                is_active=True
            ).first()
        with transaction.atomic():
            convo = (
                cls.objects.select_for_update()
                .filter(
                    instagram_account=instagram_account,
                    instagram_contact=instagram_contact,
                    is_open=True,
                )
                .order_by("-created_at")
                .first()
            )
            if convo:
                return convo
            try:
                with transaction.atomic():
                    return cls.objects.create(
                        instagram_account=instagram_account,
                        instagram_contact=instagram_contact,
                    )
            except IntegrityError:
                return cls.objects.get(
                    instagram_account=instagram_account,
                    instagram_contact=instagram_contact,
                    is_open=True,
                )

    def register_inbound(self, at=None):
        at = at or timezone.now()
        self.last_message_at = max(filter(None, [self.last_message_at, at]))
        self.last_inbound_at = max(filter(None, [self.last_inbound_at, at]))
        self.save(update_fields=["last_message_at", "last_inbound_at"])

    def register_outbound(self, at=None):
        """A business message: moves the thread forward but never reopens the reply window."""
        at = at or timezone.now()
        self.last_message_at = max(filter(None, [self.last_message_at, at]))
        self.save(update_fields=["last_message_at"])

    @property
    def window_is_open(self) -> bool:
        return bool(
            self.last_inbound_at and timezone.now() < self.last_inbound_at + self.WINDOW
        )

    def close(self):
        self.is_open = False
        self.save(update_fields=["is_open"])

    def __str__(self):
        return (
            f"Instagram DM: {self.instagram_contact} "
            f"({'open' if self.is_open else 'closed'})"
        )
