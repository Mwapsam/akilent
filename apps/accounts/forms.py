from typing import TYPE_CHECKING

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    UserCreationForm,
)
from django.contrib.auth.models import User

from apps.accounts.models import Account, Invitation

if TYPE_CHECKING:
    from apps.billing.models import Plan

# Channels each plan service type covers (Plan.service_type / Account.selected_services).
SERVICE_CHANNELS: dict[str, set[str]] = {
    Account.Services.EMAIL.value: {"email"},
    Account.Services.WHATSAPP.value: {"whatsapp"},
    Account.Services.BOTH.value: {"email", "whatsapp"},
}

# The channels a business can pick at signup, in display order, with what each one does
# here (docs/marketing/messaging.md, section 2). Facebook is Page publishing only.
CHANNEL_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("whatsapp", "WhatsApp", "A shared inbox, auto-replies and campaigns"),
    ("instagram", "Instagram", "DMs in the same inbox, comment replies and posts"),
    ("facebook", "Facebook Page", "Publish and schedule posts to your Page"),
    ("email", "Email", "Send from your own domain, with templates and campaigns"),
)


def plan_channels(plan, channels, features=None) -> frozenset[str]:
    """The channels ``plan`` gives while only ``channels`` are on offer.

    WhatsApp and Email come from the plan's service type, Instagram from its
    ``instagram`` feature and Facebook (Page publishing) from ``content_studio``.
    ``features`` is the plan's feature keys when the caller already has them.
    """
    if features is None:
        features = set(plan.features.values_list("key", flat=True))
    given = set(SERVICE_CHANNELS.get(plan.service_type, set()))
    if "instagram" in features:
        given.add("instagram")
    if "content_studio" in features:
        given.add("facebook")
    return frozenset(given & set(channels))


def signup_offer() -> tuple[
    list[tuple["Plan", frozenset[str]]], list[tuple[str, str, str]]
]:
    """What signup can sell right now: active plans (cheapest first) with the channels each
    gives, and the channels a business may pick (``CHANNEL_CHOICES`` rows).

    Only public channels count (on, and not restricted while Meta reviews the app), and a
    channel is offered only when some plan gives it, so a pick can never lead to an empty
    package step.
    """
    from apps.billing.models import Plan, PlanFeature
    from apps.core.channels import public_channels

    public = public_channels()
    plans = list(Plan.objects.filter(is_active=True).order_by("price_monthly"))
    features: dict[int, set[str]] = {}
    for plan_id, key in PlanFeature.objects.filter(plan__in=plans).values_list(
        "plan_id", "key"
    ):
        features.setdefault(plan_id, set()).add(key)
    offered = []
    for plan in plans:
        given = plan_channels(plan, public, features.get(plan.pk, set()))
        if given:
            offered.append((plan, given))
    covered = set().union(*(given for _, given in offered)) if offered else set()
    choices = [row for row in CHANNEL_CHOICES if row[0] in covered]
    return offered, choices


def plans_covering(offered, picks) -> list:
    """The offered plans that give every picked channel."""
    picks = set(picks)
    return [plan for plan, given in offered if picks <= given]


class SignupForm(UserCreationForm):
    """Channel-first signup: personal + business details that create the User
    and the Account in one step.

    Email is the login credential (see apps.accounts.backends.EmailBackend),
    so there's no separate username field — ``User.username`` is set from the
    email in the signup view. ``selected_channels`` and ``plan`` come from the
    wizard's earlier steps and are validated against each other here so the
    hidden fields can't be tampered.
    """

    email = forms.EmailField(
        required=True, widget=forms.EmailInput(attrs={"autocomplete": "email"})
    )
    first_name = forms.CharField(max_length=150, required=True, label="First name")
    last_name = forms.CharField(max_length=150, required=True, label="Last name")

    company_name = forms.CharField(max_length=255, required=True, label="Company name")
    legal_name = forms.CharField(
        max_length=255, required=False, label="Legal / registered name"
    )
    website = forms.CharField(max_length=255, required=False)
    industry = forms.CharField(max_length=120, required=False)
    company_size = forms.CharField(max_length=40, required=False, label="Company size")
    phone = forms.CharField(max_length=40, required=True, label="Phone number")

    address_line1 = forms.CharField(
        max_length=255, required=True, label="Address line 1"
    )
    address_line2 = forms.CharField(
        max_length=255, required=False, label="Address line 2"
    )
    city = forms.CharField(max_length=120, required=True)
    state_region = forms.CharField(
        max_length=120, required=False, label="State / region"
    )
    postal_code = forms.CharField(max_length=40, required=False, label="Postal code")
    country = forms.CharField(max_length=120, required=True)

    selected_channels = forms.MultipleChoiceField(
        choices=[(v, label) for v, label, _ in CHANNEL_CHOICES],
        error_messages={"required": "Choose at least one channel."},
    )
    # Chosen on the wizard's Package step; round-tripped through errors.
    plan = forms.CharField(required=False, widget=forms.HiddenInput())
    # The term a paid plan is billed for (monthly, annually, ...). Ignored for free plans.
    billing_period = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = User
        fields = (
            "email",
            "first_name",
            "last_name",
            "company_name",
            "legal_name",
            "website",
            "industry",
            "company_size",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state_region",
            "postal_code",
            "country",
            "selected_channels",
            "plan",
            "billing_period",
            "password1",
            "password2",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only channels on offer are valid, so a switched-off or restricted one is rejected.
        field = self.fields["selected_channels"]
        assert isinstance(field, forms.MultipleChoiceField)
        self._offer, choices = signup_offer()
        field.choices = [(v, label) for v, label, _ in choices]
        self.chosen_plan: Plan | None = None  # set by clean()

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned is None:
            return cleaned
        picks = cleaned.get("selected_channels")
        plan_slug = (cleaned.get("plan") or "").strip()
        if not plan_slug:
            self.add_error("plan", "Choose a subscription package.")
            return cleaned

        from apps.billing.models import Plan

        plan = Plan.objects.filter(slug=plan_slug, is_active=True).first()
        self.chosen_plan = plan
        if plan is None:
            self.add_error("plan", "That subscription package is no longer available.")
        elif picks and plan not in plans_covering(self._offer, picks):
            self.add_error(
                "plan",
                "That package doesn't include every channel you selected.",
            )
        self._clean_billing_period(cleaned, plan)
        return cleaned

    def _clean_billing_period(self, cleaned, plan):
        """The term a paid plan is billed for. A free plan has none, so whatever was
        sent is ignored rather than allowed to block the signup."""
        from apps.billing.models import Subscription

        period = (cleaned.get("billing_period") or "").strip()
        if plan is None or plan.price_monthly <= 0 or not period:
            cleaned["billing_period"] = Subscription.MONTHLY
        elif period not in dict(Subscription.BILLING_PERIOD_CHOICES):
            self.add_error(
                "billing_period", "Choose how often you'd like to be billed."
            )


class LoginForm(AuthenticationForm):
    """Log in with email + password instead of username."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )

    error_messages = {
        "invalid_login": (
            "Please enter a correct email and password. Note that both fields "
            "may be case-sensitive."
        ),
        "inactive": (
            "This account hasn't been verified yet. Check your email for a "
            "confirmation link, or resend it below."
        ),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # AuthenticationForm.__init__ clamps this to User.username's
        # max_length (150); restore the normal email length limit.
        self.fields["username"].max_length = 254  # type: ignore[attr-defined]
        self.fields["username"].widget.attrs["maxlength"] = 254


class ProfileForm(forms.ModelForm):
    """Lets a signed-in user edit their own name and email."""

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")
        labels = {
            "first_name": "First name",
            "last_name": "Last name",
            "email": "Email",
        }

    def clean_email(self):
        email = self.cleaned_data["email"]
        if (
            email
            and User.objects.filter(email__iexact=email)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("Another account already uses this email.")
        return email


class InviteForm(forms.Form):
    """Invite a teammate to the current workspace by email + role."""

    email = forms.EmailField()
    role = forms.ChoiceField(choices=Invitation.INVITE_ROLES)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class AcceptInvitationForm(UserCreationForm):
    """Create a User when accepting an invitation (email comes from the invite)."""

    class Meta:
        model = User
        fields = ("username", "password1", "password2")


class PasswordResetForm(PasswordResetForm):  # type: ignore[no-redef]
    """Password reset form that sends via the configured email provider (SES/SMTP).

    Overrides Django's default send_mail to use send_system_email instead,
    which respects suppression lists and the active send provider.
    """

    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        from django.template.loader import render_to_string

        from apps.email.services.send import send_system_email

        subject = render_to_string(subject_template_name, context).strip()
        message = render_to_string(email_template_name, context)
        html_message = None
        if html_email_template_name is not None:
            html_message = render_to_string(html_email_template_name, context)

        try:
            send_system_email(
                to_email=to_email,
                subject=subject,
                text_body=message,
                html_body=html_message or "",
                sender_label="Security",
            )
        except Exception as exc:
            raise forms.ValidationError(
                f"Failed to send password reset email: {exc}"
            ) from exc


class BusinessAddressForm(forms.ModelForm):
    """The business identity printed in every campaign email footer.

    CAN-SPAM requires marketing email to carry the sender's physical postal
    address, and campaign creation is refused without one (see
    apps.email.services.bulk). Street, city and country are the minimum,
    matching Account.has_postal_address.
    """

    class Meta:
        model = Account
        fields = (
            "legal_name",
            "address_line1",
            "address_line2",
            "city",
            "state_region",
            "postal_code",
            "country",
        )
        labels = {
            "legal_name": "Legal / registered name",
            "address_line1": "Address line 1",
            "address_line2": "Address line 2",
            "state_region": "State / region",
            "postal_code": "Postal code",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("address_line1", "city", "country"):
            self.fields[name].required = True

    def clean(self):
        cleaned = super().clean()
        if cleaned is None:
            return cleaned
        # Whitespace-only values would pass `required` but render an empty footer.
        for name, value in list(cleaned.items()):
            if isinstance(value, str):
                cleaned[name] = value.strip()
        for name in ("address_line1", "city", "country"):
            if name in cleaned and not cleaned[name]:
                self.add_error(name, "This field is required.")
        return cleaned
