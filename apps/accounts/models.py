import secrets
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Account(models.Model):
    class Services(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        BOTH = "both", "Email & WhatsApp"

    class Onboarding(models.TextChoices):
        # service_selection / plan_selection / account_information happen in the
        # browser wizard before anything is persisted; the first state we store
        # is ACCOUNT_CREATED.
        ACCOUNT_CREATED = "account_created", "Account created"
        WHATSAPP_SETUP = "whatsapp_setup", "WhatsApp setup"
        DOMAIN_SETUP = "domain_setup", "Domain setup"
        COMPLETED = "completed", "Completed"

    company_name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Which services this tenant signed up for; set by the signup wizard and
    # used to route service-specific onboarding and tailor the checklist.
    selected_services = models.CharField(
        max_length=10, choices=Services.choices, default=Services.EMAIL
    )

    # Accounts are created active (non-blocking verification); this tracks
    # whether the owner has since clicked the emailed confirmation link.
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    # Set the first time an owner opens the security settings page, so the
    # (optional) onboarding checklist item can mark itself done.
    security_reviewed_at = models.DateTimeField(null=True, blank=True)

    # Resumable service-specific onboarding state. Existing rows default to
    # COMPLETED so they're never dragged back into the wizard flow.
    onboarding_state = models.CharField(
        max_length=20, choices=Onboarding.choices, default=Onboarding.COMPLETED
    )

    # Business profile collected during signup. All optional at the DB level so
    # existing rows and non-wizard creation paths keep working.
    legal_name = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    website = models.CharField(max_length=255, blank=True, default="")
    industry = models.CharField(max_length=120, blank=True, default="")
    company_size = models.CharField(max_length=40, blank=True, default="")
    address_line1 = models.CharField(max_length=255, blank=True, default="")
    address_line2 = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    state_region = models.CharField(max_length=120, blank=True, default="")
    postal_code = models.CharField(max_length=40, blank=True, default="")
    country = models.CharField(max_length=120, blank=True, default="")
    billing_email = models.EmailField(blank=True, default="")

    # Per-account token for the Progstack domain-verification API.
    progstack_token = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["company_name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug(self.company_name or "account")
        super().save(*args, **kwargs)

    @staticmethod
    def _unique_slug(base: str) -> str:
        root = slugify(base) or "account"
        slug = root
        i = 2
        while Account.objects.filter(slug=slug).exists():
            slug = f"{root}-{i}"
            i += 1
        return slug

    @property
    def owner(self):
        membership = self.memberships.filter(role=Membership.Role.OWNER).first()
        return membership.user if membership else None

    @property
    def has_postal_address(self) -> bool:
        """Whether this account can send CAN-SPAM compliant marketing email.

        Every campaign footer renders the sender's physical mailing address, so
        campaign creation is gated on this (apps.email.services.bulk).
        """
        from apps.email.services.compliance_footer import has_postal_address

        return has_postal_address(self)

    def __str__(self):
        return self.company_name


class Membership(models.Model):
    """Links a Django ``User`` to an ``Account`` with a role.

    Allows a tenant to have multiple users and lets us resolve the "current
    account" for a logged-in user.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships"
    )
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.OWNER
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "account")
        indexes = [
            models.Index(fields=["user", "account"]),
        ]

    def __str__(self):
        return f"{self.user} -> {self.account} ({self.role})"


class Invitation(models.Model):
    """A pending invitation for someone to join an ``Account`` with a role.

    Sent by email with a tokened accept link. Accepting either signs the
    recipient into an existing account (matched by email) or lets them create
    one, then creates the corresponding ``Membership``. Owners can never be
    invited — there is exactly one owner (the account creator).
    """

    EXPIRY_DAYS = 7

    # Owner is intentionally excluded — you can only invite admins/members.
    INVITE_ROLES = [
        (Membership.Role.MEMBER, Membership.Role.MEMBER.label),
        (Membership.Role.ADMIN, Membership.Role.ADMIN.label),
    ]

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.CharField(
        max_length=20, choices=INVITE_ROLES, default=Membership.Role.MEMBER
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    invited_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sent_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one *pending* invite per email per account; an accepted
            # one may coexist (e.g. re-inviting someone after removal).
            models.UniqueConstraint(
                fields=["account", "email"],
                condition=models.Q(accepted_at__isnull=True),
                name="uniq_pending_invite_per_account_email",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    @property
    def expires_at(self):
        return self.created_at + timedelta(days=self.EXPIRY_DAYS) if self.created_at else None

    @property
    def is_expired(self) -> bool:
        return bool(self.created_at) and timezone.now() > self.expires_at

    def __str__(self):
        return f"invite {self.email} -> {self.account} ({self.role})"


class BusinessHours(models.Model):
    """When a business is open, in its own timezone (see ``apps.accounts.business_hours``).

    ``schedule`` maps a weekday key (mon..sun) to ``{"open": "09:00", "close": "17:00"}``;
    a missing day is closed. An empty schedule means "no hours set", which is treated as
    always open.
    """

    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="business_hours")
    timezone = models.CharField(max_length=64, default="UTC")
    schedule = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Hours for {self.account_id} ({self.timezone})"


class BusinessProfile(models.Model):
    """What a business tells Akilent about itself, once, in plain answers (see ``apps.accounts.profile``).

    Business data, not AI data: automations use it in replies and template blanks
    (``business.location``...), and AI treats it as checked facts. Every field is optional.
    """

    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="business_profile")
    what_you_sell = models.CharField(max_length=300, blank=True, default="")
    location = models.CharField(max_length=300, blank=True, default="")
    delivers = models.BooleanField(null=True, blank=True)      # None = not answered
    delivery_notes = models.CharField(max_length=300, blank=True, default="")
    # Keys of apps.accounts.profile.PAYMENT_METHODS, plus free text in payment_other.
    payment_methods = models.JSONField(default=list, blank=True)
    payment_other = models.CharField(max_length=120, blank=True, default="")
    website = models.URLField(max_length=300, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Business profile for {self.account_id}"
