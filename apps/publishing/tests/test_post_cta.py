"""A post's call to action: none, chat on WhatsApp (tracked link), or visit a link.

The CTA line goes at the end of each channel's caption: a tappable link on Facebook,
the wording plus "(link in bio)" on Instagram, where caption links can't be tapped.
"""

import re
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.ai.models import AIDraft
from apps.publishing.links import (
    CtaError,
    cta_caption,
    link_for_variant,
    parse_cta,
    short_url,
    whatsapp_url,
)
from apps.publishing.models import ContentPost, ContentPostVariant, TrackedLink
from apps.publishing.services import _publish_variant_facebook
from apps.whatsapp.models import WhatsAppBusinessNumber

pytestmark = pytest.mark.django_db


@pytest.fixture
def shop():
    account = Account.objects.create(company_name="Mwamba Kitchen", slug="mwamba")
    user = User.objects.create_user("owner", "owner@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="pn1",
        display_number="+260 97 1111111",
        waba_id="waba1",
        access_token="tok",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    client = Client()
    client.force_login(user)
    return account, client, user


def _save(client, **fields):
    data = {
        "caption": "Weekend sale",
        "platforms": ["facebook", "instagram"],
        "media_type": "image",
        "action": "draft",
        **fields,
    }
    return client.post(reverse("publishing:post-new"), data)


def _captions(post):
    return {v.platform: v.caption for v in post.variants.all()}


# --- Parsing ---------------------------------------------------------------------


def test_parse_defaults_and_cleans():
    assert parse_cta({}) == {}
    assert parse_cta({"cta_kind": "none", "cta_text": "x"}) == {}
    assert parse_cta({"cta_kind": "whatsapp"}) == {
        "kind": "whatsapp",
        "text": "Chat with us on WhatsApp",
        "prefill": "Hi! I saw your post and I'd like to know more.",
    }
    assert parse_cta(
        {
            "cta_kind": "link",
            "cta_url": "https://shop.example.com/a",
            "cta_text": " Order   now ",
        }
    ) == {"kind": "link", "text": "Order now", "url": "https://shop.example.com/a"}
    # An older form with only the checkbox still means "Chat on WhatsApp".
    assert parse_cta({"wa_link_facebook": "1"})["kind"] == "whatsapp"


@pytest.mark.parametrize(
    "data",
    [
        {"cta_kind": "link"},
        {"cta_kind": "link", "cta_url": "http://shop.example.com"},
        {"cta_kind": "link", "cta_url": "javascript:alert(1)"},
        {"cta_kind": "link", "cta_url": "shop.example.com"},
        {"cta_kind": "whatsapp", "cta_text": "x" * 81},
        {"cta_kind": "whatsapp", "cta_prefill": "x" * 201},
        {"cta_kind": "phone"},
    ],
)
def test_parse_rejects_bad_values(data):
    with pytest.raises(CtaError):
        parse_cta(data)


# --- Composer --------------------------------------------------------------------


def test_whatsapp_cta_with_custom_text_and_first_message(shop):
    account, client, _ = shop
    _save(
        client,
        cta_kind="whatsapp",
        cta_text="Order on WhatsApp",
        cta_prefill="Hi, I want the weekend deal",
        wa_link_facebook="1",
        wa_link_instagram="1",
    )

    post = ContentPost.objects.get(account=account)
    fb = post.variants.get(platform="facebook")
    ig = post.variants.get(platform="instagram")
    assert post.cta["text"] == "Order on WhatsApp"
    assert fb.caption.endswith(f"Order on WhatsApp: {short_url(fb.tracked_link)}")
    assert ig.caption.endswith("Order on WhatsApp (link in bio)")
    assert "/go/" not in ig.caption
    assert fb.tracked_link.prefill_text == "Hi, I want the weekend deal"
    url = whatsapp_url(fb.tracked_link)
    assert unquote(url).endswith(
        f"Hi, I want the weekend deal (ref {fb.tracked_link.code})"
    )


def test_changing_the_text_keeps_one_line_and_none_removes_it(shop):
    account, client, _ = shop
    _save(client, cta_kind="whatsapp", wa_link_facebook="1")
    post = ContentPost.objects.get(account=account)

    _save(
        client,
        post_id=post.pk,
        cta_kind="whatsapp",
        cta_text="Message us",
        wa_link_facebook="1",
    )
    fb = post.variants.get(platform="facebook")
    assert fb.caption.count("/go/") == 1
    assert "Message us:" in fb.caption

    _save(client, post_id=post.pk, cta_kind="none")
    post.refresh_from_db()
    fb.refresh_from_db()
    assert post.cta == {}
    assert "/go/" not in fb.caption
    assert TrackedLink.objects.get(variant=fb).disabled_at is not None


def test_switching_from_whatsapp_to_a_link_turns_the_chat_link_off(shop):
    account, client, _ = shop
    _save(client, cta_kind="whatsapp", wa_link_facebook="1")
    post = ContentPost.objects.get(account=account)

    _save(
        client,
        post_id=post.pk,
        cta_kind="link",
        cta_url="https://shop.example.com/sale",
        wa_link_facebook="1",  # a stale checkbox is ignored
    )

    captions = _captions(post)
    assert captions["facebook"].endswith("Shop now: https://shop.example.com/sale")
    assert captions["instagram"].endswith("Shop now (link in bio)")
    assert "https://" not in captions["instagram"]
    assert not TrackedLink.objects.filter(post=post, disabled_at__isnull=True).exists()


def test_a_bad_link_is_refused_and_nothing_is_saved(shop):
    account, client, _ = shop
    resp = _save(client, cta_kind="link", cta_url="http://shop.example.com")

    assert resp.status_code == 302
    assert not ContentPost.objects.filter(account=account).exists()


def test_without_a_whatsapp_number_the_option_is_disabled(shop):
    account, client, _ = shop
    WhatsAppBusinessNumber.objects.filter(account=account).delete()

    html = client.get(reverse("publishing:post-new")).content.decode()

    assert "Connect a WhatsApp number" in html
    assert re.search(
        r'value="whatsapp" x-model="cta.kind" class="sr-only"\s+disabled', html
    )


def test_a_draft_from_before_ctas_loads_as_whatsapp(shop):
    account, client, _ = shop
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image", caption="Hi"
    )
    variant = ContentPostVariant.objects.create(
        post=post, platform="facebook", caption="Hi"
    )
    link = link_for_variant(variant)

    resp = client.get(reverse("publishing:post-new") + f"?post_id={post.pk}")

    editor = resp.context["editor_data"]
    assert editor["cta"]["kind"] == "whatsapp"
    assert editor["wa_link_urls"] == {"facebook": short_url(link)}
    assert short_url(link) in resp.content.decode()  # shown with a Copy button


def test_editing_the_cta_of_an_approved_post_needs_a_new_review(shop):
    account, client, user = shop
    _save(client, cta_kind="link", cta_url="https://shop.example.com/a")
    post = ContentPost.objects.get(account=account)
    post.approval_required = True
    post.approval_status = "approved"
    post.approved_by = user
    post.approved_at = timezone.now()
    post.last_content_hash = post.compute_content_hash()
    post.save()
    assert post.is_approval_valid()

    post.cta = {**post.cta, "url": "https://shop.example.com/b"}
    assert not post.is_approval_valid()


def test_a_post_without_a_cta_keeps_its_old_hash():
    post = ContentPost(caption="x", platforms=["facebook"], media_type="image")
    before = post.content_hash_for(variants=[])
    post.cta = {}
    assert post.content_hash_for(variants=[]) == before


def test_post_page_shows_the_cta_and_the_short_link(shop):
    account, client, _ = shop
    _save(client, cta_kind="whatsapp", cta_prefill="Hello there", wa_link_instagram="1")
    post = ContentPost.objects.get(account=account)
    link = TrackedLink.objects.get(post=post)

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    assert "Chat on WhatsApp" in html
    assert "First message: Hello there" in html
    assert short_url(link) in html


# --- Publishing ------------------------------------------------------------------


def test_cta_caption_helpers():
    cta = {"kind": "link", "text": "Book now", "url": "https://x.example.com"}
    once = cta_caption("Hi", cta, "facebook")
    assert once == "Hi\n\nBook now: https://x.example.com"
    assert cta_caption(once, cta, "facebook") == once
    assert cta_caption("Hi", cta, "instagram") == "Hi\n\nBook now (link in bio)"
    assert cta_caption("Hi", {}, "facebook") == "Hi"
    # A WhatsApp CTA without that channel's link adds nothing.
    assert cta_caption("Hi", {"kind": "whatsapp", "text": "Chat"}, "instagram") == "Hi"


def _fb_text_post(account, cta):
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type=ContentPost.MediaType.TEXT,
        caption="Hello",
        cta=cta,
    )
    variant = ContentPostVariant.objects.create(
        post=post, platform="facebook", caption="Hello"
    )
    return post, variant


def test_a_facebook_text_post_with_a_link_cta_gets_a_preview_card(shop):
    account, _, _ = shop
    post, variant = _fb_text_post(
        account, {"kind": "link", "text": "Shop now", "url": "https://shop.example.com"}
    )
    provider = MagicMock()
    provider.create_text_post.return_value = "123_456"

    with patch(
        "apps.publishing.services._fb_provider_for_post", return_value=(provider, "123")
    ):
        result = _publish_variant_facebook(variant, post)

    assert result["success"]
    provider.create_text_post.assert_called_once_with(
        "Hello", link="https://shop.example.com"
    )


def test_a_facebook_text_post_without_a_link_cta_sends_no_link(shop):
    account, _, _ = shop
    post, variant = _fb_text_post(account, {})
    provider = MagicMock()
    provider.create_text_post.return_value = "123_456"

    with patch(
        "apps.publishing.services._fb_provider_for_post", return_value=(provider, "123")
    ):
        _publish_variant_facebook(variant, post)

    provider.create_text_post.assert_called_once_with("Hello")


def test_the_provider_sends_link_only_when_given():
    from apps.publishing.providers.facebook import FacebookPublishProvider

    provider = FacebookPublishProvider("page1", "tok")
    response = MagicMock()
    response.json.return_value = {"id": "page1_1"}
    with patch(
        "apps.publishing.providers.facebook.requests.post", return_value=response
    ) as post:
        provider.create_text_post("Hi", link="https://shop.example.com")
        provider.create_text_post("Hi")

    first, second = post.call_args_list
    assert first.kwargs["data"]["link"] == "https://shop.example.com"
    assert "link" not in second.kwargs["data"]


# --- AI brief --------------------------------------------------------------------


def test_the_brief_cta_reaches_the_caption_prompt_and_the_post(shop):
    account, client, _ = shop
    with patch("apps.ai.tasks.build_draft.delay"):
        resp = client.post(
            reverse("publishing:caption-generate"),
            {"prompt": "Summer sale", "goal": "sales", "cta": "Message us to order"},
        )
    draft = AIDraft.objects.get(pk=resp.json()["draft_id"])
    assert draft.context["cta"] == "Message us to order"

    _save(client, brief_cta="Message us to order")
    assert ContentPost.objects.get(account=account).brief_cta == "Message us to order"


def test_the_drafting_prompt_asks_for_the_cta(shop):
    account, _, _ = shop
    from apps.ai import drafting

    draft = AIDraft.objects.create(
        account=account,
        kind=AIDraft.Kind.CONTENT_CAPTION,
        prompt="Summer sale",
        context={"media_type": "image", "platform": "instagram", "cta": "Message us"},
    )
    provider = MagicMock()
    provider.chat.side_effect = RuntimeError("stop after the prompt is built")
    with pytest.raises(RuntimeError):
        drafting.run(draft, provider)

    assert "End each caption by asking readers to: Message us" in str(
        provider.chat.call_args
    )
