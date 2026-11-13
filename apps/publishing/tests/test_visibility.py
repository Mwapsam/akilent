"""Visibility diagnosis (Content Performance Recovery, Phase 1).

States what was observed, never a cause. Missing numbers are never zero reach. Reach is
compared only with same-age reads of the same kind of post. Restrictions are "unknown".
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing import visibility
from apps.publishing.metrics import State
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    VariantMetricSnapshot,
)

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status
CONNECTED = {"status": "connected"}


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Seen Co")
    user = User.objects.create_user("seen", "seen@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({visibility.FLAG})


def _variant(account, hours_ago=72, platform="instagram", media_type="image", **kw):
    post = ContentPost.objects.create(
        account=account,
        platforms=[platform],
        media_type=media_type,
        status=ContentPost.Status.PUBLISHED,
    )
    return ContentPostVariant.objects.create(
        post=post,
        platform=platform,
        status=kw.pop("status", S.PUBLISHED),
        publication_id="17890",
        published_at=timezone.now() - timedelta(hours=hours_ago),
        **kw,
    )


def _read(variant, hours_after, values, states=None, latest=True):
    states = states or dict.fromkeys(values, State.AVAILABLE)
    at = variant.published_at + timedelta(hours=hours_after)
    VariantMetricSnapshot.objects.create(
        variant=variant, collected_at=at, values=values, states=states
    )
    if latest:
        variant.metrics_latest = {"values": values, "states": states, "error": ""}
        variant.metrics_collected_at = at
        variant.save()


def _stage(variant, **kw):
    return visibility.diagnose(variant, CONNECTED, **kw)["stage"]


# --- stages --------------------------------------------------------------------------


def test_failed_publish_is_not_published(owner):
    variant = _variant(owner[0], status=S.FAILED)
    assert _stage(variant) == "not_published"


def test_post_the_channel_cant_find(owner):
    variant = _variant(owner[0])
    _read(variant, 30, {"reach": None}, {"reach": State.FAILED})
    variant.metrics_latest["post_missing"] = True
    variant.save()
    assert _stage(variant) == "post_missing"


def test_a_failed_read_is_never_zero_reach(owner):
    variant = _variant(owner[0])
    _read(variant, 30, {"reach": None}, {"reach": State.FAILED})
    assert _stage(variant) == "cant_read"


def test_not_reported_and_permission_are_cant_read(owner):
    old = _variant(owner[0])
    _read(old, 60, {"reach": None}, {"reach": State.PENDING})
    locked = _variant(owner[0])
    _read(locked, 30, {"reach": None}, {"reach": State.NEEDS_PERMISSION})
    assert _stage(old) == "cant_read"
    assert _stage(locked) == "cant_read"


def test_no_number_yet_is_too_early(owner):
    variant = _variant(owner[0], hours_ago=5)
    _read(variant, 1, {"reach": None}, {"reach": State.PENDING})
    assert _stage(variant) == "too_early"


def test_zero_before_the_check_window_is_too_early(owner):
    variant = _variant(owner[0], hours_ago=8)
    _read(variant, 6, {"reach": 0})
    assert _stage(variant) == "too_early"


def test_real_zero_after_24_hours_is_zero_reported_reach(owner):
    variant = _variant(owner[0], hours_ago=30)
    _read(variant, 24, {"reach": 0})
    d = visibility.diagnose(variant, CONNECTED)
    assert d["stage"] == "zero_reported_reach"
    assert d["attention"]
    assert d["steps"]


def test_failed_latest_read_uses_the_last_observed_zero(owner):
    variant = _variant(owner[0])
    _read(variant, 24, {"reach": 0}, latest=False)
    _read(variant, 60, {"reach": None}, {"reach": State.FAILED})
    d = visibility.diagnose(variant, CONNECTED)
    assert d["stage"] == "zero_reported_reach"
    assert d["from_earlier"]


def test_reached_states_the_funnel_condition_without_a_cause(owner):
    variant = _variant(owner[0])
    _read(variant, 24, {"reach": 140, "interactions": 0})
    d = visibility.diagnose(variant, CONNECTED)
    assert d["stage"] == "reached"
    assert d["headline"] == "Seen by 140 · no interactions recorded yet"
    assert "Possible factors" in d["possible_factors"]
    assert not d["attention"]


def test_reached_with_tracked_chats(owner):
    variant = _variant(owner[0])
    _read(variant, 24, {"reach": 140, "interactions": 12})
    d = visibility.diagnose(variant, CONNECTED, result={"chats": 3, "orders": 0})
    assert d["headline"] == "Seen by 140 · 12 interactions · 3 chats · no orders yet"


def test_untracked_chats_are_not_claimed_as_zero(owner):
    variant = _variant(owner[0])
    _read(variant, 24, {"reach": 140, "interactions": 12})
    assert "chat" not in visibility.diagnose(variant, CONNECTED)["headline"]


# --- baseline ------------------------------------------------------------------------


def _history(account, n, reach, hours_after=24, media_type="image"):
    for _ in range(n):
        other = _variant(account, hours_ago=24 * 10, media_type=media_type)
        _read(other, hours_after, {"reach": reach})


def test_no_baseline_below_the_minimum(owner):
    account = owner[0]
    _history(account, visibility.BASELINE_MIN_POSTS - 1, 1000)
    variant = _variant(account)
    _read(variant, 24, {"reach": 10})
    d = visibility.diagnose(variant, CONNECTED)
    assert d["baseline"] is None
    assert d["stage"] == "reached"


def test_low_reach_against_same_age_reads(owner):
    account = owner[0]
    _history(account, visibility.BASELINE_MIN_POSTS, 1000)
    variant = _variant(account)
    _read(variant, 24, {"reach": 100})
    d = visibility.diagnose(variant, CONNECTED)
    assert d["stage"] == "low_reach"
    assert d["baseline"] == {
        "median": 1000,
        "posts": visibility.BASELINE_MIN_POSTS,
        "age": "1-day",
        "kind": "feed posts",
    }


def test_a_28_day_read_is_never_compared_with_a_1_day_read(owner):
    account = owner[0]
    _history(account, visibility.BASELINE_MIN_POSTS, 1000, hours_after=24 * 28)
    variant = _variant(account)
    _read(variant, 24, {"reach": 100})
    assert visibility.diagnose(variant, CONNECTED)["baseline"] is None


def test_other_kinds_of_post_are_not_compared(owner):
    account = owner[0]
    _history(account, visibility.BASELINE_MIN_POSTS, 1000, media_type="reel")
    variant = _variant(account)
    _read(variant, 24, {"reach": 100})
    assert visibility.diagnose(variant, CONNECTED)["baseline"] is None


def test_baseline_median_with_mixed_values(owner):
    account = owner[0]
    for reach in (100, 200, 300, 400, 5000):
        other = _variant(account, hours_ago=240)
        _read(other, 30, {"reach": reach})
    variant = _variant(account)
    _read(variant, 30, {"reach": 250})
    assert visibility.diagnose(variant, CONNECTED)["baseline"]["median"] == 300


# --- checks --------------------------------------------------------------------------


def test_restrictions_are_always_unknown_and_checks_are_separate(owner):
    variant = _variant(owner[0], hours_ago=30)
    _read(variant, 24, {"reach": 0})
    checks = {
        c["label"]: c["status"]
        for c in visibility.diagnose(variant, {"status": "reconnect"})["checks"]
    }
    assert checks["Account restrictions"] == "unknown"
    assert checks["Connection"] == "problem"
    assert checks["Numbers readable for this post"] == "ok"


# --- post page -----------------------------------------------------------------------


def test_card_is_hidden_until_released(owner):
    account, client = owner
    variant = _variant(account, hours_ago=30)
    _read(variant, 24, {"reach": 0})
    html = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()
    assert "data-visibility-card" not in html


def test_card_shows_zero_reported_reach_when_released(owner, released):
    account, client = owner
    variant = _variant(account, hours_ago=30)
    _read(variant, 24, {"reach": 0})
    html = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()
    assert "data-visibility-card" in html
    assert 'data-visibility-stage="zero_reported_reach"' in html
    assert "zero reported reach" in html
    assert "Account restrictions" in html


@pytest.mark.parametrize(
    "values,states,hours",
    [
        ({"reach": 0}, None, 30),
        ({"reach": None}, {"reach": State.FAILED}, 30),
        ({"reach": 100}, None, 30),
        ({"reach": None}, {"reach": State.PENDING}, 3),
    ],
)
def test_card_copy_never_claims_how_meta_ranks(owner, released, values, states, hours):
    account, client = owner
    variant = _variant(account, hours_ago=hours)
    _read(variant, min(hours - 1, 24), values, states)
    html = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()
    start = html.index("data-visibility-card")
    card = html[start : html.index("</section>", start)].lower()
    assert "distribut" not in card
    assert "algorithm" not in card
