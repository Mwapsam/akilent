from datetime import timedelta

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .contact import FacebookContact


class FacebookConversation(models.Model):
    """
    A Messenger thread between the business's Facebook Page and one FacebookContact.

    The channel-specific record; apps.conversations.Conversation wraps it through
    ChannelConversation (channel "facebook").
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="facebook_conversations",
    )
    page_id = models.CharField(max_length=64)
    facebook_contact = models.ForeignKey(
        FacebookContact,
        on_delete=models.CASCADE,
        related_name="conversations",
    )

    # Messenger's standard messaging window: free-text replies are allowed for 24
    # hours after the customer's last message.
    WINDOW = timedelta(hours=24)
    # A reply a person types in the inbox may go out with the HUMAN_AGENT tag for
    # 7 days after the customer's last message. Automation never uses it.
    HUMAN_AGENT_WINDOW = timedelta(days=7)

    is_open = models.BooleanField(default=True)
    last_message_at = models.DateTimeField(blank=True, null=True)
    # The customer's last message — what both reply windows are measured from.
    last_inbound_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "page_id", "facebook_contact"],
                condition=models.Q(is_open=True),
                name="one_open_facebook_conversation_per_contact",
            ),
        ]
        indexes = [
            models.Index(fields=["facebook_contact", "is_open"]),
        ]

    @classmethod
    def get_or_open(
        cls, facebook_contact: FacebookContact, page_id: str
    ) -> "FacebookConversation":
        """The contact's open Messenger thread with this Page, opening one if needed."""
        lookup = {
            "account_id": facebook_contact.account_id,
            "page_id": page_id,
            "facebook_contact": facebook_contact,
            "is_open": True,
        }
        with transaction.atomic():
            convo = (
                cls.objects.select_for_update()
                .filter(**lookup)
                .order_by("-created_at")
                .first()
            )
            if convo:
                return convo
            try:
                with transaction.atomic():
                    return cls.objects.create(
                        account_id=facebook_contact.account_id,
                        page_id=page_id,
                        facebook_contact=facebook_contact,
                    )
            except IntegrityError:
                return cls.objects.get(**lookup)

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

    @property
    def human_agent_window_is_open(self) -> bool:
        """Whether a reply a person types may still go, with the HUMAN_AGENT tag."""
        return bool(
            self.last_inbound_at
            and timezone.now() < self.last_inbound_at + self.HUMAN_AGENT_WINDOW
        )

    @property
    def human_agent_window_closes_at(self):
        if self.last_inbound_at is None:
            return None
        return self.last_inbound_at + self.HUMAN_AGENT_WINDOW

    def close(self):
        self.is_open = False
        self.save(update_fields=["is_open"])

    def __str__(self):
        return (
            f"Messenger: {self.facebook_contact} "
            f"({'open' if self.is_open else 'closed'})"
        )
