"""CAN-SPAM compliance footer (apps.email.services.compliance_footer)."""
import pytest

from apps.accounts.models import Account
from apps.email.services.compliance_footer import (
    FOOTER_MARKER,
    append_footer,
    build_footer,
    format_postal_address,
    has_postal_address,
)

URL = "https://akilent.test/email/t/unsub/unsub_abc/"


@pytest.fixture
def account(db):
    return Account.objects.create(
        company_name="Acme",
        address_line1="1 Market St",
        address_line2="Suite 4",
        city="Lusaka",
        state_region="Lusaka Province",
        postal_code="10101",
        country="ZM",
    )


@pytest.mark.django_db
def test_address_is_comma_joined_and_skips_blanks(account):
    assert format_postal_address(account) == (
        "1 Market St, Suite 4, Lusaka, Lusaka Province, 10101, ZM"
    )

    account.address_line2 = ""
    account.postal_code = ""
    assert format_postal_address(account) == "1 Market St, Lusaka, Lusaka Province, ZM"


@pytest.mark.django_db
def test_has_postal_address_requires_street_city_country(account):
    assert has_postal_address(account) is True
    for field in ("address_line1", "city", "country"):
        original = getattr(account, field)
        setattr(account, field, "")
        assert has_postal_address(account) is False, field
        setattr(account, field, original)


@pytest.mark.django_db
def test_footer_carries_address_and_unsubscribe_link(account):
    text, html = build_footer(account, URL)
    for part in ("1 Market St", "Lusaka", "ZM"):
        assert part in text
        assert part in html
    assert URL in text
    assert URL in html
    assert FOOTER_MARKER in html
    assert "Unsubscribe" in html


@pytest.mark.django_db
def test_html_footer_goes_before_closing_body(account):
    html = "<html><body><p>Hi</p></body></html>"
    _, out = append_footer("body", html, account=account, unsubscribe_url=URL)
    assert out.index(FOOTER_MARKER) < out.index("</body>")
    assert out.endswith("</body></html>")


@pytest.mark.django_db
def test_html_footer_appends_when_there_is_no_body_tag(account):
    _, out = append_footer("body", "<p>Hi</p>", account=account, unsubscribe_url=URL)
    assert out.startswith("<p>Hi</p>")
    assert FOOTER_MARKER in out


@pytest.mark.django_db
def test_append_is_idempotent(account):
    """A retried send must not stack two footers."""
    text, html = append_footer(
        "body", "<html><body><p>Hi</p></body></html>",
        account=account, unsubscribe_url=URL,
    )
    text2, html2 = append_footer(text, html, account=account, unsubscribe_url=URL)
    assert html2 == html
    assert text2 == text
    assert html2.count(FOOTER_MARKER) == 1
    assert text2.count(URL) == 1


@pytest.mark.django_db
def test_text_only_and_html_only_messages(account):
    text, html = append_footer("plain", "", account=account, unsubscribe_url=URL)
    assert URL in text
    assert html == ""

    text, html = append_footer("", "<p>Hi</p>", account=account, unsubscribe_url=URL)
    assert text == ""
    assert URL in html


@pytest.mark.django_db
def test_footer_escapes_account_name(account):
    account.company_name = '<script>alert("x")</script>'
    _, html = build_footer(account, URL)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
