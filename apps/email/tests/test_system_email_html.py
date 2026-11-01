"""Branded HTML for Akilent's own (system) emails: the shared shell, the structured
builder, the DB-row fallback and the per-email bodies."""

from unittest import mock

import pytest
from django.contrib.auth.models import User

from apps.email.services.html_email import build_transactional_html

SEND = "apps.email.services.send.send_system_email"


@pytest.mark.django_db
class TestBuilder:
    def test_renders_every_optional_part(self):
        html = build_transactional_html(
            "First paragraph.\n\nSecond paragraph.",
            "Subject",
            eyebrow="Billing",
            heading="Payment confirmed",
            preheader="Inbox preview line",
            code="123456",
            details=[("Plan", "Starter"), ("Amount paid", "$19.00")],
            notice="Heads up",
            notice_tone="warning",
            cta_url="https://example.com/billing/",
            cta_label="View billing",
            small_print="Keep this email.",
            reason="Because reasons.",
        )
        for part in (
            "Billing",
            "Payment confirmed",
            "Inbox preview line",
            "123456",
            "Starter",
            "$19.00",
            "Heads up",
            'href="https://example.com/billing/"',
            "View billing",
            "Keep this email.",
            "Because reasons.",
            "First paragraph.",
            "Second paragraph.",
        ):
            assert part in html

    def test_plain_call_still_works_and_omits_empty_parts(self):
        html = build_transactional_html("Just text.", "Subject")
        assert "Just text." in html
        assert "<h1" not in html
        assert "letter-spacing:8px" not in html  # no code box

    def test_bare_urls_become_links_and_text_is_escaped(self):
        html = build_transactional_html(
            "See https://example.com/plans/ <script>x</script>", "S"
        )
        assert 'href="https://example.com/plans/"' in html
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


@pytest.mark.django_db
class TestSystemTemplateFallback:
    @pytest.fixture(autouse=True)
    def seeded_row(self):
        # Exactly what migration 0015 seeds: an active row with text only.
        from apps.email.models import SystemEmailTemplate
        from apps.email.services.system_templates import _seeded_rows

        seed = _seeded_rows()["accounts.verify"]
        SystemEmailTemplate.objects.update_or_create(
            key="accounts.verify",
            defaults={
                "name": seed["name"],
                "subject": seed["subject"],
                "text_body": seed["text_body"],
                "html_body": "",
                "is_active": True,
            },
        )

    def test_an_edited_text_row_keeps_the_operators_words_in_html(self):
        from apps.email.models import SystemEmailTemplate
        from apps.email.services.system_templates import render_system_email

        SystemEmailTemplate.objects.filter(key="accounts.verify").update(
            text_body="New wording from ops. Confirm here: {{ link }}"
        )
        _, _, html = render_system_email(
            "accounts.verify",
            {"site_name": "Akilent", "link": "https://x.test/v/1/"},
            fallback_subject_template="accounts/verify_email_subject.txt",
            fallback_body_template="accounts/verify_email.txt",
            fallback_html_template="accounts/verify_email.html",
        )
        assert "New wording from ops." in html
        assert 'href="https://x.test/v/1/"' in html
        assert "Confirm email address" not in html  # not the file template's old copy

    def test_seeded_text_only_row_still_gets_the_branded_html(self):
        from apps.email.services.system_templates import render_system_email

        user = User.objects.create_user("u", "u@example.com", "pw")
        subject, text, html = render_system_email(
            "accounts.verify",
            {
                "user": user,
                "site_name": "Akilent",
                "link": "https://x.test/verify/abc/",
            },
            fallback_subject_template="accounts/verify_email_subject.txt",
            fallback_body_template="accounts/verify_email.txt",
            fallback_html_template="accounts/verify_email.html",
        )
        assert "https://x.test/verify/abc/" in text
        assert 'href="https://x.test/verify/abc/"' in html
        assert "Confirm email address" in html

    def test_row_html_override_wins(self):
        from apps.email.models import SystemEmailTemplate
        from apps.email.services.system_templates import render_system_email

        SystemEmailTemplate.objects.filter(key="accounts.verify").update(
            html_body="<p>custom {{ link }}</p>"
        )
        _, _, html = render_system_email(
            "accounts.verify",
            {"site_name": "Akilent", "link": "L"},
            fallback_subject_template="accounts/verify_email_subject.txt",
            fallback_body_template="accounts/verify_email.txt",
            fallback_html_template="accounts/verify_email.html",
        )
        assert html == "<p>custom L</p>"


@pytest.mark.django_db
class TestPerEmailHtml:
    def test_sign_in_code_is_sent_with_html_and_the_code(self):
        from apps.accounts.email_mfa import deliver_code
        from apps.accounts.models import EmailLoginCode

        user = User.objects.create_user("c", "c@example.com", "pw")
        row = mock.Mock(spec=EmailLoginCode)
        row.user, row.purpose = user, "login"
        with mock.patch(SEND) as send:
            deliver_code(row)
        kwargs = send.call_args.kwargs
        code = kwargs["text_body"].split("Your code is ")[1][:6]
        assert code in kwargs["html_body"]
        assert kwargs["sender_label"] == "Security"

    def test_security_notice_is_sent_with_html(self):
        from apps.accounts.security_notices import RECOVERY_REGENERATED, _send

        user = User.objects.create_user("s", "s@example.com", "pw")
        with mock.patch(SEND) as send:
            _send(user.pk, user.email, RECOVERY_REGENERATED)
        kwargs = send.call_args.kwargs
        assert "Your recovery codes were replaced" in kwargs["html_body"]
        assert "/settings/security/" in kwargs["html_body"]


@pytest.mark.django_db
class TestPaymentConfirmation:
    @pytest.fixture
    def setup(self, db):
        from datetime import timedelta

        from django.utils import timezone

        from apps.accounts.models import Account, Membership
        from apps.billing.models import Plan, Subscription

        plan = Plan.objects.create(
            slug="starter-x", name="Starter", price_monthly="19.00"
        )
        owner = User.objects.create_user("o", "owner@example.com", "pw")
        account = Account.objects.create(company_name="Acme")
        Membership.objects.create(
            user=owner, account=account, role=Membership.Role.OWNER
        )
        sub, _ = Subscription.objects.update_or_create(
            account=account,
            defaults={
                "plan": plan,
                "status": Subscription.ACTIVE,
                "current_period_start": timezone.now(),
                "current_period_end": timezone.now() + timedelta(days=30),
            },
        )
        return sub

    def test_paid_notice_sends_a_confirmation_with_details(self, setup):
        from apps.billing.tasks import send_billing_notice

        with mock.patch(SEND) as send:
            send_billing_notice(setup.account_id, "paid", None)
        args, kwargs = send.call_args
        assert args[0] == "owner@example.com"
        assert args[1] == "Payment confirmed — you're on Starter"
        assert "Payment confirmed" in kwargs["html_body"]
        assert "Active until" in kwargs["html_body"]
        assert "/billing/plans/" in kwargs["text_body"]

    def test_link_only_paragraphs_become_the_button(self):
        from apps.billing.tasks import _strip_link_lines

        body = "Intro.\n\nPaid: $19.\n\nBilling: https://x.test/billing/plans/"
        assert _strip_link_lines(body) == "Intro."

    def test_only_the_closing_link_sentence_is_stripped(self):
        from apps.billing.tasks import _strip_link_lines

        body = (
            "PayPal couldn't take the payment.\n\n"
            "Your features stay on until 05 November 2026. Billing: https://x.test/b/"
        )
        assert _strip_link_lines(body) == (
            "PayPal couldn't take the payment.\n\n"
            "Your features stay on until 05 November 2026."
        )


@pytest.mark.django_db
class TestConsolePreviews:
    def test_every_sample_renders_in_the_new_shell(self):
        from apps.core.console.email_previews import SAMPLES

        for key, (_, _, render) in SAMPLES.items():
            html = render()
            assert "em-card" in html, key
            assert "{{" not in html and "{%" not in html, key

    def test_preview_pages_are_operator_only(self, client):
        admin = User.objects.create_superuser("op", "op@example.com", "pw")
        assert client.get("/manage/emails/").status_code in (302, 403)
        client.force_login(admin)
        assert client.get("/manage/emails/?email=payment-confirmed").status_code == 200
        assert client.get("/manage/emails/sign-in-code/frame/").status_code == 200
        assert client.get("/manage/emails/nope/frame/").status_code == 404
