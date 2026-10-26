from django.db import models
from django.utils import timezone


class FacebookContact(models.Model):
    """
    Page-scoped identity for a person who messages or comments on the business's
    Facebook Page.

    The PSID (Page-Scoped ID) is unique per Page. This model is the channel
    identity; ``contact`` links it to the canonical apps.contacts.Contact that the
    rest of Akilent operates on.
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="facebook_contacts",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="facebook_contacts",
    )

    # Page-Scoped ID (Messenger), or the commenter's id on a Page comment.
    psid = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    profile_pic_url = models.TextField(blank=True, default="")

    opted_out = models.BooleanField(default=False)

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "psid"],
                name="unique_facebook_contact_per_account",
            ),
        ]

    def mark_seen(self):
        self.last_seen = timezone.now()
        self.save(update_fields=["last_seen"])

    def __str__(self):
        return f"{self.name or self.psid} ({self.account.company_name})"
