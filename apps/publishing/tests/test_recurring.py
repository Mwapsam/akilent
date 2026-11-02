"""Phase 2b (``cs_recurring``): a repeat makes each copy a day ahead through the
composer's own checks; Instagram repeats need a change; failures are skipped and
reported; a review workspace gets each copy for review, never scheduled unseen."""

import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership
from apps.instagram.models import InstagramBusinessAccount
from apps.publishing import recurring
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
    PublishingSettings,
    RecurrenceRule,
    RecurrenceRun,
)

pytestmark = pytest.mark.django_db

LONDON = ZoneInfo("Europe/London")
S = ContentPostVariant.Status
Run = RecurrenceRun.Outcome
IMG = "https://cdn.example.com/a.jpg"


class Shop:
    def __init__(self, name="shop"):
        self.account = Account.objects.create(company_name=name)
        BusinessHours.objects.create(account=self.account, timezone="Europe/London")
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        FacebookPageConnection.objects.create(
            account=self.account,
            page_id=f"p_{name}",
            page_name="Page",
            access_token="t",
        )
        InstagramBusinessAccount.objects.create(
            account=self.account,
            instagram_business_account_id=f"ig_{name}",
            access_token="t",
            is_active=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def post(self, platforms=("facebook",), caption="Sandals are back", **kw):
        post = ContentPost.objects.create(
            account=self.account,
            platforms=list(platforms),
            media_type=kw.pop(
                "media_type", "text" if platforms == ("facebook",) else "image"
            ),
            media_urls=kw.pop(
                "media_urls", [] if platforms == ("facebook",) else [IMG]
            ),
            caption=caption,
            status=ContentPost.Status.PUBLISHED,
            created_by=self.user,
        )
        published = timezone.now() - datetime.timedelta(days=10)
        for p in platforms:
            ContentPostVariant.objects.create(
                post=post,
                platform=p,
                status=S.PUBLISHED,
                published_at=published,
                options={"first_comment": "Order on WhatsApp"}
                if p == "facebook"
                else {},
            )
        return post


@pytest.fixture
def shop():
    return Shop()


def _rule(shop, post, days_ahead=3, **kw):
    first = timezone.now() + datetime.timedelta(days=days_ahead)
    return recurring.create_rule(post, shop.user, kw.pop("every", 7), first, **kw)


def _due(rule):
    """Run the rule as if its copy is due now (a day ahead)."""
    return recurring.run_rule(
        rule.pk, now=rule.next_run_at - datetime.timedelta(hours=12)
    )


# --- Making a rule -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "every,ahead,captions,error",
    [
        (6, 3, (), "7 to 365"),
        (7, 0.5, (), "more than a day"),
        (7, 3, ["x"] * 11, "up to 10"),
    ],
)
def test_rules_that_are_refused(shop, every, ahead, captions, error):
    post = shop.post()
    with pytest.raises(recurring.RecurrenceError, match=error):
        recurring.create_rule(
            post,
            shop.user,
            every,
            timezone.now() + datetime.timedelta(days=ahead),
            captions,
        )


def test_the_first_repeat_is_at_least_a_week_after_the_post(shop):
    post = shop.post()
    ContentPostVariant.objects.filter(post=post).update(published_at=timezone.now())

    with pytest.raises(recurring.RecurrenceError, match="at least 7 days after"):
        _rule(shop, post, days_ahead=3)


# --- Running -----------------------------------------------------------------------------


def test_a_copy_is_scheduled_through_the_composer_and_the_next_run_planned(shop):
    post = shop.post()
    rule = _rule(shop, post)
    run_for = rule.next_run_at

    run = _due(rule)

    assert run.outcome == Run.SCHEDULED
    copy = run.post
    assert copy.pk != post.pk and copy.caption == post.caption
    assert copy.status == ContentPost.Status.SCHEDULED
    fb = copy.variants.get()
    assert fb.status == S.SCHEDULED and fb.scheduled_at == run_for
    assert fb.options == {"first_comment": "Order on WhatsApp"}
    rule.refresh_from_db()
    assert (
        timezone.localtime(rule.next_run_at, LONDON).time()
        == timezone.localtime(run_for, LONDON).time()
    )
    assert (rule.next_run_at - run_for).days in (6, 7)  # 7 local days, DST aside


def test_nothing_runs_before_it_is_due_and_a_run_happens_once(shop):
    rule = _rule(shop, shop.post(), days_ahead=5)

    assert recurring.run_rule(rule.pk) is None  # more than a day away

    run_for = rule.next_run_at
    first = _due(rule)
    RecurrenceRule.objects.filter(pk=rule.pk).update(next_run_at=run_for)
    second = recurring.run_rule(rule.pk, now=run_for - datetime.timedelta(hours=12))

    assert first is not None and second is None
    assert RecurrenceRun.objects.filter(rule=rule).count() == 1
    assert ContentPost.objects.filter(account=shop.account).count() == 2


def test_an_instagram_repeat_with_nothing_changed_is_held_as_a_draft(shop):
    post = shop.post(platforms=("instagram",))
    rule = _rule(shop, post)

    with patch("apps.accounts.notifications.notify_team") as notify:
        runs = recurring.run_due(now=rule.next_run_at - datetime.timedelta(hours=12))

    (run,) = runs
    assert run.outcome == Run.HELD
    assert run.post.status == ContentPost.Status.DRAFT
    assert "Instagram needs a changed caption" in run.reason
    notify.assert_called_once()


def test_alternate_captions_are_used_in_turn(shop):
    post = shop.post(platforms=("instagram",))
    rule = _rule(shop, post, captions=["Still in stock!"])

    first = _due(rule)
    rule.refresh_from_db()
    second = _due(rule)

    assert first.outcome == Run.SCHEDULED and first.post.caption == "Still in stock!"
    # Back to the original: different from the last copy, so it goes out too.
    assert second.outcome == Run.SCHEDULED and second.post.caption == post.caption


def test_a_copy_that_fails_readiness_is_skipped_and_reported(shop):
    post = shop.post(caption="")  # a Facebook text post needs words
    rule = _rule(shop, post)

    with patch("apps.accounts.notifications.notify_team") as notify:
        (run,) = recurring.run_due(now=rule.next_run_at - datetime.timedelta(hours=12))

    assert run.outcome == Run.SKIPPED and run.post is None
    assert run.reason
    assert ContentPost.objects.filter(account=shop.account).count() == 1
    assert "skipped" in notify.call_args.kwargs["subject"]


def test_a_review_workspace_gets_each_copy_for_review(shop):
    PublishingSettings.objects.create(account=shop.account, requires_review=True)
    rule = _rule(shop, shop.post())

    run = _due(rule)

    assert run.outcome == Run.IN_REVIEW
    assert run.post.status == ContentPost.Status.DRAFT
    assert run.post.approval_status == "submitted"
    assert not run.post.variants.filter(status=S.SCHEDULED).exists()


def test_a_run_missed_by_downtime_is_skipped_not_sent_late(shop):
    rule = _rule(shop, shop.post())

    run = recurring.run_rule(
        rule.pk, now=rule.next_run_at + datetime.timedelta(hours=3)
    )

    assert run.outcome == Run.SKIPPED and run.post is None


def test_the_task_does_nothing_until_released(shop):
    from apps.publishing.tasks import run_recurring_posts

    rule = _rule(shop, shop.post(), days_ahead=1.5)

    assert run_recurring_posts.run() == 0
    assert not rule.runs.exists()


# --- Pages -------------------------------------------------------------------------------


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({recurring.FLAG})


def test_repeat_from_the_post_page_and_manage_it(shop, released):
    post = shop.post()
    first = timezone.localtime(
        timezone.now() + datetime.timedelta(days=3), LONDON
    ).strftime("%Y-%m-%dT%H:%M")

    detail = shop.client.get(reverse("publishing:post-detail", args=[post.pk]))
    shop.client.post(
        reverse("publishing:recurring-create", args=[post.pk]),
        {"every_days": 14, "first_run_at": first, "captions": "One\n---\nTwo\nlines"},
    )

    assert "Repeat this post" in detail.content.decode()
    rule = RecurrenceRule.objects.get(source=post)
    assert rule.every_days == 14 and rule.captions == ["One", "Two\nlines"]

    shop.client.post(
        reverse("publishing:recurring-update", args=[rule.pk]), {"action": "pause"}
    )
    rule.refresh_from_db()
    assert not rule.active
    RecurrenceRule.objects.filter(pk=rule.pk).update(
        next_run_at=timezone.now() - datetime.timedelta(days=2)
    )
    shop.client.post(
        reverse("publishing:recurring-update", args=[rule.pk]), {"action": "resume"}
    )
    rule.refresh_from_db()
    assert rule.active and rule.next_run_at > timezone.now() + recurring.LEAD

    html = shop.client.get(reverse("publishing:recurring")).content.decode()
    assert f'data-rule="{rule.pk}"' in html


def test_another_workspaces_repeat_is_out_of_reach(shop, released):
    other = Shop("other")
    rule = _rule(other, other.post())

    resp = shop.client.post(
        reverse("publishing:recurring-update", args=[rule.pk]), {"action": "delete"}
    )

    assert resp.status_code == 404
    assert RecurrenceRule.objects.filter(pk=rule.pk).exists()


def test_recurring_is_hidden_until_released(shop):
    post = shop.post()

    assert shop.client.get(reverse("publishing:recurring")).status_code == 404
    assert (
        "Repeat this post"
        not in shop.client.get(
            reverse("publishing:post-detail", args=[post.pk])
        ).content.decode()
    )
