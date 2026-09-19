from django.db import models

from apps.accounts.fields import EncryptedTextField


class WhatsAppBusinessNumber(models.Model):
    """
    Maps a WhatsApp number (phone_number_id from Meta) to an Account, and stores
    the credentials needed to call the Cloud API for that number.

    A tenant may have multiple WhatsApp numbers for different regions,
    departments, etc. Manual registration is supported today (owner enters the
    phone_number_id + access token); the same fields are what Meta Embedded
    Signup will populate later.
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="whatsapp_numbers",
    )

    phone_number_id = models.CharField(max_length=50, unique=True, db_index=True)
    waba_id = models.CharField(max_length=50, blank=True, null=True)
    business_id = models.CharField(max_length=50, blank=True, null=True)
    display_number = models.CharField(max_length=20, blank=True, null=True)

    # Meta system-user / WABA access token used to call the Graph API for this
    # number. Stored encrypted at rest.
    access_token = EncryptedTextField(blank=True, null=True)

    # Two-step verification PIN set when the number was registered on the Cloud
    # API (via Embedded Signup). Kept so the number can be re-registered.
    verification_pin = EncryptedTextField(blank=True, null=True)

    class RegistrationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        REGISTERING = "registering", "Registering"
        REGISTERED = "registered", "Registered"
        FAILED = "failed", "Failed"

    class SetupStatus(models.TextChoices):
        """Derived (never persisted) so it can't go stale."""

        ACTION_REQUIRED = "action_required", "Action required"
        REGISTERING = "registering", "Registering"
        FAILED = "failed", "Registration failed"
        READY_FOR_TEST = "ready_for_test", "Ready for test"
        TEST_SENT = "test_sent", "Test message sent"
        READY = "ready", "Ready"
        DEGRADED = "degraded", "Degraded"

    # State of the Cloud API registration call — the one external operation we
    # persist. Everything else about setup progress is derived from this plus
    # credentials (see ``setup_status``).
    registration_status = models.CharField(
        max_length=20,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.PENDING,
    )
    registration_error = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)

    # Max outbound messages/second for this number. Meta's throughput tiers are
    # 80/s (default) rising to 1000/s; start conservative and raise per number.
    send_rate_limit = models.PositiveSmallIntegerField(default=20)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("account", "phone_number_id")
        indexes = [
            models.Index(fields=["phone_number_id"]),
            models.Index(fields=["is_active", "phone_number_id"]),
        ]

    @property
    def is_ready(self) -> bool:
        """Registered on the Cloud API with the credentials needed to send.

        The single definition of "usable" — callers (onboarding, UI) must use
        this rather than checking the token, PIN or WABA individually.
        """
        return bool(
            self.registration_status == self.RegistrationStatus.REGISTERED
            and self.access_token
            and self.waba_id
            and self.phone_number_id
        )

    @property
    def setup_status(self) -> str:
        S = self.SetupStatus
        R = self.RegistrationStatus
        if not (self.access_token and self.waba_id):
            return S.ACTION_REQUIRED
        if self.registration_status == R.REGISTERING:
            return S.REGISTERING
        if self.registration_status == R.FAILED:
            return S.FAILED
        if self.registration_status == R.REGISTERED:
            if self.last_successful_test() is None:
                return S.READY_FOR_TEST
            if not self.has_received_test_reply():
                return S.TEST_SENT
            from apps.whatsapp.health import is_degraded

            return S.DEGRADED if is_degraded(self.account) else S.READY
        return S.ACTION_REQUIRED

    def last_successful_test(self):
        """Latest verify-connection send that Meta accepted, or None."""
        return self.connection_tests.filter(status="sent").first()

    def has_received_test_reply(self) -> bool:
        """The tester replied after the test message, proving the webhook works.

        Deliberately scoped to the test recipient: an unrelated customer
        message must not complete setup.
        """
        test = self.last_successful_test()
        if test is None:
            return False
        from apps.whatsapp.models.message import MessageLog

        return MessageLog.objects.filter(
            account=self.account,
            direction=MessageLog.Direction.INBOUND,
            contact__phone_number=test.recipient,
            timestamp__gte=test.created_at,
        ).exists()

    def __str__(self):
        disp = f" ({self.display_number})" if self.display_number else ""
        return f"{self.account.company_name}: {self.phone_number_id}{disp}"


class TenantResolutionError(Exception):
    """Raised when a webhook event cannot be mapped to a tenant."""
    pass


def get_account_for_webhook(phone_number_id: str):
    """
    Resolve a WhatsApp phone_number_id (from the webhook) to its Account.

    Args:
        phone_number_id: Meta's phone number ID (from
            webhook.changes[0].value.metadata.phone_number_id)

    Returns:
        apps.accounts.models.Account instance.

    Raises:
        TenantResolutionError: if the number is not registered or is inactive.
    """
    try:
        whatsapp_num = WhatsAppBusinessNumber.objects.select_related("account").get(
            phone_number_id=phone_number_id, is_active=True
        )
        return whatsapp_num.account
    except WhatsAppBusinessNumber.DoesNotExist:
        raise TenantResolutionError(
            f"No active WhatsAppBusinessNumber found for phone_number_id={phone_number_id}. "
            "Check that the number has been registered in the dashboard."
        )


def get_number_for_webhook(phone_number_id: str) -> "WhatsAppBusinessNumber":
    """Resolve a phone_number_id to its WhatsAppBusinessNumber (carries the token)."""
    try:
        return WhatsAppBusinessNumber.objects.select_related("account").get(
            phone_number_id=phone_number_id, is_active=True
        )
    except WhatsAppBusinessNumber.DoesNotExist:
        raise TenantResolutionError(
            f"No active WhatsAppBusinessNumber found for phone_number_id={phone_number_id}."
        )
