"""The contacts list filters and pages over HTMX without a full reload.

The contract is that the same view serves both shapes: a normal navigation
gets the whole page, an HX-Request gets only the results region. The form is
still a real GET form underneath, so the screen keeps working with HTMX off -
that is what these pin.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    acc = Account.objects.create(
        company_name="Acme",
        selected_services=Account.Services.EMAIL,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture
def contacts(account):
    return [
        Contact.objects.create(account=account, email="ada@example.com", first_name="Ada"),
        Contact.objects.create(account=account, email="grace@example.com", first_name="Grace"),
    ]


@pytest.mark.django_db
def test_full_page_render_includes_the_shell(client, account, contacts):
    client.force_login(account.owner)
    body = client.get("/contacts/").content.decode()
    assert "<html" in body
    assert 'id="contact-results"' in body
    assert "ada@example.com" in body


@pytest.mark.django_db
def test_htmx_request_returns_only_the_results_region(client, account, contacts):
    client.force_login(account.owner)
    body = client.get("/contacts/", HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in body, "the fragment must not carry the page shell"
    assert 'id="contact-results"' in body
    assert "ada@example.com" in body


@pytest.mark.django_db
def test_the_fragment_re_renders_its_own_swap_target(client, account, contacts):
    """hx-swap=outerHTML replaces the element, so the fragment root has to be
    the target itself - otherwise the second filter has nothing to swap."""
    client.force_login(account.owner)
    body = client.get("/contacts/", HTTP_HX_REQUEST="true").content.decode()
    assert body.strip().startswith("<div id=\"contact-results\"")


@pytest.mark.django_db
def test_filtering_narrows_the_fragment(client, account, contacts):
    client.force_login(account.owner)
    body = client.get("/contacts/?q=ada", HTTP_HX_REQUEST="true").content.decode()
    assert "ada@example.com" in body
    assert "grace@example.com" not in body


@pytest.mark.django_db
def test_a_filter_that_matches_nothing_offers_a_way_back(client, account, contacts):
    """An empty *result* is a different state from an empty *account*: the way
    out is to clear the filter, not to go and read the API docs."""
    client.force_login(account.owner)
    body = client.get("/contacts/?q=nobody", HTTP_HX_REQUEST="true").content.decode()
    assert "No contacts match those filters" in body
    assert "Clear filters" in body


@pytest.mark.django_db
def test_empty_account_keeps_the_onboarding_empty_state(client, account):
    client.force_login(account.owner)
    body = client.get("/contacts/").content.decode()
    assert "Your customers will appear here" in body


@pytest.mark.django_db
def test_the_form_still_works_without_htmx(client, account, contacts):
    """No hx-* attribute may replace the plain GET contract."""
    client.force_login(account.owner)
    page = client.get("/contacts/").content.decode()
    assert 'method="get"' in page
    assert 'name="q"' in page
    # and the unenhanced request still filters
    body = client.get("/contacts/?q=grace").content.decode()
    assert "grace@example.com" in body
    assert "ada@example.com" not in body


@pytest.mark.django_db
def test_pagination_is_handed_an_ordered_queryset(client, account):
    """The Paginator must never be handed an unordered queryset.

    Ordering currently comes from Contact.Meta.ordering rather than the view,
    which is easy to delete without noticing: paginating an unordered queryset
    is undefined behaviour and a contact can then appear on two pages or on
    none. Django only raises UnorderedObjectListWarning, and that is emitted
    once per call site, so it is useless as an assertion once anything earlier
    in the run has tripped it. QuerySet.ordered is the direct check.
    """
    for n in range(5):
        Contact.objects.create(account=account, email="c%d@example.com" % n)

    client.force_login(account.owner)
    page = client.get("/contacts/").context["page"]
    assert page.paginator.object_list.ordered, (
        "the Paginator was handed an unordered queryset - row order across "
        "pages is whatever the database feels like"
    )


@pytest.mark.django_db
def test_contacts_are_listed_newest_first(client, account):
    """The order is not just stable, it is the one the table implies."""
    from django.utils import timezone

    made = []
    for n in range(3):
        contact = Contact.objects.create(account=account, email="c%d@example.com" % n)
        Contact.objects.filter(pk=contact.pk).update(
            first_seen=timezone.now() - timedelta(days=n)
        )
        made.append(contact.email)

    client.force_login(account.owner)
    page = client.get("/contacts/").context["page"]
    assert [c.email for c in page] == made, "expected newest first_seen first"
