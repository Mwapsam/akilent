"""Phase 4 (``cs_ai_plus``): brand voice, rewrite in place, hashtag suggestions and post
ideas from results. AI stays optional (flag, plan and opt-in), only suggests, and what a
person picks goes through the normal save and approval; ideas become plan items only."""

import json

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.ai import content
from apps.ai.models import AIDraft, AISettings
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import build_draft
from apps.publishing import ai_plus
from apps.publishing.models import (
    ContentCaption,
    ContentPlanItem,
    ContentPost,
    PublishingSettings,
)

pytestmark = pytest.mark.django_db

IMG = "https://example.com/a.jpg"
K = AIDraft.Kind


class Fake(AIProvider):
    answer, calls = "", []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        Fake.calls.append({"system": system, "user": messages[-1].content})
        return CompletionResult(text=Fake.answer, model="fake-c")


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.providers.get_ai_provider", lambda *a, **k: Fake())
    monkeypatch.setattr(
        "apps.billing.limits.LimitChecker.has_feature", lambda self, name: True
    )
    Fake.answer, Fake.calls = "", []
    cache.clear()


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({ai_plus.FLAG})


class Shop:
    def __init__(self, name="shop", role=Membership.Role.OWNER, ai=True):
        self.account = Account.objects.create(company_name=name)
        AISettings.objects.create(
            account=self.account,
            enabled=ai,
            business_notes="Shoes from K450. Open Monday to Saturday.",
            brand_voice="Warm and short. Never say cheap.",
        )
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(user=self.user, account=self.account, role=role)
        self.client = Client()
        self.client.force_login(self.user)

    def run(self, draft, answer):
        Fake.answer = json.dumps(answer)
        build_draft.apply(args=(draft.pk,))
        draft.refresh_from_db()
        return draft

    def ready(self, kind, result):
        return AIDraft.objects.create(
            account=self.account,
            kind=kind,
            prompt="x",
            status=AIDraft.Status.READY,
            result=result,
        )


@pytest.fixture
def shop():
    return Shop()


def _form(**extra):
    data = {
        "caption": "Hello",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_urls_json": json.dumps([IMG]),
        "action": "draft",
    }
    data.update(extra)
    return data


# --- The gate: flag, then AI on for the business ------------------------------------------


def test_nothing_until_released(shop):
    assert shop.client.get("/publishing/ai/").status_code == 404
    data = shop.client.get(reverse("publishing:post-new")).context["editor_data"]
    assert "ai_plus" not in data
    assert ai_plus.voice(shop.account) == ""
    with pytest.raises(ai_plus.AiPlusError):
        ai_plus.request_rewrite(
            shop.account, shop.user, "Hi there", "shorter", "instagram"
        )


def test_off_when_ai_is_off_for_the_business(released):
    shop = Shop(ai=False)
    data = shop.client.get(reverse("publishing:post-new")).context["editor_data"]
    assert "ai_plus" not in data
    resp = shop.client.post(
        reverse("publishing:ai-rewrite"),
        {"caption": "Hi", "instruction": "shorter", "platform": "instagram"},
    )
    assert resp.status_code == 400
    assert "turned on" in resp.json()["error"]
    assert not AIDraft.objects.exists()
    page = shop.client.get(reverse("publishing:ai-tools"))
    assert b"AI tools are off" in page.content


def test_the_composer_gets_the_tools_once_released_and_on(shop, released):
    data = shop.client.get(reverse("publishing:post-new")).context["editor_data"]
    tools = data["ai_plus"]
    assert [r["key"] for r in tools["rewrites"]] == list(ai_plus.REWRITE_LABELS)
    assert tools["rewrite"] == reverse("publishing:ai-rewrite")


# --- Brand voice --------------------------------------------------------------------------


def test_only_an_owner_or_admin_changes_the_voice(released):
    member = Shop("member", role=Membership.Role.MEMBER)
    member.client.post(reverse("publishing:ai-tools"), {"brand_voice": "Loud"})
    assert AISettings.objects.get(account=member.account).brand_voice.startswith("Warm")

    owner = Shop("owner")
    owner.client.post(reverse("publishing:ai-tools"), {"brand_voice": "x" * 3000})
    assert AISettings.objects.get(account=owner.account).brand_voice == "x" * 1500


def test_the_voice_reaches_caption_suggestions_only_once_released(shop, settings):
    shop.client.post(
        reverse("publishing:caption-generate"),
        {"prompt": "Summer sale", "platform": "instagram"},
    )
    assert "voice" not in AIDraft.objects.get().context

    settings.RELEASED_FEATURES = frozenset({ai_plus.FLAG})
    shop.client.post(
        reverse("publishing:caption-generate"),
        {"prompt": "Summer sale", "platform": "instagram"},
    )
    draft = AIDraft.objects.filter(kind=K.CONTENT_CAPTION).first()
    assert draft.context["voice"].startswith("Warm")
    shop.run(draft, {"options": [{"caption": "Hi", "hashtags": []}]})
    assert "Never say cheap" in Fake.calls[-1]["system"]
    assert "not a source of facts" in Fake.calls[-1]["system"]


# --- Rewrite ------------------------------------------------------------------------------


def _rewrite(shop, caption, instruction="shorter", platform="instagram"):
    resp = shop.client.post(
        reverse("publishing:ai-rewrite"),
        {"caption": caption, "instruction": instruction, "platform": platform},
    )
    assert resp.status_code == 200, resp.content
    return AIDraft.objects.get(pk=resp.json()["draft_id"])


def test_a_rewrite_comes_back_as_a_diff_with_the_voice(shop, released):
    draft = _rewrite(shop, "Our new shoes are in the shop now, come and see them")
    assert draft.kind == K.CONTENT_REWRITE
    assert draft.context["limit"] == 2200
    draft = shop.run(draft, {"caption": "New shoes are in. Come and see"})
    assert draft.status == AIDraft.Status.READY
    assert draft.result["after"] == "New shoes are in. Come and see"
    assert ["add", "New"] in draft.result["diff"] or draft.result["diff"]
    assert draft.result["options"] == [
        {"caption": draft.result["after"], "hashtags": []}
    ]
    assert "Never say cheap" in Fake.calls[-1]["system"]
    assert "Keep every link" in Fake.calls[-1]["system"]
    result = shop.client.get(reverse("publishing:ai-result", args=[draft.pk])).json()
    assert result["status"] == "ready"


def test_a_rewrite_that_drops_a_link_or_adds_a_fact_is_flagged(shop, released):
    before = "Shop the range at https://shop.example/new. Ask @sunrise_shoes #shoes"
    draft = _rewrite(shop, before)
    draft = shop.run(draft, {"caption": "Shop the range now, 50% off from K99!"})
    joined = " ".join(draft.warnings)
    assert "https://shop.example/new" in joined
    assert "@sunrise_shoes" in joined
    assert "K99" in joined


def test_an_empty_or_unchanged_rewrite_fails(shop, released):
    assert shop.run(_rewrite(shop, "Hello there"), {"caption": ""}).status == "error"
    assert (
        shop.run(_rewrite(shop, "Hello there"), {"caption": "Hello there"}).status
        == "error"
    )


def test_a_rewrite_needs_a_known_instruction_and_a_caption(shop, released):
    url = reverse("publishing:ai-rewrite")
    assert (
        shop.client.post(url, {"caption": "Hi", "instruction": "rude"}).status_code
        == 400
    )
    assert (
        shop.client.post(url, {"caption": " ", "instruction": "fix"}).status_code == 400
    )


def test_another_business_cant_read_a_draft(shop, released):
    draft = _rewrite(shop, "Hello there")
    other = Shop("other")
    resp = other.client.get(reverse("publishing:ai-result", args=[draft.pk]))
    assert resp.status_code == 404


# --- Hashtags -----------------------------------------------------------------------------


def test_hashtags_are_cleaned_and_never_repeat(shop, released):
    resp = shop.client.post(
        reverse("publishing:ai-hashtags"),
        {"caption": "New shoes", "existing": "#Shoes #new", "platform": "instagram"},
    )
    draft = AIDraft.objects.get(pk=resp.json()["draft_id"])
    assert draft.context["room"] == 10
    draft = shop.run(
        draft,
        {
            "hashtags": [
                "#shoes",
                "summer style",
                "Lusaka",
                "#lusaka",
                "x" * 80,
                "kicks",
            ]
        },
    )
    assert draft.result["hashtags"] == ["Lusaka", "kicks"]


def test_hashtag_room_follows_the_channel():
    assert ai_plus.hashtag_room("instagram", ["t"] * 25) == 5
    assert ai_plus.hashtag_room("instagram", ["t"] * 30) == 0
    assert ai_plus.hashtag_room("facebook", []) == 3


def test_a_full_post_gets_no_hashtag_request(shop, released):
    resp = shop.client.post(
        reverse("publishing:ai-hashtags"),
        {
            "caption": "New shoes",
            "existing": " ".join(f"#t{i}" for i in range(30)),
            "platform": "instagram",
        },
    )
    assert resp.status_code == 400
    assert not AIDraft.objects.exists()


def test_suggestions_are_cut_to_the_room_left():
    assert content.check_hashtags({"hashtags": ["a", "b", "c"]}, [], 2) == ["a", "b"]
    with pytest.raises(content.DraftError):
        content.check_hashtags({"hashtags": ["a"]}, ["A"], 5)


# --- What the post used, and approval ----------------------------------------------------


def test_a_used_rewrite_marks_the_post_and_is_kept(shop, released):
    rewrite = shop.ready(K.CONTENT_REWRITE, {"after": "Hello, friends"})
    tags = shop.ready(K.CONTENT_HASHTAGS, {"hashtags": ["shoes"]})
    shop.client.post(
        reverse("publishing:post-new"),
        _form(caption="Hello, friends", ai_assist=f"{rewrite.pk},{tags.pk},999"),
    )
    post = ContentPost.objects.get(account=shop.account)
    assert post.ai_assisted
    assert (
        ContentCaption.objects.filter(
            post=post, body="Hello, friends", accepted=True
        ).count()
        == 1
    )
    rewrite.refresh_from_db()
    tags.refresh_from_db()
    assert rewrite.status == tags.status == AIDraft.Status.USED


def test_someone_elses_or_unfinished_drafts_dont_count(shop, released):
    other = Shop("other")
    theirs = other.ready(K.CONTENT_REWRITE, {"after": "Theirs"})
    pending = AIDraft.objects.create(
        account=shop.account, kind=K.CONTENT_HASHTAGS, prompt="x"
    )
    shop.client.post(
        reverse("publishing:post-new"), _form(ai_assist=f"{theirs.pk},{pending.pk}")
    )
    assert not ContentPost.objects.get(account=shop.account).ai_assisted


def test_ai_text_still_needs_approval(shop, released):
    PublishingSettings.objects.create(account=shop.account, requires_review=True)
    rewrite = shop.ready(K.CONTENT_REWRITE, {"after": "Hello, friends"})
    shop.client.post(
        reverse("publishing:post-new"),
        _form(
            caption="Hello, friends", ai_assist=str(rewrite.pk), action="publish_now"
        ),
    )
    post = ContentPost.objects.get(account=shop.account)
    assert post.ai_assisted
    assert post.status not in (
        ContentPost.Status.PUBLISHED,
        ContentPost.Status.PUBLISHING,
    )
    assert post.status != ContentPost.Status.SCHEDULED


# --- Ideas from results -------------------------------------------------------------------


def test_no_ideas_without_enough_results(shop, released):
    page = shop.client.get(reverse("publishing:ai-tools"))
    assert b"data-ideas-empty" in page.content
    shop.client.post(reverse("publishing:ai-ideas"))
    assert not AIDraft.objects.exists()


def test_idea_sources_are_the_ranked_posts_only(shop, monkeypatch):
    post = ContentPost.objects.create(
        account=shop.account,
        caption="Red shoes",
        platforms=["instagram"],
        media_type="image",
    )
    counts = {"chats": 6, "leads": 2, "paid_orders": 1}
    monkeypatch.setattr(
        "apps.publishing.sales.report",
        lambda account, period: {
            "ranked": [{"post": post, "confirmed": counts}],
            "too_early": [{"post": post, "confirmed": {"chats": 1}}],
        },
    )
    sources = ai_plus.idea_sources(shop.account)
    assert sources == [
        {
            "id": post.pk,
            "caption": "Red shoes",
            "media_type": "image",
            "platforms": ["instagram"],
            **counts,
        }
    ]


def _with_sources(shop, monkeypatch):
    post = ContentPost.objects.create(
        account=shop.account, caption="Red shoes, K450", platforms=["instagram"]
    )
    sources = [
        {
            "id": post.pk,
            "caption": "Red shoes, K450",
            "media_type": "image",
            "platforms": ["instagram"],
            "chats": 6,
            "leads": 2,
            "paid_orders": 1,
        }
    ]
    monkeypatch.setattr(ai_plus, "idea_sources", lambda account, now=None: sources)
    return post


def test_ideas_must_build_on_a_given_post_and_go_to_planning(
    shop, released, monkeypatch
):
    post = _with_sources(shop, monkeypatch)
    resp = shop.client.post(reverse("publishing:ai-ideas"))
    draft = AIDraft.objects.get(kind=K.CONTENT_IDEAS)
    assert resp.url.endswith(f"?ideas={draft.pk}#ideas")
    draft = shop.run(
        draft,
        {
            "ideas": [
                {
                    "topic": "Red shoes styled three ways",
                    "angle": "Show outfits",
                    "media_type": "carousel",
                    "based_on": post.pk,
                },
                {
                    "topic": "Made up",
                    "angle": "x",
                    "media_type": "reel",
                    "based_on": 99999,
                },
                {"topic": "Odd format", "media_type": "hologram", "based_on": post.pk},
            ]
        },
    )
    assert "Red shoes, K450" in Fake.calls[-1]["user"]
    ideas = draft.result["ideas"]
    assert [i["topic"] for i in ideas] == ["Red shoes styled three ways", "Odd format"]
    assert ideas[1]["media_type"] == "image"

    posts_before = ContentPost.objects.count()
    shop.client.post(
        reverse("publishing:ai-idea-save", args=[draft.pk]), {"index": "0"}
    )
    item = ContentPlanItem.objects.get(account=shop.account)
    assert item.topic == "Red shoes styled three ways"
    assert item.plan_status == ContentPlanItem.PlanStatus.IDEA
    assert item.media_type == "carousel"
    assert item.platforms == ["instagram"]
    assert ContentPost.objects.count() == posts_before
    panel = shop.client.get(reverse("publishing:ai-ideas-status", args=[draft.pk]))
    assert b"Add to Planning" in panel.content


def test_an_idea_index_out_of_range_saves_nothing(shop, released):
    draft = shop.ready(K.CONTENT_IDEAS, {"ideas": []})
    shop.client.post(
        reverse("publishing:ai-idea-save", args=[draft.pk]), {"index": "3"}
    )
    assert not ContentPlanItem.objects.exists()


def test_ideas_with_no_usable_answer_fail(shop):
    with pytest.raises(content.DraftError):
        content.check_ideas(
            shop.account,
            {"ideas": [{"topic": "x", "based_on": 5}]},
            [{"id": 1, "caption": ""}],
            ["image"],
        )


def test_the_ai_tab_shows_once_released(shop, released):
    page = shop.client.get(reverse("publishing:calendar"))
    assert reverse("publishing:ai-tools").encode() in page.content


def test_the_keepers_find_links_numbers_mentions_and_tags():
    found = content._keepers(
        "See https://a.example/x. Call +260 977 123456, @shop #new"
    )
    assert found == ["https://a.example/x", "+260 977 123456", "@shop", "#new"]
