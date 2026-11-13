"""Measurement truth (Content Performance Recovery, Phase 0).

Missing numbers are never zero. An old "pending" becomes "not reported", a failed read
doesn't erase what was last observed, and every screen shows the same state.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing import scheduling
from apps.publishing.metrics import (
    State,
    effective_state,
    last_known,
    performance_for,
)
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    VariantMetricSnapshot,
)
from apps.publishing.templatetags.publishing_filters import reach_summary

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Truth Co")
    user = User.objects.create_user("truth", "truth@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


def _published(account, platform="instagram", hours_ago=72):
    post = ContentPost.objects.create(
        account=account,
        platforms=[platform],
        media_type="image",
        status=ContentPost.Status.PUBLISHED,
    )
    return ContentPostVariant.objects.create(
        post=post,
        platform=platform,
        status=S.PUBLISHED,
        publication_id="17890",
        published_at=timezone.now() - timedelta(hours=hours_ago),
    )


def _read(variant, hours_after, values, states, latest=True):
    at = variant.published_at + timedelta(hours=hours_after)
    VariantMetricSnapshot.objects.create(
        variant=variant, collected_at=at, values=values, states=states
    )
    if latest:
        variant.metrics_latest = {"values": values, "states": states, "error": ""}
        variant.metrics_collected_at = at
        variant.save()
    return at


def _row(perf, key):
    return next(r for r in perf["rows"] if r["key"] == key)


# --- effective_state -----------------------------------------------------------------


def test_pending_stays_pending_within_48_hours():
    now = timezone.now()
    assert effective_state(State.PENDING, now - timedelta(hours=40), now) == (
        State.PENDING
    )


def test_pending_after_48_hours_is_not_reported():
    now = timezone.now()
    assert effective_state(State.PENDING, now - timedelta(hours=72), now) == (
        State.NOT_REPORTED
    )


def test_other_states_are_unchanged_and_missing_is_pending():
    now = timezone.now()
    old = now - timedelta(days=10)
    assert effective_state(State.FAILED, old, now) == State.FAILED
    assert effective_state(None, now, now) == State.PENDING


def test_old_post_says_not_reported_instead_of_wait(owner):
    account, _ = owner
    variant = _published(account)
    _read(variant, 60, {"reach": None}, {"reach": State.PENDING})

    row = _row(performance_for(variant), "reach")

    assert row["state"] == State.NOT_REPORTED
    assert "48 hours" not in row["reason"]
    assert "hasn't returned" in row["reason"]


# --- last known value ----------------------------------------------------------------


def test_failed_read_keeps_the_last_observed_number(owner):
    account, _ = owner
    variant = _published(account)
    first = _read(variant, 24, {"reach": 410}, {"reach": State.AVAILABLE})
    _read(variant, 60, {"reach": None}, {"reach": State.FAILED})

    perf = performance_for(variant)
    row = _row(perf, "reach")

    assert row["value"] == 410
    assert row["as_of"] == first
    assert perf["from_earlier"]
    # The latest read itself is left as it was.
    assert variant.metrics_latest["states"]["reach"] == State.FAILED


def test_last_known_ignores_failed_reads_and_unread_values(owner):
    account, _ = owner
    variant = _published(account)
    _read(variant, 1, {"reach": 0}, {"reach": State.AVAILABLE}, latest=False)
    _read(variant, 6, {"reach": None}, {"reach": State.FAILED}, latest=False)

    assert last_known(variant, "reach")[0] == 0  # a real zero is a number
    assert last_known(variant, "saved") == (None, None)


def test_post_page_shows_as_of_for_an_earlier_number(owner):
    account, client = owner
    variant = _published(account)
    _read(variant, 24, {"reach": 410}, {"reach": State.AVAILABLE})
    _read(variant, 60, {"reach": None}, {"reach": State.FAILED})

    html = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()

    assert ">410<" in html
    assert "as of" in html


# --- reach_summary (Library) ---------------------------------------------------------


def test_reach_summary_labels_each_state(owner):
    account, _ = owner
    failed = _published(account)
    _read(failed, 30, {"reach": None}, {"reach": State.FAILED})
    old = _published(account, platform="facebook")
    _read(old, 60, {"reach": None}, {"reach": State.PENDING})

    assert reach_summary(failed.post) == "Reach: Instagram read failed"
    assert reach_summary(old.post) == "Reach: Facebook not reported"


def test_reach_summary_keeps_the_last_number_after_a_failed_read(owner):
    account, _ = owner
    variant = _published(account)
    _read(variant, 24, {"reach": 1204}, {"reach": State.AVAILABLE})
    _read(variant, 60, {"reach": None}, {"reach": State.FAILED})

    assert reach_summary(variant.post).startswith("Reach: Instagram 1,204 (as of ")


def test_library_and_post_page_agree(owner):
    account, client = owner
    variant = _published(account)
    _read(variant, 60, {"reach": None}, {"reach": State.PENDING})

    library = client.get(reverse("publishing:library")).content.decode()
    page = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()

    assert "Instagram not reported" in library
    assert "hasn&#x27;t returned this number" in page or "hasn't returned" in page


# --- best times ----------------------------------------------------------------------


def test_best_times_uses_the_newest_read_that_has_numbers(owner):
    account, _ = owner
    now = timezone.now()
    for week in range(scheduling.BEST_MIN_POSTS):
        post = ContentPost.objects.create(
            account=account,
            platforms=["instagram"],
            media_type="image",
            status=ContentPost.Status.PUBLISHED,
        )
        published = now - timedelta(days=7 * week + 3)
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=S.PUBLISHED,
            publication_id=f"m{week}",
            published_at=published,
        )
        VariantMetricSnapshot.objects.create(
            variant=variant,
            collected_at=published + timedelta(days=1),
            values={"reach": 100, "interactions": 10},
            states={"reach": State.AVAILABLE, "interactions": State.AVAILABLE},
        )
        # The newest read failed.
        VariantMetricSnapshot.objects.create(
            variant=variant,
            collected_at=published + timedelta(days=2),
            values={"reach": None, "interactions": None},
            states={"reach": State.FAILED, "interactions": State.FAILED},
        )

    best = scheduling.best_times(account, "instagram", now=now)

    assert best["measured"] == scheduling.BEST_MIN_POSTS
    assert best["enough"]
