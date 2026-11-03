"""Phase 2a (``cs_scheduling``): the month calendar with drag (and a keyboard Move form)
to reschedule, weekly posting slots, and when a business's posts have done best."""

import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership
from apps.publishing import scheduling
from apps.publishing.models import (
    ContentPlanItem,
    ContentPost,
    ContentPostVariant,
    PostingSlot,
    VariantMetricSnapshot,
)

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status
LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({scheduling.FLAG})


class Shop:
    def __init__(self, name="shop", role=Membership.Role.OWNER, tz="Europe/London"):
        self.account = Account.objects.create(company_name=name)
        BusinessHours.objects.create(account=self.account, timezone=tz)
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(user=self.user, account=self.account, role=role)
        self.client = Client()
        self.client.force_login(self.user)

    def post(
        self,
        when,
        platforms=("instagram", "facebook"),
        status=S.SCHEDULED,
        caption="New stock",
        **fields,
    ):
        post = ContentPost.objects.create(
            account=self.account,
            platforms=list(platforms),
            media_type=fields.pop("media_type", "image"),
            caption=caption,
            status=ContentPost.Status.SCHEDULED
            if status == S.SCHEDULED
            else ContentPost.Status.PUBLISHED,
            scheduled_at=when,
            **fields,
        )
        for p in platforms:
            ContentPostVariant.objects.create(
                post=post,
                platform=p,
                status=status,
                scheduled_at=when,
                published_at=when if status == S.PUBLISHED else None,
            )
        return post


@pytest.fixture
def shop():
    return Shop()


def _local(y, m, d, h=9, minute=0, tz=LONDON):
    return datetime.datetime(y, m, d, h, minute, tzinfo=tz)


def _future(days=10, hour=9):
    day = timezone.localdate() + datetime.timedelta(days=days)
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=LONDON)


# --- Month view --------------------------------------------------------------------------


def test_the_month_view_is_hidden_until_released(shop):
    resp = shop.client.get(reverse("publishing:calendar") + "?view=month")

    html = resp.content.decode()
    assert resp.status_code == 200
    assert ">Month</a>" not in html  # no Month tab
    assert "cal-month" not in html  # the week view instead


def test_the_month_view_shows_whole_weeks_and_marks_what_can_move(shop, released):
    when = _future(days=3)
    movable = shop.post(when, caption="Moves")
    locked = shop.post(when, status=S.PUBLISHED, caption="Already out")
    month = when.strftime("%Y-%m")

    resp = shop.client.get(
        reverse("publishing:calendar") + f"?view=month&month={month}"
    )

    html = resp.content.decode()
    ctx = resp.context
    weeks = ctx["month_weeks"]
    assert weeks[0][0]["date"].weekday() == 0 and weeks[-1][-1]["date"].weekday() == 6
    assert ctx["month_first"] == when.date().replace(day=1)
    assert f'data-cal-entry="{movable.pk}" data-movable="true"' in html
    assert f'data-cal-entry="{locked.pk}" data-movable="false"' in html
    assert f'id="move-{movable.pk}-instagram-facebook"' in html
    assert f'id="move-{locked.pk}-' not in html  # locked posts have no Move form
    assert "Posting slots" in html


def test_the_month_view_shows_planned_ideas(shop, released):
    import datetime

    day = datetime.date(2026, 12, 10)
    item = ContentPlanItem.objects.create(
        account=shop.account, topic="Holiday teaser", planned_date=day
    )
    ContentPlanItem.objects.create(
        account=shop.account, topic="Next year", planned_date=datetime.date(2027, 2, 1)
    )

    resp = shop.client.get(reverse("publishing:calendar") + "?view=month&month=2026-12")

    html = resp.content.decode()
    assert resp.context["month_planned"] == 1
    assert "0 posts · 1 planned" in html
    assert "Holiday teaser" in html and "Next year" not in html
    assert reverse("publishing:plan-edit", args=[item.pk]) in html


def test_month_navigation(shop, released):
    resp = shop.client.get(reverse("publishing:calendar") + "?view=month&month=2026-12")

    assert resp.context["prev_month_param"] == "2026-11"
    assert resp.context["next_month_param"] == "2027-01"
    assert "December 2026" in resp.content.decode()


# --- Moving ------------------------------------------------------------------------------


def _move(shop, post, day, platforms=("instagram", "facebook"), **extra):
    data = {"post": post.pk, "platforms": list(platforms), "date": day.isoformat()}
    data.update(extra)
    return shop.client.post(reverse("publishing:calendar-move"), data)


def test_a_move_keeps_the_local_time_across_a_clock_change(shop, released):
    """09:00 in London stays 09:00 after the clocks go back, though UTC moves."""
    post = shop.post(_local(2099, 10, 20, 9, 30))

    resp = _move(shop, post, datetime.date(2099, 11, 3))

    assert resp.status_code == 302
    for v in post.variants.all():
        assert v.status == S.SCHEDULED
        assert v.scheduled_at.astimezone(LONDON) == _local(2099, 11, 3, 9, 30)
        assert v.dispatched_at is None
    post.refresh_from_db()
    assert post.scheduled_at.astimezone(LONDON) == _local(2099, 11, 3, 9, 30)


def test_a_per_channel_entry_moves_only_its_channel(shop, released):
    post = shop.post(_future())
    _move(shop, post, _future(days=20).date(), platforms=("facebook",))

    ig = post.variants.get(platform="instagram")
    fb = post.variants.get(platform="facebook")
    assert ig.scheduled_at == _future()
    assert fb.scheduled_at == _future(days=20)


@pytest.mark.parametrize(
    "case,error",
    [
        ("published", "gone out can be moved"),
        ("past", "still ahead"),
        ("unapproved", "approved review"),
    ],
)
def test_moves_that_are_refused(shop, released, case, error):
    if case == "published":
        post = shop.post(_future(), status=S.PUBLISHED)
        day = _future(days=12).date()
    elif case == "unapproved":
        post = shop.post(_future(), approval_required=True)
        day = _future(days=12).date()
    else:
        post = shop.post(_future())
        day = timezone.localdate() - datetime.timedelta(days=1)
    before = {v.pk: v.scheduled_at for v in post.variants.all()}

    resp = shop.client.post(
        reverse("publishing:calendar-move"),
        {"post": post.pk, "platforms": ["instagram", "facebook"], "date": day},
        follow=True,
    )

    assert error in resp.content.decode()
    assert {v.pk: v.scheduled_at for v in post.variants.all()} == before


def test_another_workspaces_post_cant_be_moved(shop, released):
    other = Shop("other")
    post = other.post(_future())

    _move(shop, post, _future(days=12).date())

    assert post.variants.first().scheduled_at == _future()


def test_moving_is_hidden_until_released(shop):
    post = shop.post(_future())

    assert _move(shop, post, _future(days=12).date()).status_code == 404


def test_the_move_returns_to_the_calendar_page_only(shop, released):
    post = shop.post(_future())
    month = reverse("publishing:calendar") + "?view=month&month=2099-01"

    ok = _move(shop, post, _future(days=12).date(), next=month)
    evil = _move(shop, post, _future(days=13).date(), next="https://evil.example/")

    assert ok["Location"] == month
    assert evil["Location"] == reverse("publishing:calendar") + "?view=month"


# --- Posting slots -----------------------------------------------------------------------


def test_next_free_slot_skips_the_past_and_taken_slots(shop):
    now = _local(2099, 3, 2, 10)  # a Monday, 10:00
    PostingSlot.objects.create(account=shop.account, weekday=0, time=datetime.time(9))
    PostingSlot.objects.create(account=shop.account, weekday=0, time=datetime.time(18))
    PostingSlot.objects.create(account=shop.account, weekday=2, time=datetime.time(12))
    shop.post(_local(2099, 3, 2, 18), platforms=("instagram",))

    # Monday 09:00 is past, 18:00 is taken on Instagram: Wednesday noon.
    assert scheduling.next_free_slot(shop.account, ["instagram"], after=now) == _local(
        2099, 3, 4, 12
    )
    # Facebook has nothing at 18:00.
    assert scheduling.next_free_slot(shop.account, ["facebook"], after=now) == _local(
        2099, 3, 2, 18
    )
    # A post to both needs both free.
    assert scheduling.next_free_slot(
        shop.account, ["instagram", "facebook"], after=now
    ) == _local(2099, 3, 4, 12)


def test_slots_are_on_the_business_clock_across_a_clock_change(shop):
    PostingSlot.objects.create(account=shop.account, weekday=6, time=datetime.time(9))
    # Saturday before the clocks go forward (Sunday 29 March 2099).
    at = scheduling.next_free_slot(
        shop.account, ["instagram"], after=_local(2099, 3, 28, 12)
    )

    assert at.astimezone(LONDON) == _local(2099, 3, 29, 9)
    assert at.utcoffset() == datetime.timedelta(hours=1)


def test_no_slot_when_none_or_all_taken(shop):
    now = _local(2099, 3, 2, 10)
    assert scheduling.next_free_slot(shop.account, ["instagram"], after=now) is None

    PostingSlot.objects.create(account=shop.account, weekday=1, time=datetime.time(9))
    for week in range(scheduling.SLOT_WEEKS + 1):
        shop.post(
            _local(2099, 3, 3, 9) + datetime.timedelta(weeks=week),
            platforms=("instagram",),
        )

    assert scheduling.next_free_slot(shop.account, ["instagram"], after=now) is None


def test_the_composer_asks_for_the_next_slot(shop, released):
    url = reverse("publishing:slot-next")

    none = shop.client.get(url, {"platforms": ["instagram"]}).json()
    assert none["at"] is None and none["setup"] is True

    PostingSlot.objects.create(account=shop.account, weekday=0, time=datetime.time(9))
    got = shop.client.get(url, {"platforms": ["instagram", "nonsense"]}).json()

    at = datetime.datetime.fromisoformat(got["at"])
    assert at.weekday() == 0 and at.time() == datetime.time(9)


def test_the_composer_offers_next_free_slot_once_released(shop, settings):
    url = reverse("publishing:post-new")
    assert "fillNextSlot" not in shop.client.get(url).content.decode()

    settings.RELEASED_FEATURES = frozenset({scheduling.FLAG})
    resp = shop.client.get(url)

    assert "fillNextSlot" in resp.content.decode()
    assert resp.context["editor_data"]["urls"]["slot_next"] == reverse(
        "publishing:slot-next"
    )


def test_owners_add_and_remove_slots(shop, released):
    url = reverse("publishing:slots")

    shop.client.post(url, {"weekday": ["0", "2"], "time": "09:30"})
    dup = shop.client.post(url, {"weekday": ["0"], "time": "09:30"}, follow=True)

    assert sorted(
        PostingSlot.objects.filter(account=shop.account).values_list("weekday", "time")
    ) == [(0, datetime.time(9, 30)), (2, datetime.time(9, 30))]
    assert "already in your schedule" in dup.content.decode()

    slot = PostingSlot.objects.get(weekday=2)
    shop.client.post(url, {"action": "delete", "slot": slot.pk})
    assert not PostingSlot.objects.filter(pk=slot.pk).exists()


def test_members_cant_change_slots_and_others_slots_are_safe(released):
    member = Shop("member", role=Membership.Role.MEMBER)
    other = Shop("other")
    theirs = PostingSlot.objects.create(
        account=other.account, weekday=0, time=datetime.time(9)
    )
    url = reverse("publishing:slots")

    member.client.post(url, {"weekday": ["1"], "time": "10:00"})
    other_owner = Shop("owner2")
    other_owner.client.post(url, {"action": "delete", "slot": theirs.pk})

    assert not PostingSlot.objects.filter(account=member.account).exists()
    assert PostingSlot.objects.filter(pk=theirs.pk).exists()


def test_slots_page_is_hidden_until_released(shop):
    assert shop.client.get(reverse("publishing:slots")).status_code == 404


# --- Best times --------------------------------------------------------------------------


NOW = _local(2099, 6, 30, 12)


def _published(shop, when, values, platform="instagram", media_type="image"):
    post = shop.post(
        when, platforms=(platform,), status=S.PUBLISHED, media_type=media_type
    )
    variant = post.variants.get()
    VariantMetricSnapshot.objects.create(
        variant=variant, collected_at=when + datetime.timedelta(days=1), values={}
    )
    VariantMetricSnapshot.objects.create(
        variant=variant, collected_at=when + datetime.timedelta(days=2), values=values
    )
    return variant


def _tuesday_morning(week):
    return _local(2099, 6, 2, 10) + datetime.timedelta(weeks=week)  # a Tuesday


def test_nothing_is_suggested_below_the_minimum_sample(shop):
    for week in range(scheduling.BEST_MIN_POSTS - 1):
        _published(shop, _tuesday_morning(-week), {"reach": 100, "interactions": 9})

    best = scheduling.best_times(shop.account, "instagram", now=NOW)

    assert not best["enough"]
    assert best["measured"] == scheduling.BEST_MIN_POSTS - 1
    assert best["top"] == []


def test_the_best_block_is_by_interactions_per_person_reached(shop):
    # Tuesday 09-12: 5 posts at 10%. Friday 18-21: 4 posts at 4%, but more reach.
    for week in range(5):
        _published(shop, _tuesday_morning(-week), {"reach": 100, "interactions": 10})
    friday = _local(2099, 6, 5, 19)
    for week in range(4):
        _published(
            shop,
            friday - datetime.timedelta(weeks=week),
            {"reach": 1000, "interactions": 40},
        )
    # Sunday 06-09: one great post is too few to suggest.
    _published(shop, _local(2099, 6, 7, 7), {"reach": 10, "interactions": 9})
    # Left out: a story, a post too recent to have settled, and one with no reach.
    _published(
        shop,
        _local(2099, 6, 5, 7),
        {"reach": 10, "interactions": 9},
        media_type="story",
    )
    _published(shop, NOW - datetime.timedelta(days=1), {"reach": 10, "interactions": 9})
    _published(shop, _local(2099, 6, 3, 7), {"reach": None, "interactions": 9})

    best = scheduling.best_times(shop.account, "instagram", now=NOW)

    assert best["enough"] and best["measured"] == 10
    assert [(t["day"], t["label"], t["posts"]) for t in best["top"]] == [
        ("Tuesday", "09:00 to 12:00", 5),
        ("Friday", "18:00 to 21:00", 4),
    ]
    assert best["top"][0]["percent"] == 10.0
    assert best["typical"] == 10.0


def test_facebook_counts_reactions_comments_and_shares(shop):
    for n in range(scheduling.BEST_MIN_POSTS):  # two a week, both in the 09-12 block
        _published(
            shop,
            _tuesday_morning(-(n // 2)) + datetime.timedelta(hours=n % 2),
            {"reach": 200, "reactions": 10, "comments": 4, "shares": None},
            platform="facebook",
        )

    best = scheduling.best_times(shop.account, "facebook", now=NOW)

    assert best["top"][0]["percent"] == 7.0


def test_the_slots_page_shows_best_times(shop, released, settings):
    from apps.core import channels as ch

    settings.FACEBOOK_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    ch.invalidate_channel_cache()
    try:
        html = shop.client.get(reverse("publishing:slots")).content.decode()
    finally:
        ch.invalidate_channel_cache()

    assert 'data-best="instagram"' in html
    assert "Not enough posts yet: 0 of 10" in html
