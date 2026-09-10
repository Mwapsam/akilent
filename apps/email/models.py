import hashlib
import re
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

# Shared presentation for the DNS records a tenant must publish. Used by both
# the legacy column-derived path and the EmailDnsRecord-backed (SES) path so the
# domain card and the DNS checker always agree on labels/ordering.
_DNS_LABELS = {
    "verify": "Domain verification",
    "dkim": "DKIM",
    "spf": "SPF",
    "dmarc": "DMARC",
}
_DNS_DESCS = {
    "verify": "Proves you own this domain so we can switch it on.",
    "dkim": "Signs your mail so providers trust it wasn't tampered with.",
    "spf": "Lists the servers allowed to send for your domain.",
    "dmarc": "Tells receivers what to do with mail that fails the checks.",
}
_DNS_KEY_ORDER = {"verify": 0, "dkim": 1, "spf": 2, "dmarc": 3}


class ProvisioningJob(models.Model):
    """Tracks the lifecycle of an async Celery provisioning operation.

    Created before the Celery task is dispatched; updated by the task as it
    runs. Provides operators and tenants with visibility into in-progress or
    failed provisioning work.
    """

    class Status(models.TextChoices):
        PENDING  = "pending",  "Pending"
        RUNNING  = "running",  "Running"
        SUCCESS  = "success",  "Success"
        FAILED   = "failed",   "Failed"
        RETRYING = "retrying", "Retrying"

    class JobType(models.TextChoices):
        PROVISION_DOMAIN  = "provision_domain",  "Provision Domain"
        DEPROVISION_DOMAIN = "deprovision_domain", "Deprovision Domain"
        PROVISION_MAILBOX = "provision_mailbox", "Provision Mailbox"
        DEPROVISION_MAILBOX = "deprovision_mailbox", "Deprovision Mailbox"
        CHANGE_PASSWORD   = "change_password",   "Change Password"
        SET_QUOTA         = "set_quota",         "Set Quota"
        ROTATE_DKIM       = "rotate_dkim",       "Rotate DKIM"
        SUSPEND_MAILBOX   = "suspend_mailbox",   "Suspend Mailbox"
        PROVISION_ALIAS   = "provision_alias",   "Provision Alias"
        DEPROVISION_ALIAS = "deprovision_alias", "Deprovision Alias"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="provisioning_jobs",
    )
    job_type      = models.CharField(max_length=50, choices=JobType.choices)
    resource_type = models.CharField(max_length=50)    # "domain" | "mailbox" | "alias"
    resource_id   = models.CharField(max_length=255)   # domain name / email / alias address
    status        = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    error         = models.TextField(blank=True, default="")
    attempts      = models.PositiveSmallIntegerField(default=0)
    metadata      = models.JSONField(default=dict)
    created_at    = models.DateTimeField(auto_now_add=True)
    started_at    = models.DateTimeField(blank=True, null=True)
    completed_at  = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["created_at"]),
        ]

    def mark_running(self) -> None:
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.attempts += 1
        self.save(update_fields=["status", "started_at", "attempts"])

    def mark_success(self) -> None:
        self.status = self.Status.SUCCESS
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])

    def mark_failed(self, error: str, *, retrying: bool = False) -> None:
        self.status = self.Status.RETRYING if retrying else self.Status.FAILED
        self.error = (error or "")[:5000]
        self.completed_at = timezone.now() if not retrying else None
        update_fields = ["status", "error"]
        if self.completed_at is not None:
            update_fields.append("completed_at")
        self.save(update_fields=update_fields)

    def __str__(self) -> str:
        return f"{self.job_type} {self.resource_id} [{self.status}]"


class AuditLog(models.Model):
    """Immutable record of every mail provider operation.

    Written by apps.email.audit.record() — never written directly.
    Rows are never updated or deleted (use a retention policy at the DB level).
    """

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="email_audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    action        = models.CharField(max_length=100)   # e.g. "domain.create"
    resource_type = models.CharField(max_length=50)    # "domain" | "mailbox" | "alias"
    resource_id   = models.CharField(max_length=255)
    success       = models.BooleanField(default=True)
    error         = models.TextField(blank=True, default="")
    metadata      = models.JSONField(default=dict)
    timestamp     = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["account", "timestamp"]),
            models.Index(fields=["resource_type", "resource_id"]),
        ]

    def __str__(self) -> str:
        status = "OK" if self.success else "FAIL"
        return f"[{status}] {self.action} {self.resource_id} @ {self.timestamp}"


class EmailDomain(models.Model):
    """A per-tenant sending domain provisioned on Mailcow.

    Transactional email is sent *from* a verified domain; we provision the
    domain on Mailcow, expose the generated DKIM record for the tenant to add to
    DNS, and only allow sending once verified.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending DNS / verification"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_domains"
    )

    domain = models.CharField(max_length=255, unique=True)

    dkim_selector = models.CharField(max_length=63, default="dkim")
    dkim_public_key = models.TextField(blank=True, default="")  # DKIM TXT record value

    # Self-hosted ownership-verification TXT record (minted by
    # ``ensure_verification_token``). The customer adds this to DNS; the live
    # DNS check (apps.email.dnscheck) confirms it and verifies the domain.
    verify_record_name = models.CharField(max_length=255, blank=True, default="")
    verify_record_value = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    is_active = models.BooleanField(default=True)  # enabled/disabled on the mail server
    # Per-record DNS readiness, refreshed by the live DNS check. Ownership ("verify")
    # readiness is tracked by ``status == VERIFIED``.
    spf_ok = models.BooleanField(default=False)
    dkim_ok = models.BooleanField(default=False)
    dmarc_ok = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(blank=True, null=True)
    last_checked_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["domain"]

    @property
    def is_verified(self) -> bool:
        return self.status == self.Status.VERIFIED

    def is_ses_backed(self) -> bool:
        """True when the active mail backend is AWS SES (infra or send).

        SES publishes DKIM as three CNAME records, so its DNS spec comes from
        EmailDnsRecord rows rather than the single-TXT columns below.
        """
        from apps.core.models import MailProviderSettings

        try:
            s = MailProviderSettings.load()
            return "ses" in (s.infra_backend, s.send_backend)
        except Exception:
            return False

    def ensure_verification_token(self) -> bool:
        """Generate a self-hosted ownership TXT record if one isn't set yet.

        Returns True if a new record was created. Replaces the old per-account
        Progstack token flow — ownership is now proven by a TXT record we mint
        and check via DNS ourselves, so customers never handle an API token.
        """
        if self.verify_record_value:
            return False
        token = secrets.token_hex(16)
        self.verify_record_name = self.domain  # root TXT, alongside SPF
        self.verify_record_value = f"automator-domain-verification={token}"
        return True

    @property
    def spf_value(self) -> str:
        from apps.core.models import MailProviderSettings

        try:
            mail_settings = MailProviderSettings.load()
            # For SES, use Amazon's SES mail server (region-specific)
            if mail_settings.infra_backend == "ses" or mail_settings.send_backend == "ses":
                region = mail_settings.aws_region or "us-east-1"
                # SES mail server endpoint for SPF (sendmail.region.amazonses.com)
                return f"v=spf1 include:amazonses.com ~all"
            # For Stalwart or other backends, use the configured SMTP host
            host = settings.EMAIL_HOST or "YOUR_MAIL_HOST"
            return f"v=spf1 include:{host} ~all"
        except Exception:
            host = settings.EMAIL_HOST or "YOUR_MAIL_HOST"
            return f"v=spf1 include:{host} ~all"

    @property
    def dmarc_value(self) -> str:
        return f"v=DMARC1; p=none; rua=mailto:dmarc@{self.domain}"

    def dns_records(self):
        """The DNS records the customer must add, with current readiness baked in.

        Single source of truth for both the UI (the domain card iterates this)
        and the DNS checker, so the names/values they compare always agree.

        When EmailDnsRecord rows exist (SES Easy DKIM = 3 CNAMEs), the spec is
        built from them; otherwise it is derived from this row's columns
        (Stalwart-style single-TXT DKIM).
        """
        rows = list(self.dns_record_rows.all()) if self.pk else []
        if rows:
            rows.sort(key=lambda r: (_DNS_KEY_ORDER.get(r.key, 9), r.name))
            return [
                {
                    "key": r.key,
                    "label": _DNS_LABELS.get(r.key, r.key.upper()),
                    "type": r.record_type,
                    "name": r.name,
                    "value": r.value,
                    "desc": _DNS_DESCS.get(r.key, ""),
                    "required": r.key in ("verify", "dkim"),
                    "ok": r.is_ok,
                }
                for r in rows
            ]
        return [
            {
                "key": "verify", "label": "Domain verification", "type": "TXT",
                "name": self.verify_record_name or self.domain,
                "value": self.verify_record_value,
                "desc": "Proves you own this domain so we can switch it on.",
                "required": True, "ok": self.is_verified,
            },
            {
                "key": "dkim", "label": "DKIM", "type": "TXT",
                "name": self.dkim_record_name, "value": self.dkim_txt_value,
                "desc": "Signs your mail so providers trust it wasn't tampered with.",
                "required": True, "ok": self.dkim_ok,
            },
            {
                "key": "spf", "label": "SPF", "type": "TXT",
                "name": self.domain, "value": self.spf_value,
                "desc": "Lists the servers allowed to send for your domain.",
                "required": False, "ok": self.spf_ok,
            },
            {
                "key": "dmarc", "label": "DMARC", "type": "TXT",
                "name": self.dmarc_record_name, "value": self.dmarc_value,
                "desc": "Tells receivers what to do with mail that fails the checks.",
                "required": False, "ok": self.dmarc_ok,
            },
        ]

    @property
    def dns_found_count(self) -> int:
        return sum(1 for r in self.dns_records() if r["ok"])

    @property
    def dns_total_count(self) -> int:
        return len(self.dns_records())

    @property
    def dkim_txt_value(self) -> str:
        """The DKIM key as a single DNS-ready TXT value.

        ``amavisd showkeys`` emits BIND format — the record name/TTL/TXT prefix
        plus the key split across quoted chunks inside parentheses. DNS wants
        just the concatenated value (``v=DKIM1; p=...``), so pull out the quoted
        segments and join them; fall back to collapsing whitespace.
        """
        raw = self.dkim_public_key or ""
        chunks = re.findall(r'"([^"]*)"', raw)
        if chunks:
            return "".join(chunks)
        return " ".join(raw.split())

    @property
    def dkim_record_name(self) -> str:
        return f"{self.dkim_selector}._domainkey.{self.domain}"

    @property
    def dmarc_record_name(self) -> str:
        return f"_dmarc.{self.domain}"

    def __str__(self):
        return f"{self.domain} ({self.status})"


class EmailDnsRecord(models.Model):
    """One DNS record a tenant must publish for a sending domain.

    Used when the mail backend needs a record shape the legacy single-TXT-DKIM
    columns on EmailDomain can't express — notably AWS SES Easy DKIM, which is
    three CNAME records. For Stalwart-style domains this table stays empty and
    EmailDomain.dns_records() derives the list from its own columns instead.
    """

    class Key(models.TextChoices):
        VERIFY = "verify", "Domain verification"
        DKIM = "dkim", "DKIM"
        SPF = "spf", "SPF"
        DMARC = "dmarc", "DMARC"

    class RecordType(models.TextChoices):
        TXT = "TXT", "TXT"
        CNAME = "CNAME", "CNAME"

    domain = models.ForeignKey(
        EmailDomain, on_delete=models.CASCADE, related_name="dns_record_rows"
    )
    key = models.CharField(max_length=10, choices=Key.choices)
    record_type = models.CharField(
        max_length=10, choices=RecordType.choices, default=RecordType.TXT
    )
    name = models.CharField(max_length=255)
    value = models.TextField()
    is_ok = models.BooleanField(default=False)
    checked_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["domain_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["domain", "key", "name"],
                name="uniq_dns_record_per_domain_key_name",
            )
        ]

    def __str__(self):
        state = "ok" if self.is_ok else "pending"
        return f"{self.record_type} {self.name} [{state}]"


class EmailApiKey(models.Model):
    """A per-account key authenticating calls to the transactional send API.

    New keys are ``ak_live_...``, hashed (sha256) before storage — the
    plaintext is only ever available once, at creation, via
    ``create_for_account``. ``key`` is a legacy column: pre-migration keys
    (``ek_...``) were stored in plaintext there and keep authenticating via
    ``authenticate()``'s fallback lookup until customers rotate onto a
    hashed key, at which point ``key`` can be dropped in a follow-up migration.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_api_keys"
    )
    name = models.CharField(max_length=100, default="default")

    # Legacy plaintext column — new keys leave this blank.
    key = models.CharField(max_length=64, unique=True, db_index=True, blank=True, null=True)

    key_hash = models.CharField(max_length=64, unique=True, db_index=True, blank=True, null=True)
    key_prefix = models.CharField(max_length=12, default="ak_live_")
    last4 = models.CharField(max_length=4, blank=True, default="")

    class Mode(models.TextChoices):
        LIVE = "live", "Live"
        TEST = "test", "Test"

    # Test keys route through the sandbox provider: nothing is delivered, no
    # SES quota / reputation impact, deterministic outcomes by magic recipient.
    mode = models.CharField(max_length=4, choices=Mode.choices, default=Mode.LIVE)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(blank=True, null=True)
    rate_per_min = models.PositiveIntegerField(blank=True, null=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(blank=True, null=True)

    @staticmethod
    def generate_key(mode: str = "live") -> str:
        prefix = "ak_test_" if mode == "test" else "ak_live_"
        return prefix + secrets.token_urlsafe(36)

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @classmethod
    def create_for_account(
        cls,
        account,
        *,
        name: str = "default",
        scopes: list[str] | None = None,
        mode: str = "live",
    ):
        """Create a new key. Returns (instance, plaintext) — plaintext is shown once."""
        raw = cls.generate_key(mode)
        obj = cls.objects.create(
            account=account,
            name=name,
            mode=mode,
            key_hash=cls.hash_key(raw),
            key_prefix=raw.split("_")[0] + "_" + raw.split("_")[1] + "_",
            last4=raw[-4:],
            scopes=scopes or ["messages:send"],
        )
        return obj, raw

    @classmethod
    def authenticate(cls, raw_key: str):
        """Look up an active key by hash, falling back to the legacy plaintext column."""
        if not raw_key:
            return None
        by_hash = cls.objects.filter(
            key_hash=cls.hash_key(raw_key), is_active=True
        ).select_related("account").first()
        if by_hash is not None:
            return by_hash
        return cls.objects.filter(key=raw_key, is_active=True).select_related("account").first()

    def touch(self):
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at"])

    def __str__(self):
        return f"{self.account} :: {self.name}"


class SmtpCredential(models.Model):
    """A per-domain SMTP AUTH identity for direct SMTP relay.

    No longer provisioned on the mail server. Instead, credentials are validated
    locally and messages are delivered via SES (or other send provider).
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="smtp_credentials"
    )
    domain = models.ForeignKey(
        EmailDomain, on_delete=models.CASCADE, related_name="smtp_credentials"
    )
    username = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    secret_hash = models.CharField(max_length=64, blank=True, default="")
    last4 = models.CharField(max_length=4, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.username} ({'active' if self.is_active else 'revoked'})"


class EmailTemplate(models.Model):
    """A reusable, tenant-owned email template with variable interpolation.

    Subject/body fields use Django template syntax rendered through a
    sandboxed engine (apps.email.services.render.render_template) — only the
    variables dict passed at render time is visible, never settings/request
    data, so tenant-authored templates can't reach app internals.
    """

    class BuilderMode(models.TextChoices):
        RAW = "raw", "Raw HTML"
        BLOCKS = "blocks", "Drag-and-drop"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_templates"
    )
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=150)

    subject = models.CharField(max_length=998, blank=True, default="")
    text_body = models.TextField(blank=True, default="")
    html_body = models.TextField(blank=True, default="")

    # GrapesJS project data (block tree) for templates edited via the
    # drag-and-drop builder. html_body/text_body remain the compiled output
    # that render.py/send.py actually consume — this field is the editable
    # source, not read by the render/send pipeline.
    content_blocks = models.JSONField(default=dict, blank=True)
    builder_mode = models.CharField(
        max_length=10, choices=BuilderMode.choices, default=BuilderMode.RAW
    )

    # Example variables used by the dashboard preview/test-render.
    sample_variables = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("account", "slug")

    def __str__(self):
        return f"{self.name} ({self.account})"


class EmailTemplateLocale(models.Model):
    """A per-locale content variant of an EmailTemplate.

    Each field falls back to the base template when left blank, so a locale
    row can override just the subject, or the whole message. Rendering picks a
    locale from an explicit ``locale`` arg or ``contact.locale``; an unknown
    locale falls back to the base template.
    """

    template = models.ForeignKey(
        EmailTemplate, on_delete=models.CASCADE, related_name="locales"
    )
    locale = models.CharField(max_length=15)  # e.g. "en", "fr", "pt-BR"
    subject = models.CharField(max_length=998, blank=True, default="")
    text_body = models.TextField(blank=True, default="")
    html_body = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["locale"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "locale"], name="uniq_template_locale"
            )
        ]

    def __str__(self):
        return f"{self.template.slug} [{self.locale}]"


class EmailTemplateDataSource(models.Model):
    """A named HTTPS endpoint fetched at render time into a template's context.

    The fetched JSON object is injected under ``key`` (so ``{{ key.field }}``).
    Fetches are HTTPS-only, SSRF-guarded, size/time-capped, and cached for
    ``ttl_seconds`` — see apps.email.services.datafetch.
    """

    template = models.ForeignKey(
        EmailTemplate, on_delete=models.CASCADE, related_name="data_sources"
    )
    key = models.SlugField(max_length=60)
    url = models.URLField(max_length=1000)
    headers = models.JSONField(default=dict, blank=True)
    ttl_seconds = models.PositiveIntegerField(default=300)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "key"], name="uniq_template_datasource_key"
            )
        ]

    def __str__(self):
        return f"{self.template.slug}:{self.key}"


class SystemEmailTemplate(models.Model):
    """A platform-owned (non-tenant) transactional/system email template.

    Unlike EmailTemplate (tenant-owned, FK'd to Account), these are internal
    emails the app sends about itself (invites, verification, admin alerts)
    and have no tenant owner — identified by a unique `key` instead of
    (account, slug). Rendered through the same sandboxed engine as
    EmailTemplate (apps.email.services.render), which is duck-typed on
    .subject/.text_body/.html_body rather than tied to EmailTemplate.

    If no active row exists for a given key, callers fall back to the
    original file-based template — see apps.email.services.system_templates.
    """

    key = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=150)

    subject = models.CharField(max_length=998, blank=True, default="")
    text_body = models.TextField(blank=True, default="")
    html_body = models.TextField(blank=True, default="")

    sample_variables = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return f"{self.name} ({self.key})"


class EmailTemplateVersion(models.Model):
    """A numbered snapshot of an EmailTemplate's content.

    Written on every edit (snapshot-before-overwrite) and on explicit publishes.
    ``is_active`` marks which snapshot the live template currently reflects;
    activating an older one is a rollback.
    """

    template = models.ForeignKey(
        EmailTemplate, on_delete=models.CASCADE, related_name="versions"
    )
    number = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=False)

    subject = models.CharField(max_length=998, blank=True, default="")
    text_body = models.TextField(blank=True, default="")
    html_body = models.TextField(blank=True, default="")
    content_blocks = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-number", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "number"], name="uniq_template_version_number"
            )
        ]

    def __str__(self):
        return f"{self.template.name} @ {self.created_at:%Y-%m-%d %H:%M}"


class EmailTemplateAsset(models.Model):
    """An image uploaded for use inside the drag-and-drop template builder."""

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_template_assets"
    )
    file = models.ImageField(upload_to="email_assets/%Y/%m/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.file.name


class BulkEmailCampaign(models.Model):
    """One client-submitted bulk send, fanned out to N EmailMessage rows.

    BulkEmailRecipient is to this model what WebhookDelivery is to
    WebhookEndpoint: one row per fan-out target, created up front, updated
    as each recipient's send is dispatched/completes.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="bulk_campaigns"
    )
    domain = models.ForeignKey(
        EmailDomain, on_delete=models.SET_NULL, blank=True, null=True
    )
    template = models.ForeignKey(
        EmailTemplate, on_delete=models.SET_NULL, blank=True, null=True
    )

    from_email = models.EmailField()
    # Used only when no template is set (ad hoc bulk send).
    subject_override = models.CharField(max_length=998, blank=True, default="")
    text_override = models.TextField(blank=True, default="")
    html_override = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    recipient_count = models.PositiveIntegerField(default=0)
    queued_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["account", "status"])]

    def mark_sending(self) -> None:
        if self.status == self.Status.DRAFT or self.status == self.Status.QUEUED:
            self.status = self.Status.SENDING
            if self.started_at is None:
                self.started_at = timezone.now()
            self.save(update_fields=["status", "started_at"])
            self._notify_campaign("campaign.started")

    def mark_completed(self) -> None:
        already = self.status == self.Status.COMPLETED
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])
        if not already:
            self._notify_campaign("campaign.completed")

    def _notify_campaign(self, event_type: str) -> None:
        try:
            from apps.email.webhooks import notify

            notify(self.account, event_type, {
                "id": self.pk,
                "status": self.status,
                "recipient_count": self.recipient_count,
                "sent_count": self.sent_count,
                "failed_count": self.failed_count,
            })
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "campaign %s webhook notify failed", self.pk
            )

    def mark_paused(self, reason: str = "") -> None:
        self.status = self.Status.PAUSED
        if reason:
            self.error = reason[:5000]
        self.save(update_fields=["status", "error"])

    def increment_counts(self, *, queued=0, sent=0, failed=0) -> None:
        updates = []
        if queued:
            self.queued_count = models.F("queued_count") + queued
            updates.append("queued_count")
        if sent:
            self.sent_count = models.F("sent_count") + sent
            updates.append("sent_count")
        if failed:
            self.failed_count = models.F("failed_count") + failed
            updates.append("failed_count")
        if updates:
            self.save(update_fields=updates)
            self.refresh_from_db(fields=updates)

    def __str__(self):
        return f"Campaign {self.pk} ({self.account}) [{self.status}]"


class BulkEmailRecipient(models.Model):
    """One recipient of a BulkEmailCampaign, with per-recipient template variables."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    campaign = models.ForeignKey(
        BulkEmailCampaign, on_delete=models.CASCADE, related_name="recipients"
    )
    to_email = models.EmailField()
    variables = models.JSONField(default=dict, blank=True)
    message = models.ForeignKey(
        "EmailMessage", on_delete=models.SET_NULL, blank=True, null=True
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["pk"]
        indexes = [models.Index(fields=["campaign", "status"])]

    def __str__(self):
        return f"{self.to_email} [{self.status}] (campaign {self.campaign_id})"


class BulkEmailCampaignVersion(models.Model):
    """Immutable content snapshot of a BulkEmailCampaign.

    One is taken automatically when a campaign is submitted, so there is an
    audit record of exactly what content and recipient count went out. Drafts
    can be rolled back to a snapshot (see apps.email.services.campaign_versions).
    """

    campaign = models.ForeignKey(
        BulkEmailCampaign, on_delete=models.CASCADE, related_name="versions"
    )
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=120, blank=True, default="")

    from_email = models.EmailField()
    subject_override = models.CharField(max_length=998, blank=True, default="")
    text_override = models.TextField(blank=True, default="")
    html_override = models.TextField(blank=True, default="")
    template = models.ForeignKey(
        EmailTemplate, on_delete=models.SET_NULL, blank=True, null=True
    )
    template_version_number = models.PositiveIntegerField(blank=True, null=True)
    recipient_count = models.PositiveIntegerField(default=0)
    status_at_snapshot = models.CharField(max_length=20, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "number"], name="uniq_campaign_version_number"
            )
        ]

    def __str__(self):
        return f"campaign {self.campaign_id} v{self.number}"


def _message_public_id() -> str:
    return "msg_" + secrets.token_hex(16)


class EmailMessage(models.Model):
    """Log of a transactional email send."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Complained"
        OPENED = "opened", "Opened"
        CLICKED = "clicked", "Clicked"

    public_id = models.CharField(
        max_length=40, unique=True, default=_message_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_messages"
    )
    domain = models.ForeignKey(
        EmailDomain, on_delete=models.SET_NULL, blank=True, null=True
    )
    template = models.ForeignKey(
        EmailTemplate, on_delete=models.SET_NULL, blank=True, null=True
    )
    campaign = models.ForeignKey(
        BulkEmailCampaign,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="messages",
    )

    from_email = models.EmailField()
    to_email = models.EmailField()
    subject = models.CharField(max_length=998, blank=True, default="")

    # "live" or "test" — set from the API key that created the message. Test
    # messages go through the sandbox provider and are excluded from live stats.
    key_mode = models.CharField(max_length=4, default="live")

    # Snapshot of the exact content sent, independent of later template
    # edits — the audit trail for "what did we actually send."
    rendered_subject = models.CharField(max_length=998, blank=True, default="")
    rendered_text = models.TextField(blank=True, default="")
    rendered_html = models.TextField(blank=True, default="")

    # Attachment metadata only: [{"filename", "content_type", "size"}]. The bytes
    # are carried to the send task, not persisted here.
    attachments = models.JSONField(default=list, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    provider_message_id = models.CharField(max_length=255, blank=True, null=True)
    error = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def mark_sent(self, provider_message_id: str = ""):
        self.status = self.Status.SENT
        self.provider_message_id = provider_message_id or None
        self.sent_at = timezone.now()
        self.save(update_fields=["status", "provider_message_id", "sent_at"])
        self._record_event("sent", source="pipeline")

    def mark_failed(self, error: str):
        self.status = self.Status.FAILED
        self.error = (error or "")[:5000]
        self.save(update_fields=["status", "error"])
        self._record_event("failed", source="pipeline", data={"error": self.error})

    def _record_event(self, event_type: str, *, source: str, data: dict | None = None) -> None:
        """Append a MessageEvent (which owns the outbound-webhook fan-out)."""
        from apps.logs.services import record_message_event

        try:
            record_message_event(self, event_type, source=source, data=data or {})
        except Exception:  # noqa: BLE001 - observability must not break sends
            import logging

            logging.getLogger(__name__).exception(
                "EmailMessage._record_event failed (message %s, %s)", self.pk, event_type
            )

    def _enqueue_webhook_event(self, event_type: str) -> None:
        from apps.email.webhooks import enqueue_event

        enqueue_event(
            event_type,
            account=self.account,
            message=self,
            data={
                "id": self.pk,
                "from": self.from_email,
                "to": self.to_email,
                "subject": self.subject,
                "status": self.status,
            },
        )

    def __str__(self):
        return f"{self.to_email} [{self.status}]"


class EmailTrackingToken(models.Model):
    """Maps an opaque URL token to a (message, recipient, original_url) triple.

    Django generates the token when rewriting outgoing email links. The token
    is the only secret in the tracking URL — no internal DB IDs are exposed.
    """

    token = models.CharField(max_length=64, unique=True, db_index=True)
    message = models.ForeignKey(
        EmailMessage, on_delete=models.CASCADE, related_name="tracking_tokens"
    )
    recipient = models.EmailField()
    url = models.TextField(blank=True, default="")  # blank = open-pixel token
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        kind = "click" if self.url else "open"
        return f"{kind} token for {self.message_id}"


class UnsubscribeToken(models.Model):
    """One-time token for secure unsubscribe links.

    Recipient clicks the unsubscribe link with this token, which gets added to
    the suppression list. Tokens are opaque (no internal DB IDs exposed).
    """

    token = models.CharField(max_length=64, unique=True, db_index=True)
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="unsubscribe_tokens"
    )
    email = models.EmailField()
    campaign = models.ForeignKey(
        BulkEmailCampaign,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="unsubscribe_tokens",
    )
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "email"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"Unsubscribe for {self.email}"


class WebhookEndpoint(models.Model):
    """A client-configured URL that receives signed event callbacks.

    Requests are HMAC-signed with signing_secret (see apps.email.webhooks) so
    the receiving server can verify a payload really came from Akilent.
    """

    EVENT_CHOICES = [
        ("message.sent", "Message sent"),
        ("message.failed", "Message failed"),
        ("message.delivered", "Message delivered"),
        ("message.opened", "Message opened"),
        ("message.clicked", "Link clicked"),
        ("message.bounced", "Message bounced"),
        ("message.complained", "Spam complaint"),
        ("campaign.started", "Campaign started"),
        ("campaign.completed", "Campaign completed"),
        ("contact.created", "Contact created"),
        ("contact.updated", "Contact updated"),
        ("contact.unsubscribed", "Contact unsubscribed"),
        ("workflow.started", "Workflow run started"),
        ("workflow.completed", "Workflow run completed"),
        ("event.received", "Business event received"),
    ]

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="webhook_endpoints"
    )
    url = models.URLField(max_length=500)
    signing_secret = models.CharField(max_length=64, blank=True)
    event_types = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    last_error = models.TextField(blank=True, default="")
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_success_at = models.DateTimeField(blank=True, null=True)
    last_failure_at = models.DateTimeField(blank=True, null=True)
    disabled_at = models.DateTimeField(blank=True, null=True)
    disabled_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # Consecutive exhausted deliveries before the endpoint is auto-disabled.
    AUTO_DISABLE_THRESHOLD = 10

    class Meta:
        ordering = ["-created_at"]

    @staticmethod
    def generate_secret() -> str:
        return "whsec_" + secrets.token_urlsafe(32)

    def save(self, *args, **kwargs):
        if not self.signing_secret:
            self.signing_secret = self.generate_secret()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.url} ({self.account})"

    @property
    def health(self) -> str:
        if not self.is_active:
            return "disabled"
        if self.consecutive_failures > 0:
            return "degraded"
        return "healthy"

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_success_at = timezone.now()
        self.last_error = ""
        self.save(update_fields=["consecutive_failures", "last_success_at", "last_error"])

    def record_failure(self, error: str = "") -> bool:
        """Register one exhausted delivery. Returns True if this auto-disabled the endpoint."""
        self.consecutive_failures = (self.consecutive_failures or 0) + 1
        self.last_failure_at = timezone.now()
        if error:
            self.last_error = error[:2000]
        fields = ["consecutive_failures", "last_failure_at", "last_error"]
        disabled = False
        if self.is_active and self.consecutive_failures >= self.AUTO_DISABLE_THRESHOLD:
            self.is_active = False
            self.disabled_at = timezone.now()
            self.disabled_reason = (
                f"Auto-disabled after {self.consecutive_failures} consecutive failed deliveries"
            )
            fields += ["is_active", "disabled_at", "disabled_reason"]
            disabled = True
        self.save(update_fields=fields)
        return disabled

    def reactivate(self) -> None:
        self.is_active = True
        self.consecutive_failures = 0
        self.disabled_at = None
        self.disabled_reason = ""
        self.last_error = ""
        self.save(update_fields=[
            "is_active", "consecutive_failures", "disabled_at", "disabled_reason", "last_error",
        ])


class WebhookDelivery(models.Model):
    """One attempted (or retried) delivery of an event to a WebhookEndpoint."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        EXHAUSTED = "exhausted", "Exhausted"

    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries"
    )
    event_type = models.CharField(max_length=50)
    message = models.ForeignKey(
        EmailMessage, on_delete=models.SET_NULL, blank=True, null=True
    )
    payload = models.JSONField(default=dict)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    response_code = models.PositiveSmallIntegerField(blank=True, null=True)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["endpoint", "status"])]

    def mark_succeeded(self, response_code: int) -> None:
        self.status = self.Status.SUCCEEDED
        self.response_code = response_code
        self.attempt_count += 1
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["status", "response_code", "attempt_count", "last_attempt_at"])

    def mark_failed(self, response_code: int | None, *, exhausted: bool = False) -> None:
        self.status = self.Status.EXHAUSTED if exhausted else self.Status.FAILED
        self.response_code = response_code
        self.attempt_count += 1
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["status", "response_code", "attempt_count", "last_attempt_at"])

    def __str__(self):
        return f"{self.event_type} -> {self.endpoint_id} [{self.status}]"


class EmailTrackingEvent(models.Model):
    """An open or click event recorded when a recipient interacts with an email."""

    class Kind(models.TextChoices):
        OPEN = "open", "Open"
        CLICK = "click", "Click"

    message = models.ForeignKey(
        EmailMessage, on_delete=models.CASCADE, related_name="tracking_events"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    url = models.TextField(blank=True, default="")  # only set for click events
    ip = models.GenericIPAddressField(blank=True, null=True)
    ua = models.CharField(max_length=512, blank=True, default="")
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["message", "kind"]),
            models.Index(fields=["occurred_at"]),
        ]

    def __str__(self):
        return f"{self.kind} on message {self.message_id} at {self.occurred_at}"


class SuppressionListEntry(models.Model):
    """Email addresses suppressed from sending due to bounces, complaints, or unsubscribes.

    Used for SES compliance and deliverability best practices. Checked before every send
    to prevent bouncing to known-bad addresses or sending to unsubscribed recipients.
    """

    class Reason(models.TextChoices):
        BOUNCE = "bounce", "Hard Bounce"
        SOFT_BOUNCE = "soft_bounce", "Transient/Undetermined Bounce"
        COMPLAINT = "complaint", "Complaint (Abuse Report)"
        UNSUBSCRIBE = "unsubscribe", "Unsubscribed"
        INVALID = "invalid", "Failed Validation"
        MANUAL = "manual", "Manually Suppressed"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_suppressions"
    )
    email = models.EmailField()
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.BOUNCE)

    # For auditing: which message triggered the suppression (if any)
    triggered_by_message = models.ForeignKey(
        EmailMessage,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="suppression_entries",
    )

    # SNS bounce/complaint metadata (if from SES)
    bounce_type = models.CharField(
        max_length=20, blank=True, default="",
        help_text="SES bounce type: Permanent, Transient, or Undetermined"
    )

    # Track repeat events for soft bounces and other reasons
    bounce_count = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "email"]),
            models.Index(fields=["account", "reason"]),
            models.Index(fields=["email"]),
        ]
        # Prevent duplicate suppressions per email per account
        unique_together = ["account", "email"]

    def __str__(self):
        return f"{self.email} ({self.get_reason_display()})"


class SendReputation(models.Model):
    """Rolling bounce / complaint counters per account, with a circuit breaker.

    Counters accumulate over a trailing window (``MailProviderSettings.
    reputation_window_hours``) and reset when the window rolls over. When the
    bounce or complaint rate crosses the configured halt threshold the account's
    non-system sends are blocked until an operator resets it — this is what
    keeps one tenant's bad list from getting the whole SES account suspended.
    """

    class State(models.TextChoices):
        OK = "ok", "OK"
        WARNED = "warned", "Warned"
        HALTED = "halted", "Halted"

    account = models.OneToOneField(
        "accounts.Account", on_delete=models.CASCADE, related_name="send_reputation"
    )
    window_started_at = models.DateTimeField(auto_now_add=True)
    sent = models.PositiveIntegerField(default=0)
    bounced = models.PositiveIntegerField(default=0)
    complained = models.PositiveIntegerField(default=0)

    state = models.CharField(max_length=10, choices=State.choices, default=State.OK)
    state_changed_at = models.DateTimeField(auto_now_add=True)
    halted_reason = models.CharField(max_length=255, blank=True, default="")

    updated_at = models.DateTimeField(auto_now=True)

    @property
    def bounce_rate(self) -> float:
        return (self.bounced / self.sent) if self.sent else 0.0

    @property
    def complaint_rate(self) -> float:
        return (self.complained / self.sent) if self.sent else 0.0

    def __str__(self):
        return f"{self.account} reputation [{self.state}] b={self.bounce_rate:.3%}"


class ProcessedSnsMessage(models.Model):
    """Durable idempotency ledger for SES/SNS notifications.

    The webhook also keeps a 7-day cache entry as a fast path, but a cache flush
    would otherwise let SNS re-deliveries replay suppression/status writes. A row
    here is the authoritative "already handled" record.
    """

    message_id = models.CharField(max_length=255, primary_key=True)
    event_type = models.CharField(max_length=40, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["received_at"])]

    def __str__(self):
        return f"{self.message_id} ({self.event_type or 'unknown'})"


class DeliverabilitySnapshot(models.Model):
    """Daily point-in-time deliverability score, for trend lines and week-over-week deltas.

    ``domain`` NULL means the account-wide roll-up. Written once per day by
    ``apps.email.tasks.snapshot_deliverability``.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="deliverability_snapshots"
    )
    domain = models.ForeignKey(
        EmailDomain, on_delete=models.CASCADE, blank=True, null=True
    )
    day = models.DateField()
    score = models.PositiveIntegerField()
    grade = models.CharField(max_length=16, default="")
    checks = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "domain", "day"], name="uniq_deliverability_snapshot"
            )
        ]
        indexes = [models.Index(fields=["account", "day"])]
        ordering = ["-day"]

    def __str__(self):
        scope = self.domain.domain if self.domain_id else "account"
        return f"{scope} {self.day}: {self.score}"


class TemplateComponent(models.Model):
    """A reusable snippet a tenant can drop into templates with
    ``{% component "name" arg=value %}``. Built-in components (AkilentButton,
    AkilentHeader, …) are defined in ``apps.email.template_components`` and do
    not need a row here.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="template_components"
    )
    name = models.CharField(max_length=64)
    html = models.TextField(blank=True, default="")
    text = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "name"], name="uniq_template_component_account_name"
            )
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name
