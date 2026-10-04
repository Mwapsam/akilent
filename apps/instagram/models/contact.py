from django.db import models
from django.utils import timezone


class InstagramContact(models.Model):
    """
    Instagram-scoped identity for a person who interacts with the business via
    Instagram DMs or comments.

    The IGSID (Instagram-Scoped User ID) is unique per Instagram Business Account.
    This model is the channel identity; ``contact`` links it to the canonical
    apps.contacts.Contact that the rest of Akilent operates on.
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="instagram_contacts",
    )
    # The canonical Akilent contact — resolved on first interaction or when the
    # business manually links identities.
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="instagram_contacts",
    )

    # Instagram-Scoped User ID — unique per page, not across pages.
    instagram_scoped_id = models.CharField(max_length=50, db_index=True)
    username = models.CharField(max_length=100, blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")

    opted_out = models.BooleanField(default=False)

    # Cached eligibility state — the provider always performs a live check at
    # send time; this field is for display and pre-flight optimisation only.
    messaging_eligible = models.BooleanField(default=True)

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "instagram_scoped_id"],
                name="unique_instagram_contact_per_account",
            ),
        ]

    def mark_seen(self):
        self.last_seen = timezone.now()
        self.save(update_fields=["last_seen"])

    def __str__(self):
        label = self.username or self.instagram_scoped_id
        return f"{label} ({self.account.company_name})"
