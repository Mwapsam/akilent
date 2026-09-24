"""Email consent provenance on Contact, and its enforcement at dispatch.

`status` is the effective deliverability state; `consent_status` records how we
know we may mail someone. They're deliberately separate -- a contact can be
SUBSCRIBED with UNKNOWN consent, which is exactly the pre-consent backlog we
need to be able to see.
"""
import pytest

from apps.accounts.models import Account
from apps.contacts.models import Contact


@pytest.fixture
def account(db):
    return Account.objects.create(
        company_name="Acme",
        address_line1="1 Market St",
        city="Lusaka",
        country="ZM",
    )


@pytest.mark.django_db
def test_new_contacts_start_with_unknown_consent(account):
    """Subscribed is not the same as proven consent."""
    c = Contact.objects.create(account=account, email="a@x.com")
    assert c.status == Contact.Status.SUBSCRIBED
    assert c.consent_status == Contact.ConsentStatus.UNKNOWN
    assert c.consent_at is None
    assert c.is_opted_out is False


@pytest.mark.django_db
def test_record_opt_in_captures_evidence(account):
    c = Contact.objects.create(account=account, email="a@x.com")
    c.record_opt_in(
        "signup_form",
        ip="203.0.113.7",
        evidence={"form": "https://acme.com/newsletter"},
    )
    c.refresh_from_db()

    assert c.consent_status == Contact.ConsentStatus.OPTED_IN
    assert c.consent_source == "signup_form"
    assert c.consent_at is not None
    assert c.consent_ip == "203.0.113.7"
    assert c.consent_evidence == {"form": "https://acme.com/newsletter"}


@pytest.mark.django_db
def test_opt_out_preserves_the_original_opt_in_proof(account):
    """We still need to show how the address got onto the list."""
    c = Contact.objects.create(account=account, email="a@x.com")
    c.record_opt_in("signup_form", evidence={"form": "https://acme.com/n"})
    consent_at = c.consent_at

    c.record_opt_out("email_unsubscribe:get")
    c.refresh_from_db()

    assert c.consent_status == Contact.ConsentStatus.OPTED_OUT
    assert c.is_opted_out is True
    assert c.opt_out_at is not None
    assert c.opt_out_reason == "email_unsubscribe:get"
    # Preserved:
    assert c.consent_source == "signup_form"
    assert c.consent_at == consent_at
    assert c.consent_evidence == {"form": "https://acme.com/n"}


@pytest.mark.django_db
def test_opt_in_after_opt_out_clears_the_opt_out(account):
    c = Contact.objects.create(account=account, email="a@x.com")
    c.record_opt_out("bounced around")
    c.record_opt_in("double_optin")
    c.refresh_from_db()

    assert c.consent_status == Contact.ConsentStatus.OPTED_IN
    assert c.opt_out_at is None
    assert c.opt_out_reason == ""


@pytest.mark.django_db
def test_long_values_are_truncated_to_the_column(account):
    c = Contact.objects.create(account=account, email="a@x.com")
    c.record_opt_in("s" * 200)
    c.record_opt_out("r" * 400)
    c.refresh_from_db()
    assert len(c.consent_source) == 100
    assert len(c.opt_out_reason) == 255


# --- Enforcement at campaign dispatch ----------------------------------------

@pytest.fixture
def campaign(account, db):
    from apps.billing.models import Plan, Subscription
    from decimal import Decimal
    from django.utils import timezone

    from apps.email.models import BulkEmailCampaign, BulkEmailRecipient, EmailDomain

    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        max_emails_per_month=1000, email_apis=True, bulk_email=True,
        max_bulk_recipients_per_campaign=500,
    )
    Subscription.objects.create(
        account=account, plan=plan, status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    domain = EmailDomain.objects.create(
        account=account, domain="acme.com", status=EmailDomain.Status.VERIFIED
    )
    camp = BulkEmailCampaign.objects.create(
        account=account, domain=domain,
        from_email="news@acme.com", subject_override="Hi", text_override="Hello",
        recipient_count=2,
    )
    for email in ("yes@x.com", "no@x.com"):
        BulkEmailRecipient.objects.create(campaign=camp, to_email=email)
    return camp


def _dispatch(monkeypatch, campaign):
    """Run dispatch_campaign with delivery and MX lookups stubbed out."""
    from apps.email.tasks import dispatch_campaign

    monkeypatch.setattr(
        "apps.email.services.validation.validate_recipient", lambda e: True
    )
    monkeypatch.setattr(
        "apps.email.tasks.send_bulk_recipient_email.delay", lambda *a, **k: None
    )
    dispatch_campaign(campaign.id)


@pytest.mark.django_db
def test_opted_out_contacts_are_refused(account, campaign, monkeypatch):
    from apps.email.models import BulkEmailRecipient

    Contact.objects.create(account=account, email="yes@x.com")
    blocked = Contact.objects.create(account=account, email="no@x.com")
    blocked.record_opt_out("asked us to stop")

    _dispatch(monkeypatch, campaign)

    refused = BulkEmailRecipient.objects.get(campaign=campaign, to_email="no@x.com")
    assert refused.status == BulkEmailRecipient.Status.FAILED
    assert "consent" in refused.error.lower()

    allowed = BulkEmailRecipient.objects.get(campaign=campaign, to_email="yes@x.com")
    assert allowed.status != BulkEmailRecipient.Status.FAILED


@pytest.mark.django_db
def test_unknown_consent_passes_unless_explicitly_required(account, campaign, monkeypatch):
    """Default-off, so the pre-consent backlog doesn't become unmailable overnight."""
    from apps.email.models import BulkEmailRecipient

    Contact.objects.create(account=account, email="yes@x.com")
    Contact.objects.create(account=account, email="no@x.com")

    _dispatch(monkeypatch, campaign)

    assert not BulkEmailRecipient.objects.filter(
        campaign=campaign, status=BulkEmailRecipient.Status.FAILED
    ).exists()


@pytest.mark.django_db
def test_require_explicit_consent_refuses_unproven_contacts(account, campaign, monkeypatch):
    from apps.core.models import MailProviderSettings
    from apps.email.models import BulkEmailRecipient

    settings = MailProviderSettings.load()
    settings.require_explicit_consent = True
    settings.save()

    proven = Contact.objects.create(account=account, email="yes@x.com")
    proven.record_opt_in("signup_form")
    Contact.objects.create(account=account, email="no@x.com")  # UNKNOWN

    _dispatch(monkeypatch, campaign)

    refused = BulkEmailRecipient.objects.get(campaign=campaign, to_email="no@x.com")
    assert refused.status == BulkEmailRecipient.Status.FAILED
    allowed = BulkEmailRecipient.objects.get(campaign=campaign, to_email="yes@x.com")
    assert allowed.status != BulkEmailRecipient.Status.FAILED
