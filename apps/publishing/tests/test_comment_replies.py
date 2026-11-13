"""A post's own comment auto-reply with its WhatsApp chat link (outreach plan Phase C)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.accounts.models import Account, Membership
from apps.instagram.models.trigger import CommentTrigger
from apps.instagram.services.triggers import first_matching_trigger, reply_links
from apps.publishing import comment_replies
from apps.publishing.models import ContentPost, ContentPostVariant, TrackedLink

FLAG = comment_replies.FLAG


def _post(
    account, platforms=("instagram", "facebook"), caption="Summer dresses are in."
):
    post = ContentPost.objects.create(
        account=account,
        platforms=list(platforms),
        media_type="image",
        caption=caption,
        status=ContentPost.Status.PUBLISHED,
    )
    for p in platforms:
        ContentPostVariant.objects.create(
            post=post,
            platform=p,
            caption=caption,
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id=f"{p}_{post.pk}",
        )
    return post


def _link(variant, code):
    return TrackedLink.objects.create(
        account=variant.post.account,
        post=variant.post,
        variant=variant,
        code=code,
        wa_number="15551234567",
    )


def _rule(account, post=None, keywords="price", reply="Hi {username}! {whatsapp_link}"):
    return CommentTrigger.objects.create(
        account=account,
        post=post,
        name="r",
        match_type=CommentTrigger.MatchType.KEYWORD,
        keywords=keywords,
        reply_template=reply,
    )


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Dress Shop")


@pytest.fixture
def both_channels(monkeypatch):
    monkeypatch.setattr(
        "apps.instagram.rules_views.rule_channels",
        lambda account: ["instagram", "facebook"],
    )


# --- Placeholders --------------------------------------------------------------


def test_placeholders_are_filled_or_dropped_cleanly():
    rule = CommentTrigger(
        reply_template="Hi {username}! Order here: {whatsapp_link} or see {bio_link}"
    )
    filled = rule.render_reply(
        "sam", {"whatsapp_link": "https://x/go/ABC123", "bio_link": "https://x/b/shop"}
    )
    assert filled == "Hi sam! Order here: https://x/go/ABC123 or see https://x/b/shop"
    # No link: the placeholder never reaches the customer.
    bare = rule.render_reply("sam", {})
    assert "{" not in bare and bare.startswith("Hi sam! Order here:")
    assert rule.uses_links()
    assert not CommentTrigger(reply_template="Hi {username}").uses_links()


# --- Which rule answers --------------------------------------------------------


@pytest.mark.django_db
def test_a_posts_own_rule_comes_first_and_only_on_that_post(account):
    post, other = _post(account), _post(account, caption="Shoes")
    general = _rule(account, keywords="price")
    own = _rule(account, post=post, keywords="price")
    on_post = post.variants.get(platform="instagram")
    on_other = other.variants.get(platform="instagram")

    assert first_matching_trigger(account, "instagram", "price?", on_post) == own
    assert first_matching_trigger(account, "instagram", "price?", on_other) == general
    # A comment on something that isn't a Content Studio post: general rules only.
    assert first_matching_trigger(account, "instagram", "price?") == general
    general.delete()
    assert first_matching_trigger(account, "instagram", "price?", on_other) is None


@pytest.mark.django_db
def test_reply_links_use_the_commented_channels_link(account):
    post = _post(account)
    ig, fb = (
        post.variants.get(platform="instagram"),
        post.variants.get(platform="facebook"),
    )
    _link(ig, "IGLNK1")
    _link(fb, "FBLNK1")
    assert reply_links(account, ig)["whatsapp_link"].endswith("/go/IGLNK1")
    assert reply_links(account, fb)["whatsapp_link"].endswith("/go/FBLNK1")
    # Never makes one.
    assert "whatsapp_link" not in reply_links(account, _post(account).variants.first())


# --- Firing (Instagram, end to end) ---------------------------------------------


@pytest.mark.django_db
@patch("apps.instagram.providers.meta.MetaInstagramProvider")
def test_the_dm_carries_the_posts_link(MockProvider):
    from apps.instagram.models.message import OutboundMessage
    from apps.instagram.services.triggers import evaluate_triggers
    from apps.instagram.tests.helpers import (
        make_account,
        make_instagram_account,
        make_instagram_contact,
    )
    from apps.instagram.tests.test_phase3_acceptance import make_thread

    instance = MockProvider.return_value
    instance.can_send.return_value = MagicMock(eligible=True)
    instance.private_reply.return_value = MagicMock(
        success=True, provider_message_id="mid_1", error="", terminal=False
    )
    acc, _ = make_account()
    ig_account = make_instagram_account(acc)
    ig_contact, _ = make_instagram_contact(acc, ig_account)
    post = _post(acc, platforms=("instagram",))
    variant = post.variants.get()
    _link(variant, "DRESS1")
    _rule(acc, post=post, reply="Hi {username}! Chat with us: {whatsapp_link}")

    thread = make_thread(ig_account, ig_contact, "price please")
    thread.content_variant = variant
    thread.save(update_fields=["content_variant"])

    assert evaluate_triggers(thread, "price please")
    body = OutboundMessage.objects.get(instagram_account=ig_account).body
    assert body.endswith("/go/DRESS1") and "{whatsapp_link}" not in body


# --- The post page's card -------------------------------------------------------


@pytest.mark.django_db
def test_save_makes_a_post_rule_on_the_posts_channels(account, both_channels):
    post = _post(account)
    result = comment_replies.save(
        post,
        account,
        {"keywords": "price, how much ,", "reply_template": "Hi {username}"},
    )
    assert not result.errors
    rule = result.rule
    assert rule.post == post and rule.channels == "both"
    assert rule.keywords == "price, how much"
    assert rule.reply_type == CommentTrigger.ReplyType.DM

    ig_only = _post(account, platforms=("instagram",))
    rule2 = comment_replies.save(
        ig_only, account, {"every_comment": "1", "reply_template": "Hi"}
    ).rule
    assert rule2.channels == "instagram"
    assert rule2.match_type == CommentTrigger.MatchType.ANY_COMMENT


@pytest.mark.django_db
def test_save_checks_the_form(account, both_channels):
    post = _post(account)
    errors = comment_replies.save(
        post, account, {"keywords": "", "reply_template": ""}
    ).errors
    assert set(errors) == {"keywords", "reply_template"}
    assert not CommentTrigger.objects.exists()


@pytest.mark.django_db
def test_a_link_reply_without_a_whatsapp_number_says_so(account, both_channels):
    post = _post(account)
    result = comment_replies.save(
        post, account, {"keywords": "price", "reply_template": "Here: {whatsapp_link}"}
    )
    assert result.rule is not None
    assert "Connect a WhatsApp number" in result.note
    assert not TrackedLink.objects.exists()


@pytest.mark.django_db
def test_a_switched_off_link_stays_off(account, both_channels):
    from django.utils import timezone

    post = _post(account, platforms=("instagram",))
    link = _link(post.variants.get(), "OFFLNK")
    link.disabled_at = timezone.now()
    link.save()
    note = comment_replies.ensure_links(post)
    link.refresh_from_db()
    assert link.disabled_at is not None and "switched off" in note


@pytest.fixture
def owner_client(account):
    user = User.objects.create_user("owner-cr", "cr@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_hidden_until_released(account, owner_client, both_channels, settings):
    settings.RELEASED_FEATURES = frozenset()
    post = _post(account)
    page = owner_client.get(f"/publishing/posts/{post.pk}/").content.decode()
    assert "Auto-reply to comments" not in page
    resp = owner_client.post(
        f"/publishing/posts/{post.pk}/comment-replies/",
        {"keywords": "price", "reply_template": "Hi"},
    )
    assert resp.status_code == 404
    assert not CommentTrigger.objects.exists()


@pytest.mark.django_db
def test_the_card_adds_pauses_and_removes(
    account, owner_client, both_channels, settings
):
    settings.RELEASED_FEATURES = frozenset({FLAG})
    post = _post(account)
    base = f"/publishing/posts/{post.pk}/comment-replies/"

    page = owner_client.get(f"/publishing/posts/{post.pk}/").content.decode()
    assert "Auto-reply to comments" in page and "Turn on auto-reply" in page

    owner_client.post(base, {"keywords": "price", "reply_template": "Hi {username}"})
    rule = CommentTrigger.objects.get(post=post)
    assert rule.is_active

    owner_client.post(f"{base}{rule.pk}/toggle/")
    rule.refresh_from_db()
    assert not rule.is_active
    # Editing keeps it paused.
    owner_client.post(
        f"{base}{rule.pk}/", {"keywords": "cost", "reply_template": "Hello"}
    )
    rule.refresh_from_db()
    assert rule.keywords == "cost" and not rule.is_active

    owner_client.post(f"{base}{rule.pk}/delete/")
    assert not CommentTrigger.objects.filter(post=post).exists()


@pytest.mark.django_db
def test_another_businesses_post_or_rule_is_out_of_reach(
    account, owner_client, settings
):
    settings.RELEASED_FEATURES = frozenset({FLAG})
    other = Account.objects.create(company_name="Other")
    theirs = _post(other)
    rule = _rule(other, post=theirs)
    assert (
        owner_client.post(
            f"/publishing/posts/{theirs.pk}/comment-replies/{rule.pk}/delete/"
        ).status_code
        == 404
    )
    assert CommentTrigger.objects.filter(pk=rule.pk).exists()


@pytest.mark.django_db
def test_rules_page_names_the_post(account):
    from apps.instagram.services.rule_text import sentence

    post = _post(account, caption="Summer dresses are in.")
    assert "Summer dresses are in." in sentence(_rule(account, post=post))


# --- Review answers (2026-11-12) ------------------------------------------------


def test_keywords_match_whole_words_ignoring_case():
    from apps.instagram.services.triggers import keyword_in

    assert keyword_in("What's the COST?", "cost")
    assert not keyword_in("Love this costume", "cost")
    assert keyword_in("pp", "pp") and keyword_in("PP please", "pp")
    assert not keyword_in("so happy", "pp")
    assert keyword_in("hm for the blue one", "hm")
    assert keyword_in("How much is it", "how much")
    assert keyword_in("is it available?", "available")
    assert not keyword_in("unavailable", "available")


def test_the_card_suggests_how_buyers_really_ask():
    for word in ("pp", "hm", "dm", "inbox", "available"):
        assert word in comment_replies.DEFAULT_KEYWORDS.split(", ")


@pytest.mark.django_db
def test_general_rules_still_answer_when_the_posts_rule_misses(account):
    post = _post(account)
    _rule(account, post=post, keywords="price")
    general = _rule(account, keywords="delivery")
    on_post = post.variants.get(platform="instagram")
    assert (
        first_matching_trigger(account, "instagram", "delivery to Ndola?", on_post)
        == general
    )


def _ig_setup():
    from apps.instagram.tests.helpers import (
        make_account,
        make_instagram_account,
        make_instagram_contact,
    )

    acc, _ = make_account()
    ig_account = make_instagram_account(acc)
    ig_contact, _ = make_instagram_contact(acc, ig_account)
    return acc, ig_account, ig_contact


def _provider(MockProvider):
    instance = MockProvider.return_value
    instance.can_send.return_value = MagicMock(eligible=True)
    instance.private_reply.return_value = MagicMock(
        success=True, provider_message_id="mid_1", error="", terminal=False
    )
    return instance


@pytest.mark.django_db
@patch("apps.instagram.providers.meta.MetaInstagramProvider")
def test_one_dm_per_person_per_post(MockProvider):
    from apps.instagram.services.triggers import evaluate_triggers
    from apps.instagram.tests.test_phase3_acceptance import make_thread

    instance = _provider(MockProvider)
    acc, ig_account, ig_contact = _ig_setup()
    _rule(acc, keywords="price", reply="Hi {username}")
    first = make_thread(ig_account, ig_contact, "price?")
    second = make_thread(ig_account, ig_contact, "price??")
    second.post_id = first.post_id
    second.save(update_fields=["post_id"])
    elsewhere = make_thread(ig_account, ig_contact, "price on this one?")

    assert evaluate_triggers(first, "price?")
    assert not evaluate_triggers(second, "price??")  # same person, same post
    assert evaluate_triggers(elsewhere, "price on this one?")  # another post
    assert instance.private_reply.call_count == 2


@pytest.mark.django_db
@patch("apps.instagram.providers.meta.MetaInstagramProvider")
def test_a_comment_past_the_window_isnt_sent_and_says_why(MockProvider):
    import datetime

    from django.utils import timezone

    from apps.conversations.comments import (
        CommentItem,
        ThreadItem,
        _attach_auto_reply_errors,
    )
    from apps.instagram.models.message import OutboundMessage
    from apps.instagram.services.triggers import evaluate_triggers
    from apps.instagram.tests.test_phase3_acceptance import make_thread

    instance = _provider(MockProvider)
    acc, ig_account, ig_contact = _ig_setup()
    _rule(acc, keywords="price", reply="Hi {username}")
    thread = make_thread(ig_account, ig_contact, "price?")
    thread.received_at = timezone.now() - datetime.timedelta(days=8)
    thread.save(update_fields=["received_at"])

    evaluate_triggers(thread, "price?")
    instance.private_reply.assert_not_called()
    outbound = OutboundMessage.objects.get(instagram_account=ig_account)
    assert outbound.status == OutboundMessage.Status.FAILED

    root = CommentItem(
        channel="instagram",
        pk=1,
        comment_id=thread.comment_id,
        author="a",
        body="price?",
        timestamp=thread.received_at,
        outbound=False,
        state="",
        hidden=False,
    )
    item = ThreadItem(
        channel="instagram",
        pk=thread.pk,
        post_id=thread.post_id,
        root=root,
        replies=[],
        intent="",
        conversation_public_id="",
        received_at=thread.received_at,
        answered=False,
        flagged=False,
    )
    _attach_auto_reply_errors(acc, [item])
    assert "more than 7 days old" in item.auto_reply_error
    assert not item.auto_reply_error.startswith("Not sent")


@pytest.mark.django_db
def test_short_reply_keywords_arent_flagged_as_matching_inside_words(account):
    from apps.instagram.services.rule_lint import lint_rules

    _rule(account, keywords="pp, dm, hm")
    texts = " ".join(w.message for w in lint_rules(account).warnings)
    assert "inside" not in texts
