from django.db import models

from apps.accounts.fields import EncryptedTextField


class InstagramBusinessAccount(models.Model):
    """
    Links an Instagram Business Account (and its connected Facebook Page) to an
    Akilent Account, and stores the credentials needed to call the Graph API.
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="instagram_accounts",
    )

    # Instagram Business Account ID from Meta
    instagram_business_account_id = models.CharField(
        max_length=50, unique=True, db_index=True
    )
    # Connected Facebook Page ID (needed for comment webhooks and private replies)
    page_id = models.CharField(max_length=50, blank=True, default="")
    # Human-readable name synced from the page
    name = models.CharField(max_length=255, blank=True, default="")
    # Instagram username (e.g. "@yourbrand")
    username = models.CharField(max_length=100, blank=True, default="")

    # Page access token — encrypted at rest; used for Graph API calls
    access_token = EncryptedTextField(blank=True, null=True)
    # When the token expires (for long-lived tokens this is typically 60 days).
    # None means permanent/never-expires (system user tokens).
    token_expires_at = models.DateTimeField(blank=True, null=True)
    # Set when a send fails due to an invalid/expired token; cleared when new
    # credentials are saved.
    token_expired = models.BooleanField(default=False)

    # Random token used in the Meta webhook verification handshake for this account
    verify_token = models.CharField(max_length=64)
    webhook_subscribed_at = models.DateTimeField(blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("account", "instagram_business_account_id")

    @property
    def is_ready(self) -> bool:
        return bool(
            self.access_token
            and not self.token_expired
            and self.instagram_business_account_id
            and self.is_active
        )

    def __str__(self):
        label = self.username or self.instagram_business_account_id
        return f"{self.account.company_name}: {label}"


class TenantResolutionError(Exception):
    """Raised when a webhook event cannot be mapped to a tenant."""
    pass


def get_account_for_webhook(page_id: str):
    """Resolve a Facebook Page ID (from the webhook) to its Akilent Account."""
    try:
        iba = (
            InstagramBusinessAccount.objects.select_related("account")
            .get(page_id=page_id, is_active=True)
        )
        return iba.account
    except InstagramBusinessAccount.DoesNotExist as exc:
        raise TenantResolutionError(
            f"No active InstagramBusinessAccount found for page_id={page_id}."
        ) from exc


def get_instagram_account_for_webhook(page_id: str) -> "InstagramBusinessAccount":
    """Resolve a Page ID to the InstagramBusinessAccount (carries the token)."""
    try:
        return (
            InstagramBusinessAccount.objects.select_related("account")
            .get(page_id=page_id, is_active=True)
        )
    except InstagramBusinessAccount.DoesNotExist as exc:
        raise TenantResolutionError(
            f"No active InstagramBusinessAccount found for page_id={page_id}."
        ) from exc
