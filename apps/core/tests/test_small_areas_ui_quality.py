"""Phase 8 of the UI quality programme (docs/design/ui-quality-log.md, Entry 15): orders, interested
customers, starter packs, chatbots and support say what the records say, ask before changing live
automations, and use the shared form fields (16px on phones, so iOS doesn't zoom)."""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order, Payment
from apps.commerce.services import create_order
from apps.contacts.models import Contact
from apps.crm.models import Lead

TEMPLATES = Path(settings.BASE_DIR) / "templates"
AREAS = ["contacts", "crm", "commerce", "chatbot", "support", "verticals", "scheduler"]


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def owner_client(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account, phone="+260971234567", first_name="Ruth"
    )


def _order(account, contact, status=None):
    order = create_order(
        account,
        contact,
        items=[{"name": "Burger", "unit_price": Decimal("75.00"), "quantity": 1}],
    )
    if status:
        order.status = status
        order.save(update_fields=["status"])
    return order


# ---- orders -------------------------------------------------------------------------------


@pytest.mark.django_db
class TestOrders:
    def test_needs_attention_with_nothing_waiting_is_not_the_first_run(
        self, account, contact, owner_client
    ):
        _order(account, contact, Order.Status.PAID)
        html = owner_client.get("/orders/").content.decode()
        assert 'data-orders="none-need-attention"' in html
        assert "Create your first order" not in html

    def test_the_list_pages_and_marks_the_current_view(
        self, account, contact, owner_client
    ):
        for _ in range(3):
            _order(account, contact)
        from apps.commerce import views

        views.ORDERS_PER_PAGE, before = 2, views.ORDERS_PER_PAGE
        try:
            html = owner_client.get("/orders/").content.decode()
        finally:
            views.ORDERS_PER_PAGE = before
        assert 'aria-label="Pagination"' in html
        assert 'class="tab tab-active" aria-current="page">Needs attention' in html

    @pytest.mark.parametrize(
        "status,marker,offers_request",
        [
            (Order.Status.CANCELLED, 'data-payment="cancelled"', False),
            (Order.Status.REFUNDED, 'data-payment="refunded"', False),
        ],
    )
    def test_a_closed_order_offers_nothing_to_collect(
        self, account, contact, owner_client, status, marker, offers_request
    ):
        order = _order(account, contact, status)
        html = owner_client.get(f"/orders/{order.public_id}/").content.decode()
        assert marker in html
        assert ("Request payment" in html) is offers_request

    def test_a_failed_payment_is_not_called_a_link_to_share(
        self, account, contact, owner_client
    ):
        order = _order(account, contact, Order.Status.AWAITING_PAYMENT)
        Payment.objects.create(
            account=account,
            order=order,
            amount=order.total,
            status=Payment.Status.FAILED,
            checkout_url="https://pay.example/old",
        )
        html = owner_client.get(f"/orders/{order.public_id}/").content.decode()
        assert 'data-payment="failed"' in html
        assert "Request payment again" in html
        assert "Payment link created" not in html

    def test_a_pending_link_is_offered_to_share(self, account, contact, owner_client):
        order = _order(account, contact, Order.Status.AWAITING_PAYMENT)
        Payment.objects.create(
            account=account,
            order=order,
            amount=order.total,
            status=Payment.Status.PENDING,
            checkout_url="https://pay.example/abc",
        )
        html = owner_client.get(f"/orders/{order.public_id}/").content.decode()
        assert 'data-payment="link-pending"' in html
        assert 'aria-current="page"' in html  # the breadcrumb

    def test_a_customers_order_count_opens_their_orders(
        self, account, contact, owner_client
    ):
        mine = _order(account, contact, Order.Status.PAID)
        other = Contact.objects.create(account=account, phone="+260979999999")
        theirs = _order(account, other, Order.Status.PAID)
        profile = owner_client.get(f"/contacts/{contact.public_id}/").content.decode()
        assert f"?view=all&amp;contact={contact.public_id}" in profile
        html = owner_client.get(
            f"/orders/?view=all&contact={contact.public_id}"
        ).content.decode()
        assert mine.public_id in html and theirs.public_id not in html
        assert 'data-orders="for-contact"' in html

    def test_another_business_customer_is_not_a_filter(self, account, owner_client):
        stranger = Contact.objects.create(
            account=Account.objects.create(company_name="Other"), phone="+260970000001"
        )
        html = owner_client.get(
            f"/orders/?contact={stranger.public_id}"
        ).content.decode()
        assert 'data-orders="for-contact"' not in html

    def test_awaiting_payment_stands_out(self, account, contact, owner_client):
        order = _order(account, contact, Order.Status.AWAITING_PAYMENT)
        html = owner_client.get(f"/orders/{order.public_id}/").content.decode()
        assert '<span class="badge badge-warning">Awaiting payment</span>' in html


# ---- interested customers ------------------------------------------------------------------


@pytest.mark.django_db
class TestInterestedCustomers:
    def test_the_list_says_it_shows_only_open_leads(
        self, account, contact, owner_client
    ):
        Lead.objects.create(account=account, contact=contact, status=Lead.Status.NEW)
        html = owner_client.get("/sales/").content.decode()
        assert "Still deciding" in html
        assert "Everyone who showed interest" not in html

    def test_the_pipeline_fits_a_phone(self):
        html = (TEMPLATES / "crm" / "sales.html").read_text(encoding="utf-8")
        assert 'class="pipeline-grid"' in html
        assert "grid-template-columns: repeat(" not in html
        css = (Path(settings.BASE_DIR) / "assets" / "app.css").read_text(
            encoding="utf-8"
        )
        assert ".pipeline-grid { @apply grid gap-4 grid-cols-1 sm:grid-cols-2; }" in css

    def test_the_lead_page_names_its_fields_and_presses_the_current_status(
        self, account, contact, owner_client
    ):
        lead = Lead.objects.create(
            account=account, contact=contact, status=Lead.Status.NEW
        )
        html = owner_client.get(f"/sales/leads/{lead.public_id}/").content.decode()
        assert '<label class="label" for="deal-title">' in html
        assert '<label class="label" for="deal-value">' in html
        assert 'aria-pressed="true">New' in html
        assert "btn-primary" not in html.split("Where are they?")[1].split("</form>")[0]


# ---- starter packs, chatbots, support ------------------------------------------------------


def test_starter_packs_ask_before_changing_live_automations():
    html = (TEMPLATES / "verticals" / "templates.html").read_text(encoding="utf-8")
    assert "go live straight away" in html
    assert "changes you made to them are replaced" in html
    assert html.count("data-confirm=") == 2


@pytest.mark.django_db
class TestChatbots:
    def _bot(self, account, domains):
        from apps.chatbot.models import ChatbotConfig

        return ChatbotConfig.objects.create(
            account=account, name="Help bot", allowed_domains=domains
        )

    def test_a_bot_allowed_nowhere_is_not_just_active(self, account, owner_client):
        bot = self._bot(account, [])
        for url in ("/chatbot/", f"/chatbot/{bot.pk}/"):
            html = owner_client.get(url).content.decode()
            assert 'data-bot-status="no-sites"' in html, url

    def test_a_bot_with_a_site_is_active(self, account, owner_client):
        self._bot(account, ["https://mwamba.co"])
        html = owner_client.get("/chatbot/").content.decode()
        assert 'data-bot-status="active"' in html

    def test_saved_changes_are_announced_once(self, account, owner_client):
        bot = self._bot(account, ["https://mwamba.co"])
        html = (TEMPLATES / "chatbot" / "detail.html").read_text(encoding="utf-8")
        assert "for message in messages" not in html
        assert owner_client.get(f"/chatbot/{bot.pk}/").status_code == 200


def test_forms_use_the_shared_fields():
    # A colour picker is a swatch, not a text field, so it keeps its own look.
    hand_styled = re.compile(
        r"<(?:input(?![^>]*type=\"color\")|select|textarea)\b[^>]*class=\"[^\"]*(?:rounded-(?:md|lg) border|focus:ring)",
        re.S,
    )
    for area in AREAS:
        for path in (TEMPLATES / area).glob("*.html"):
            assert not hand_styled.search(path.read_text(encoding="utf-8")), path


def test_icon_only_back_links_are_named():
    for area in AREAS:
        for path in (TEMPLATES / area).glob("*.html"):
            html = path.read_text(encoding="utf-8")
            for m in re.finditer(
                r"<a [^>]*>\s*\{% include \"components/icon.html\" with name=\"arrow-left\"",
                html,
            ):
                assert "aria-label=" in m.group(0), path


def test_support_selects_are_labelled():
    html = (TEMPLATES / "support" / "_ticket_meta.html").read_text(encoding="utf-8")
    for select_id in re.findall(r'<select [^>]*id="([^"]+)"', html):
        assert f'for="{select_id}"' in html, select_id


def test_the_readiness_legend_is_valid_markup():
    html = (TEMPLATES / "contacts" / "list_detail.html").read_text(encoding="utf-8")
    assert "<dl" not in html or "<a" not in html.split("<dl")[1].split("</dl>")[0]
