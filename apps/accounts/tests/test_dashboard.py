"""Dashboard shell + its two HTMX fragments.

The dashboard renders in three pieces: the page itself (cheap, and the only
thing on the first byte), a polled work-queue fragment, and a lazily-loaded
panel fragment holding the heavier aggregates. These tests pin that split so a
future change cannot quietly move an expensive query back onto first paint.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import FollowUp


def _make_account(company="Acme"):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    acc = Account.objects.create(
        company_name=company,
        selected_services=Account.Services.EMAIL,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture
def account(db):
    return _make_account()


# --- The page ------------------------------------------------------------

@pytest.mark.django_db
def test_dashboard_renders_work_queue_inline(client, account):
    """The primary answer is server-rendered, not waited on."""
    client.force_login(account.owner)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="work-queue"' in body
    assert "What needs your attention today" in body
    # Counts are present without a second request.
    assert "customer" in body and "waiting" in body


@pytest.mark.django_db
def test_dashboard_defers_the_heavy_panels(client, account):
    """Secondary aggregates load after paint, behind a skeleton."""
    client.force_login(account.owner)
    body = client.get("/dashboard/").content.decode()
    assert 'id="dashboard-panels"' in body
    assert 'hx-get="/dashboard/panels/"' in body
    assert 'hx-trigger="load"' in body
    assert "skeleton-stat" in body
    # The real panel content must NOT be on the first byte.
    assert "Recent activity" not in body
    assert "Upcoming sends" not in body


@pytest.mark.django_db
def test_work_queue_polls_only_while_visible(client, account):
    client.force_login(account.owner)
    body = client.get("/dashboard/").content.decode()
    assert "every 30s" in body
    assert "document.visibilityState" in body


@pytest.mark.django_db
def test_dashboard_without_a_workspace(client, db):
    """A staff-only user with no tenant gets the empty state, not a 500."""
    user = User.objects.create_user("lonely", "l@example.com", "Sup3r-secret-pw")
    client.force_login(user)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert b"No workspace yet" in resp.content


# --- The fragments -------------------------------------------------------

@pytest.mark.django_db
def test_work_queue_fragment_renders_counts(client, account):
    contact = Contact.objects.create(account=account, email="c@example.com")
    FollowUp.objects.create(
        account=account,
        contact=contact,
        due_at=timezone.now() - timedelta(hours=1),  # overdue → counts
    )
    FollowUp.objects.create(
        account=account,
        contact=contact,
        due_at=timezone.now() + timedelta(days=1),  # not yet due → does not
    )
    client.force_login(account.owner)
    resp = client.get("/dashboard/work-queue/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="work-queue"' in body
    assert "follow-up due" in body
    # The swapped-in fragment must carry its own poll attributes, or the
    # first outerHTML swap would silently stop the polling.
    assert "hx-get" in body and "every 30s" in body


@pytest.mark.django_db
def test_panels_fragment_renders(client, account):
    client.force_login(account.owner)
    resp = client.get("/dashboard/panels/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Upcoming sends" in body
    assert "Recent activity" in body
    assert "Setup" in body
    # Loads once — it must not re-trigger itself after the swap.
    assert 'hx-trigger="load"' not in body


@pytest.mark.django_db
def test_scheduled_is_not_duplicated_as_a_stat(client, account):
    """"Scheduled" is answered by the Upcoming sends panel, not also by a stat
    card. The old dashboard showed both, which read as two different numbers
    for the same question."""
    client.force_login(account.owner)
    body = client.get("/dashboard/panels/").content.decode()
    assert body.count('class="stat-card"') == 3
    assert "Upcoming sends" in body
    assert "upcoming sends" not in body.lower().replace("upcoming sends", "", 1)


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/dashboard/work-queue/", "/dashboard/panels/"])
def test_fragments_require_login(client, db, url):
    resp = client.get(url)
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/dashboard/work-queue/", "/dashboard/panels/"])
def test_fragments_reject_a_user_without_a_workspace(client, db, url):
    user = User.objects.create_user("lonely", "l@example.com", "Sup3r-secret-pw")
    client.force_login(user)
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_first_byte_stays_cheap(client, account, django_assert_max_num_queries):
    """The point of the split is that the heavy aggregates are not on the
    critical path. If someone moves _email_stats / _upcoming_sends / the domain
    and subscription lookups back into the page view, this fails.

    This is a ratchet, not a target. The page currently costs ~25 queries, most
    of which are not the dashboard's: onboarding state is computed once by the
    view and again by the onboarding_status context processor, and each pass
    re-reads domains and API keys. That duplication is app-wide, not specific to
    this page, so it is recorded here rather than fixed here. Lower the number
    if you fix it; do not raise it without a reason.
    """
    client.force_login(account.owner)
    with django_assert_max_num_queries(28):
        assert client.get("/dashboard/").status_code == 200


@pytest.mark.django_db
def test_progress_bars_have_accessible_names(client, account):
    """role=progressbar with no name is unreadable to a screen reader."""
    client.force_login(account.owner)
    body = client.get("/dashboard/").content.decode()
    for chunk in body.split('role="progressbar"')[1:]:
        head = chunk[:400]
        assert "aria-labelledby" in head or "aria-label" in head


# --- Swap-target layout parity -------------------------------------------

def _class_attr(html, element_id):
    """The class list of the element carrying `element_id`, as a set."""
    m = re.search(
        r'<div[^>]*\bid="%s"[^>]*>' % re.escape(element_id), html
    )
    assert m, "no element with id=%r in the rendered HTML" % element_id
    cls = re.search(r'\bclass="([^"]*)"', m.group(0))
    return set(cls.group(1).split()) if cls else set()


@pytest.mark.django_db
def test_panels_fragment_keeps_the_skeletons_layout_classes(client, account):
    """An outerHTML swap replaces the element, so the fragment root must carry
    the placeholder's layout classes itself.

    This caught a real bug: the skeleton was `space-y-6 mt-6` but the fragment
    root was only `space-y-6`, so the gap above the panels collapsed to zero
    the moment HTMX swapped them in - correct for one frame, wrong thereafter.
    Spacing regressions like this are invisible to every other test here.
    """
    client.force_login(account.owner)
    page = client.get("/dashboard/").content.decode()
    fragment = client.get("/dashboard/panels/", HTTP_HX_REQUEST="true").content.decode()

    placeholder = _class_attr(page, "dashboard-panels")
    swapped_in = _class_attr(fragment, "dashboard-panels")

    layout = {c for c in placeholder if re.match(r"^(mt|mb|space-y|grid|gap)-", c)}
    missing = layout - swapped_in
    assert not missing, (
        "the panels fragment drops layout classes the skeleton had: %s - "
        "the page will shift when HTMX swaps it in" % sorted(missing)
    )
