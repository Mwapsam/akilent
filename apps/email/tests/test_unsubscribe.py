"""List-Unsubscribe (RFC 8058) wiring for bulk sends."""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email.models import (
    BulkEmailCampaign,
    EmailDomain,
    EmailMessage,
    UnsubscribeToken,
)
from apps.email.services.unsubscribe import (
    build_list_unsubscribe_headers,
    build_unsubscribe_context,
    create_unsubscribe_token,
    get_unsubscribe_url,
)


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(
        company_name="Acme",
        address_line1="1 Market St",
        city="Lusaka",
        country="ZM",
    )
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.mark.django_db
def test_token_fits_column_and_is_unique(account):
    t1 = create_unsubscribe_token(account, "a@x.com")
    t2 = create_unsubscribe_token(account, "a@x.com")
    assert t1 != t2
    assert len(t1) <= 64
    assert UnsubscribeToken.objects.filter(token=t1, is_used=False).exists()


@pytest.mark.django_db
def test_build_headers_shape(account, settings):
    settings.DEFAULT_FROM_EMAIL = "no-reply@acme.com"
    settings.BASE_DOMAIN = "app.acme.com"
    settings.DEBUG = False
    headers = build_list_unsubscribe_headers(account, "rcpt@x.com")

    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    lu = headers["List-Unsubscribe"]
    assert lu.startswith("<https://app.acme.com/email/t/unsub/")
    assert "<mailto:no-reply@acme.com?subject=unsubscribe>" in lu

    token = UnsubscribeToken.objects.filter(email="rcpt@x.com").get().token
    assert token in lu


@pytest.mark.django_db
def test_get_unsubscribe_url_without_request(account, settings):
    settings.BASE_DOMAIN = "app.acme.com"
    settings.DEBUG = False
    url = get_unsubscribe_url("unsub_abc")
    assert url == "https://app.acme.com/email/t/unsub/unsub_abc/"


@pytest.mark.django_db
def test_send_path_attaches_headers_for_campaign(account, monkeypatch):
    domain = EmailDomain.objects.create(
        account=account, domain="acme.com", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account, domain=domain,
        from_email="news@acme.com", subject_override="Hi",
    )
    msg = EmailMessage.objects.create(
        account=account, domain=domain, campaign=campaign,
        from_email="news@acme.com", to_email="rcpt@x.com", subject="Hi",
    )

    captured = {}

    class _Provider:
        def send(self, outbound):
            captured["headers"] = dict(outbound.headers)
            from apps.email.types import SendResult
            return SendResult(success=True, provider_message_id="mid-1")

    monkeypatch.setattr("apps.email.tasks.get_send_provider", lambda: _Provider())
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda acc, em: False
    )

    from apps.email.tasks import _send_email_message

    class _Task:
        class request:
            retries = 0

    _send_email_message(_Task, msg, "text", "<p>html</p>")

    assert captured["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "List-Unsubscribe" in captured["headers"]
    assert UnsubscribeToken.objects.filter(email="rcpt@x.com", campaign=campaign).exists()


@pytest.mark.django_db
def test_send_path_no_headers_for_transactional(account, monkeypatch):
    msg = EmailMessage.objects.create(
        account=account, from_email="app@acme.com", to_email="rcpt@x.com", subject="Hi",
    )
    captured = {}

    class _Provider:
        def send(self, outbound):
            captured["headers"] = dict(outbound.headers)
            from apps.email.types import SendResult
            return SendResult(success=True, provider_message_id="mid-2")

    monkeypatch.setattr("apps.email.tasks.get_send_provider", lambda: _Provider())
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda acc, em: False
    )

    from apps.email.tasks import _send_email_message

    class _Task:
        class request:
            retries = 0

    _send_email_message(_Task, msg, "text", "")
    assert captured["headers"] == {}


@pytest.mark.django_db
def test_one_click_post_unsubscribes(client, account):
    token = create_unsubscribe_token(account, "gone@x.com")
    resp = client.post(
        f"/email/t/unsub/{token}/",
        data="List-Unsubscribe=One-Click",
        content_type="application/x-www-form-urlencoded",
    )
    assert resp.status_code == 200
    UnsubscribeToken.objects.get(token=token, is_used=True)

    from apps.email.services.suppression import is_suppressed
    assert is_suppressed(account, "gone@x.com")


@pytest.mark.django_db
def test_context_mints_exactly_one_token_backing_header_and_link(account):
    """The body link and the header must resolve to the same token.

    Minting separately would leave an orphan UnsubscribeToken per send and mean
    clicking the footer didn't invalidate the header's one-click token.
    """
    ctx = build_unsubscribe_context(account, "rcpt@x.com")

    tokens = UnsubscribeToken.objects.filter(email="rcpt@x.com")
    assert tokens.count() == 1
    assert ctx["token"] == tokens.get().token
    assert ctx["token"] in ctx["url"]
    assert f"<{ctx['url']}>" in ctx["headers"]["List-Unsubscribe"]


@pytest.mark.django_db
def test_campaign_body_carries_unsubscribe_link_and_postal_address(account, monkeypatch):
    domain = EmailDomain.objects.create(
        account=account, domain="acme.com", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account, domain=domain,
        from_email="news@acme.com", subject_override="Hi",
    )
    msg = EmailMessage.objects.create(
        account=account, domain=domain, campaign=campaign,
        from_email="news@acme.com", to_email="rcpt@x.com", subject="Hi",
    )

    captured = {}

    class _Provider:
        def send(self, outbound):
            captured["html"] = outbound.html_body
            captured["text"] = outbound.text_body
            captured["headers"] = dict(outbound.headers)
            from apps.email.types import SendResult
            return SendResult(success=True, provider_message_id="mid-3")

    monkeypatch.setattr("apps.email.tasks.get_send_provider", lambda: _Provider())
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda acc, em: False
    )

    from apps.email.tasks import _send_email_message

    class _Task:
        class request:
            retries = 0

    _send_email_message(_Task, msg, "text", "<html><body><p>Hi</p></body></html>")

    token = UnsubscribeToken.objects.get(email="rcpt@x.com", campaign=campaign)
    url = get_unsubscribe_url(token.token)

    # The visible opt-out, in the body -- not just the header.
    assert url in captured["html"]
    assert url in captured["text"]
    # CAN-SPAM physical address.
    assert "1 Market St" in captured["html"]
    assert "Lusaka" in captured["html"]
    # ...and the header still points at the same token.
    assert f"<{url}>" in captured["headers"]["List-Unsubscribe"]


@pytest.mark.django_db
def test_unsubscribe_link_is_never_click_tracked(account, monkeypatch):
    """Regression: the footer must be appended after the tracking rewrite.

    apply_tracking rewrites every href into a /email/t/click/ redirect. If the
    footer went in first, the unsubscribe link would be swallowed by it -- and
    a one-click unsubscribe that 302s through a tracker is not a working
    unsubscribe.
    """
    domain = EmailDomain.objects.create(
        account=account, domain="acme.com", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account, domain=domain,
        from_email="news@acme.com", subject_override="Hi",
    )
    msg = EmailMessage.objects.create(
        account=account, domain=domain, campaign=campaign,
        from_email="news@acme.com", to_email="rcpt@x.com", subject="Hi",
    )

    captured = {}

    class _Provider:
        def send(self, outbound):
            captured["html"] = outbound.html_body
            from apps.email.types import SendResult
            return SendResult(success=True, provider_message_id="mid-4")

    monkeypatch.setattr("apps.email.tasks.get_send_provider", lambda: _Provider())
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda acc, em: False
    )
    # Force tracking on so the rewrite actually runs.
    monkeypatch.setattr(
        "apps.billing.limits.LimitChecker.has_feature", lambda self, f: True
    )

    from apps.email.tasks import _send_email_message

    class _Task:
        class request:
            retries = 0

    _send_email_message(
        _Task, msg, "text",
        '<html><body><a href="https://acme.com/sale">Sale</a></body></html>',
    )

    token = UnsubscribeToken.objects.get(email="rcpt@x.com", campaign=campaign)
    url = get_unsubscribe_url(token.token)

    # The marketing link got tracked...
    assert "/email/t/click/" in captured["html"]
    # ...but the unsubscribe link is present verbatim.
    assert f'href="{url}"' in captured["html"]


@pytest.mark.django_db
def test_unsubscribe_marks_the_contact_unsubscribed(client, account):
    """The suppression list alone leaves the UI claiming they're subscribed."""
    from apps.contacts.models import Contact, ContactEvent

    contact = Contact.objects.create(account=account, email="gone@x.com")
    assert contact.status == Contact.Status.SUBSCRIBED

    token = create_unsubscribe_token(account, "gone@x.com")
    resp = client.get(f"/email/t/unsub/{token}/")
    assert resp.status_code == 200

    contact.refresh_from_db()
    assert contact.status == Contact.Status.UNSUBSCRIBED
    assert contact.consent_status == Contact.ConsentStatus.OPTED_OUT
    assert contact.opt_out_at is not None
    assert "email_unsubscribe" in contact.opt_out_reason

    assert ContactEvent.objects.filter(
        contact=contact, type="email.unsubscribed"
    ).count() == 1

    from apps.email.services.suppression import is_suppressed
    assert is_suppressed(account, "gone@x.com")


@pytest.mark.django_db
def test_unsubscribe_survives_a_missing_contact(client, account):
    """A recipient with no Contact row must still be suppressed."""
    token = create_unsubscribe_token(account, "nobody@x.com")
    resp = client.get(f"/email/t/unsub/{token}/")
    assert resp.status_code == 200

    from apps.email.services.suppression import is_suppressed
    assert is_suppressed(account, "nobody@x.com")


@pytest.mark.django_db
def test_reusing_a_spent_token_is_a_no_op(client, account):
    from apps.contacts.models import Contact, ContactEvent

    contact = Contact.objects.create(account=account, email="gone@x.com")
    token = create_unsubscribe_token(account, "gone@x.com")

    client.get(f"/email/t/unsub/{token}/")
    first_opt_out = Contact.objects.get(pk=contact.pk).opt_out_at

    resp = client.get(f"/email/t/unsub/{token}/")
    assert resp.status_code == 200
    assert Contact.objects.get(pk=contact.pk).opt_out_at == first_opt_out
    assert ContactEvent.objects.filter(
        contact=contact, type="email.unsubscribed"
    ).count() == 1
