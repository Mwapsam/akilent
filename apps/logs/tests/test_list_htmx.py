"""Both log screens filter and page over HTMX without a full reload.

Same contract as the contacts list: one view, two shapes. A normal navigation
gets the page, an HX-Request gets only the results region, and the plain GET
form still works with HTMX switched off.
"""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email.models import EmailMessage
from apps.logs.models import ApiRequest


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, acc


@pytest.fixture
def two_messages(logged_in):
    _, acc = logged_in
    EmailMessage.objects.create(account=acc, from_email="a@acme.test",
                                to_email="ada@example.com", subject="One")
    EmailMessage.objects.create(account=acc, from_email="a@acme.test",
                                to_email="grace@example.com", subject="Two")
    return acc


# --- Activity log ---------------------------------------------------------

@pytest.mark.django_db
def test_message_log_full_page_has_the_shell(logged_in, two_messages):
    client, _ = logged_in
    body = client.get("/logs/messages/").content.decode()
    assert "<html" in body
    assert 'id="message-results"' in body


@pytest.mark.django_db
def test_message_log_htmx_returns_only_the_results(logged_in, two_messages):
    client, _ = logged_in
    body = client.get("/logs/messages/", HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in body
    assert body.strip().startswith('<div id="message-results"')
    assert "ada@example.com" in body


@pytest.mark.django_db
def test_message_log_filter_narrows_the_fragment(logged_in, two_messages):
    client, _ = logged_in
    body = client.get("/logs/messages/?q=ada", HTTP_HX_REQUEST="true").content.decode()
    assert "ada@example.com" in body
    assert "grace@example.com" not in body


@pytest.mark.django_db
def test_message_log_distinguishes_no_results_from_no_data(logged_in, two_messages):
    client, _ = logged_in
    filtered = client.get("/logs/messages/?q=nobody", HTTP_HX_REQUEST="true").content.decode()
    assert "No messages match those filters" in filtered
    assert "Clear filters" in filtered


@pytest.mark.django_db
def test_message_log_empty_account_keeps_its_own_empty_state(logged_in):
    client, _ = logged_in
    body = client.get("/logs/messages/", HTTP_HX_REQUEST="true").content.decode()
    assert "No messages yet" in body


# --- API request log ------------------------------------------------------

@pytest.mark.django_db
def test_request_log_htmx_returns_only_the_results(logged_in):
    client, acc = logged_in
    ApiRequest.objects.create(account=acc, method="POST", path="/api/v1/messages",
                              status_code=202, latency_ms=12)
    body = client.get("/logs/requests/", HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in body
    assert body.strip().startswith('<div id="request-results"')
    assert "/api/v1/messages" in body


@pytest.mark.django_db
def test_request_log_filter_narrows_the_fragment(logged_in):
    client, acc = logged_in
    ApiRequest.objects.create(account=acc, method="POST", path="/api/v1/messages",
                              status_code=202, latency_ms=12)
    ApiRequest.objects.create(account=acc, method="GET", path="/api/v1/domains",
                              status_code=200, latency_ms=8)
    body = client.get("/logs/requests/?path=domains", HTTP_HX_REQUEST="true").content.decode()
    assert "/api/v1/domains" in body
    assert "/api/v1/messages" not in body


@pytest.mark.django_db
def test_both_logs_still_work_without_htmx(logged_in, two_messages):
    """The hx-* attributes must not have replaced the plain GET contract."""
    client, _ = logged_in
    for url in ("/logs/messages/", "/logs/requests/"):
        page = client.get(url).content.decode()
        assert 'method="get"' in page, url
    body = client.get("/logs/messages/?q=grace").content.decode()
    assert "grace@example.com" in body
    assert "ada@example.com" not in body
