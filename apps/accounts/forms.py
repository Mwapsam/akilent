from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, UserCreationForm
from django.contrib.auth.models import User

from apps.accounts.models import Account, Invitation


class SignupForm(UserCreationForm):
    """Service-first signup: personal + business details that create the User
    and the Account in one step.

    Email is the login credential (see apps.accounts.backends.EmailBackend),
    so there's no separate username field — ``User.username`` is set from the
    email in the signup view. ``selected_services`` and ``plan`` come from the
    wizard's earlier steps and are validated against each other here so the
    hidden fields can't be tampered.
    """

    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    first_name = forms.CharField(max_length=150, required=True, label="First name")
    last_name = forms.CharField(max_length=150, required=True, label="Last name")

    company_name = forms.CharField(max_length=255, required=True, label="Company name")
    legal_name = forms.CharField(max_length=255, required=False, label="Legal / registered name")
    website = forms.CharField(max_length=255, required=False)
    industry = forms.CharField(max_length=120, required=False)
    company_size = forms.CharField(max_length=40, required=False, label="Company size")
    phone = forms.CharField(max_length=40, required=True, label="Phone number")

    address_line1 = forms.CharField(max_length=255, required=True, label="Address line 1")
    address_line2 = forms.CharField(max_length=255, required=False, label="Address line 2")
    city = forms.CharField(max_length=120, required=True)
    state_region = forms.CharField(max_length=120, required=False, label="State / region")
    postal_code = forms.CharField(max_length=40, required=False, label="Postal code")
    country = forms.CharField(max_length=120, required=True)

    selected_services = forms.ChoiceField(choices=Account.Services.choices)
    # Chosen on the wizard's Package step; round-tripped through errors.
    plan = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = User
        fields = (
            "email", "first_name", "last_name", "company_name", "legal_name",
            "website", "industry", "company_size", "phone", "address_line1",
            "address_line2", "city", "state_region", "postal_code", "country",
            "selected_services", "plan", "password1", "password2",
        )

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        services = cleaned.get("selected_services")
        plan_slug = (cleaned.get("plan") or "").strip()
        if not plan_slug:
            self.add_error("plan", "Choose a subscription package.")
            return cleaned

        from apps.billing.models import Plan

        plan = Plan.objects.filter(slug=plan_slug, is_active=True).first()
        if plan is None:
            self.add_error("plan", "That subscription package is no longer available.")
        elif services and plan.service_type != services:
            self.add_error(
                "plan",
                "That package doesn't match the services you selected.",
            )
        return cleaned


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
        self.fields["username"].max_length = 254
        self.fields["username"].widget.attrs["maxlength"] = 254


class ProfileForm(forms.ModelForm):
    """Lets a signed-in user edit their own name and email."""

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")
        labels = {"first_name": "First name", "last_name": "Last name", "email": "Email"}

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
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


class PasswordResetForm(PasswordResetForm):
    """Password reset form that sends via the configured email provider (SES/SMTP).

    Overrides Django's default send_mail to use send_system_email instead,
    which respects suppression lists and the active send provider.
    """

    def send_mail(
        self, subject_template_name, email_template_name, context, from_email,
        to_email, html_email_template_name=None,
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
            )
        except Exception as exc:
            raise forms.ValidationError(f"Failed to send password reset email: {exc}")


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
            "legal_name", "address_line1", "address_line2", "city",
            "state_region", "postal_code", "country",
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
        # Whitespace-only values would pass `required` but render an empty footer.
        for name, value in list(cleaned.items()):
            if isinstance(value, str):
                cleaned[name] = value.strip()
        for name in ("address_line1", "city", "country"):
            if name in cleaned and not cleaned[name]:
                self.add_error(name, "This field is required.")
        return cleaned
