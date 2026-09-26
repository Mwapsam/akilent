from django.db import models

from apps.accounts.fields import EncryptedTextField

class Configurations(models.Model):
    name = models.CharField(max_length=100, unique=True)
    value = EncryptedTextField()
    is_secret = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Configuration"
        verbose_name_plural = "Configurations"

    def __str__(self):
        return self.name

    @property
    def masked_value(self):
        if self.is_secret:
            return "••••••••"
        val = str(self.value)
        return val[:20] + "..." if len(val) > 20 else val


class SiteSettings(models.Model):
    # Branding
    app_name = models.CharField(max_length=100, default="Automator")
    logo = models.ImageField(upload_to="branding/", blank=True, null=True)
    support_email = models.EmailField(blank=True, default="")

    whatsapp_enabled = models.BooleanField(default=True)
    signups_enabled = models.BooleanField(default=True)
    payments_enabled = models.BooleanField(default=True)

    # Phase 1: temporary gate for automation event publishing until Phase 4's ModuleSubscription exists
    automation_events_enabled = models.BooleanField(
        default=False,
        help_text="Enable domain event publishing for automation rules and AI (Phase 1 beta)",
    )

    # New-signup defaults
    default_plan = models.ForeignKey(
        "billing.Plan", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    default_trial_days = models.PositiveIntegerField(default=14)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return self.app_name

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class MailProviderSettings(models.Model):
    """Platform-wide mail infrastructure configuration, edited by superadmins.

    A singleton (always pk=1) loaded via MailProviderSettings.load().
    Allows runtime switching between different mail server backends (Stalwart, SES, etc.)
    and message delivery providers (SMTP, SES API, etc.) without redeploying.
    """

    BACKEND_CHOICES = [
        ("stalwart", "Stalwart Mail Server"),
        ("ses", "AWS SES"),
        ("null", "Null (dev/test)"),
    ]

    SEND_BACKEND_CHOICES = [
        ("smtp", "SMTP Relay"),
        ("ses", "AWS SES API"),
        ("null", "Null (dev/test)"),
    ]

    infra_backend = models.CharField(
        max_length=32,
        choices=BACKEND_CHOICES,
        default="stalwart",
        help_text="Mail server for domain/DKIM infrastructure"
    )

    # Message delivery provider
    send_backend = models.CharField(
        max_length=32,
        choices=SEND_BACKEND_CHOICES,
        default="smtp",
        help_text="Provider for outbound message delivery"
    )

    # SES-specific configuration (read from env vars at runtime, just store names here)
    aws_region = models.CharField(
        max_length=32,
        default="us-east-1",
        help_text="AWS region for SES (e.g., us-east-1, eu-west-1). Credentials from AWS_ACCESS_KEY_ID env vars."
    )
    ses_configuration_set = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="SES configuration set name for tracking bounces/complaints (optional but recommended)"
    )
    ses_sns_topic_arn = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="SNS topic ARN for receiving bounce/complaint notifications (required if configuration_set is used)"
    )

    # SES operational hardening (Phase 3)
    ses_send_rate_limit = models.PositiveIntegerField(
        default=14,
        help_text="Max sends per second for SES (default 14, AWS SES sandbox default; can go higher with send limit increase)"
    )

    # SMTP relay hardening (Phase 3)
    smtp_require_tls = models.BooleanField(
        default=True,
        help_text="Enforce TLS for SMTP relay connections (security best-practice; set False only for dev/testing)"
    )

    # Phase 4: recipient validation & suppression hardening
    enable_recipient_validation = models.BooleanField(
        default=True,
        help_text="Enable pre-send email validation (syntax + MX record checks) to suppress invalid addresses"
    )
    mx_validation_cache_ttl_seconds = models.PositiveIntegerField(
        default=86400,
        help_text="Cache time-to-live for MX record lookups in seconds (default 24 hours)"
    )
    soft_bounce_threshold = models.PositiveIntegerField(
        default=3,
        help_text="Number of transient bounces before escalating to hard suppression (default 3)"
    )
    require_explicit_consent = models.BooleanField(
        default=False,
        help_text=(
            "Refuse campaign recipients whose Contact has no recorded opt-in. "
            "Opted-out contacts are always refused regardless of this setting. "
            "Leave off until imported lists carry a consent attestation, "
            "otherwise every pre-consent contact becomes unmailable."
        ),
    )

    # Reputation circuit breaker — per-account bounce/complaint rate limits.
    # SES enforcement kicks in around bounce >5% (review) / >10% (pause) and
    # complaint >0.1% (review) / >0.5% (pause); defaults sit just inside those.
    reputation_bounce_warn = models.FloatField(
        default=0.05,
        help_text="Bounce rate at which an account is flagged (warned) — 0.05 = 5%"
    )
    reputation_bounce_halt = models.FloatField(
        default=0.10,
        help_text="Bounce rate at which an account's non-system sends are halted — 0.10 = 10%"
    )
    reputation_complaint_halt = models.FloatField(
        default=0.005,
        help_text="Complaint rate at which an account's non-system sends are halted — 0.005 = 0.5%"
    )
    reputation_min_volume = models.PositiveIntegerField(
        default=100,
        help_text="Minimum sends in the window before the breaker can act (avoids tiny-sample noise)"
    )
    reputation_window_hours = models.PositiveIntegerField(
        default=24,
        help_text="Trailing window (hours) over which bounce/complaint rates are measured"
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mail provider settings"
        verbose_name_plural = "Mail provider settings"

    def __str__(self):
        return f"Mail: {self.get_infra_backend_display()} (send: {self.get_send_backend_display()})"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """Get or create the singleton settings row."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AdminAction(models.Model):
    """Something a platform operator did in the Operator Console, or a "View as" session.

    Written by ``apps.core.audit.audit`` for every console change, never edited afterwards. The
    business's own team never sees these; they're for accountability between operators.
    """

    actor = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, related_name="admin_actions")
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="admin_actions")
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=200, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["account", "-at"])]

    def __str__(self):
        return f"{self.actor} {self.action} {self.target}".strip()
