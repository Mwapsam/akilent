"""The link-in-bio page (Phase 0b): tiles with their own ref codes, the tile lifecycle,
the four Instagram credit labels, the bio-link check, and the release gate."""

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import ConversationEntryPoint
from apps.conversations.services import record_inbound_whatsapp_message
from apps.instagram.models import InstagramBusinessAccount
from apps.publishing import bio
from apps.publishing.links import link_for_variant
from apps.publishing.models import (
    BioPage,
    BioTile,
    ContentPost,
    ContentPostVariant,
    LinkClick,
)
from apps.publishing.results import results_for
from apps.publishing.templatetags.publishing_filters import credit_label
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppBusinessNumber, WhatsAppContact

pytestmark = pytest.mark.django_db


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({bio.FLAG})


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
    return account, client


def _post(account, platforms=("instagram",), caption="New stock in!"):
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


def _ig_link(account, caption="New stock in!"):
    post = _post(account, caption=caption)
    return link_for_variant(post.variants.get(platform="instagram"))


@pytest.fixture
def receive(shop):
    """Deliver an inbound WhatsApp message through the real inbound path."""
    account, _ = shop
    counter = iter(range(1, 1000))

    def _receive(body, phone="+260971234567"):
        contact, _ = Contact.objects.get_or_create(account=account, phone=phone)
        wa_contact, _ = WhatsAppContact.objects.get_or_create(
            account=account, phone_number=phone, defaults={"contact": contact}
        )
        wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
        log = MessageLog.objects.create(
            account=account,
            conversation=wa_conversation,
            contact=wa_contact,
            message_id=f"wamid.{next(counter)}",
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT,
            content=body,
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
        )
        record_inbound_whatsapp_message(
            contact=contact,
            wa_contact=wa_contact,
            whatsapp_conversation=wa_conversation,
            message_log=log,
        )
        return ConversationEntryPoint.objects.filter(
            account=account, conversation__contact=contact
        ).first()

    return _receive


# --- Tiles and their codes -----------------------------------------------------------


def test_a_tile_has_its_own_code_and_opens_the_posts_chat(shop):
    account, client = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    link = _ig_link(account)
    tile = bio.add_tile(page, link)

    assert tile.code != link.code
    resp = Client().get(f"/go/{tile.code.lower()}")

    assert resp.status_code == 302
    assert f"%28ref%20{tile.code}%29" in resp["Location"]
    click = LinkClick.objects.get()
    assert click.link == link and click.bio_tile == tile


def test_the_links_own_code_still_records_no_tile(shop):
    account, _ = shop
    link = _ig_link(account)

    Client().get(f"/go/{link.code}")

    assert LinkClick.objects.get().bio_tile is None


def test_an_archived_tile_still_works_but_a_disabled_link_does_not(shop):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    link = _ig_link(account)
    tile = bio.add_tile(page, link)
    tile.archived_at = timezone.now()
    tile.save()

    assert Client().get(f"/go/{tile.code}").status_code == 302

    link.disabled_at = timezone.now()
    link.save()
    assert Client().get(f"/go/{tile.code}").status_code == 404


def test_a_chat_with_a_tile_code_names_the_tile(shop, receive):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    link = _ig_link(account)
    tile = bio.add_tile(page, link)

    entry = receive(f"Hi! I'd like to order (ref {tile.code})")

    assert entry.post == link.post and entry.link == link
    assert entry.bio_tile == tile
    assert bio.credit_basis(entry) == bio.CONFIRMED


def test_another_workspaces_tile_code_is_ignored(shop, receive):
    other = Account.objects.create(company_name="Other", slug="other")
    WhatsAppBusinessNumber.objects.create(
        account=other,
        phone_number_id="pn2",
        display_number="+260 97 2222222",
        waba_id="waba2",
        access_token="tok",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    page = bio.create_page(other, "other", "Other")
    tile = bio.add_tile(page, _ig_link(other))

    assert receive(f"hello (ref {tile.code})") is None


# --- Tile lifecycle ------------------------------------------------------------------


def test_the_thirteenth_tile_archives_the_oldest_unpinned_one(shop):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    tiles = []
    for i in range(12):
        tile = bio.add_tile(page, _ig_link(account, caption=f"Post {i}"))
        BioTile.objects.filter(pk=tile.pk).update(
            created_at=timezone.now() - timedelta(hours=12 - i)
        )
        tiles.append(tile)
    BioTile.objects.filter(pk=tiles[0].pk).update(pinned=True)

    newest = bio.add_tile(page, _ig_link(account, caption="Post 12"))

    active = set(
        page.tiles.filter(archived_at__isnull=True).values_list("pk", flat=True)
    )
    assert len(active) == 12
    assert tiles[0].pk in active  # pinned, so kept although oldest
    assert tiles[1].pk not in active  # oldest unpinned goes
    assert newest.pk in active


def test_stale_tiles_are_archived_but_pinned_ones_stay(shop):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    old = bio.add_tile(page, _ig_link(account, "Old"))
    pinned = bio.add_tile(page, _ig_link(account, "Pinned"))
    fresh = bio.add_tile(page, _ig_link(account, "Fresh"))
    long_ago = timezone.now() - timedelta(days=31)
    BioTile.objects.filter(pk__in=[old.pk, pinned.pk]).update(created_at=long_ago)
    BioTile.objects.filter(pk=pinned.pk).update(pinned=True)

    assert bio.archive_stale_tiles() == 1

    old.refresh_from_db()
    pinned.refresh_from_db()
    fresh.refresh_from_db()
    assert old.archived_at is not None
    assert pinned.archived_at is None and fresh.archived_at is None


def test_adding_an_archived_post_again_restores_its_tile_and_code(shop):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    link = _ig_link(account)
    tile = bio.add_tile(page, link)
    BioTile.objects.filter(pk=tile.pk).update(archived_at=timezone.now())

    again = bio.add_tile(page, link)

    assert again.pk == tile.pk and again.code == tile.code
    assert again.archived_at is None


def test_a_link_from_another_workspace_cant_be_added(shop):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    other = Account.objects.create(company_name="Other", slug="other")
    WhatsAppBusinessNumber.objects.create(
        account=other,
        phone_number_id="pn2",
        display_number="+260 97 2222222",
        waba_id="waba2",
        access_token="tok",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )

    with pytest.raises(bio.BioError):
        bio.add_tile(page, _ig_link(other))


@pytest.mark.parametrize("slug", ["ab", "Has Caps", "-lead", "trail-", "x" * 41])
def test_bad_addresses_are_refused(shop, slug):
    account, _ = shop
    with pytest.raises(bio.BioError):
        bio.create_page(account, slug, "Title")


# --- The four Instagram credit cases --------------------------------------------------


def test_no_bio_page_labels_instagram_link_credit_unconfirmed(shop, receive):
    account, _ = shop
    link = _ig_link(account)

    entry = receive(f"hi (ref {link.code})")

    assert bio.credit_basis(entry) == bio.BIO_UNCONFIRMED
    assert credit_label(entry) == "via bio link, post not confirmed"


def test_a_chat_from_before_the_page_went_live_is_unconfirmed(shop, receive):
    account, _ = shop
    link = _ig_link(account)
    entry = receive(f"hi (ref {link.code})")
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    assert entry.matched_at < page.activated_at

    assert bio.credit_basis(entry) == bio.BIO_UNCONFIRMED


def test_a_direct_link_after_the_page_went_live_is_labelled_direct(shop, receive):
    account, _ = shop
    bio.create_page(account, "mwamba", "Mwamba Kitchen")
    link = _ig_link(account)

    entry = receive(f"hi (ref {link.code})")

    assert bio.credit_basis(entry) == bio.DIRECT_LINK
    assert credit_label(entry).startswith("direct link: confirmed for the link")


def test_facebook_link_and_comment_credits_stay_confirmed(shop, receive):
    account, _ = shop
    fb = link_for_variant(
        _post(account, platforms=("facebook",)).variants.get(platform="facebook")
    )

    entry = receive(f"hi (ref {fb.code})")

    assert bio.credit_basis(entry) == bio.CONFIRMED
    comment = ConversationEntryPoint(
        account=account, kind=ConversationEntryPoint.Kind.IG_COMMENT
    )
    assert bio.credit_basis(comment) == bio.CONFIRMED
    assert credit_label(None) == ""


def test_results_card_splits_instagram_chats_by_credit(shop, receive):
    account, _ = shop
    link = _ig_link(account)
    receive(f"hi (ref {link.code})", phone="+260970000001")  # before the page
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    tile = bio.add_tile(page, link)
    receive(f"hi (ref {tile.code})", phone="+260970000002")  # through the tile
    receive(f"hi (ref {link.code})", phone="+260970000003")  # direct, after

    row = next(r for r in results_for(link.post) if r["variant"] == link.variant)

    assert row["chats"] == 3
    assert row["credit_breakdown"] == {
        bio.CONFIRMED: 1,
        bio.BIO_UNCONFIRMED: 1,
        bio.DIRECT_LINK: 1,
    }


def test_journey_evidence_shows_the_credit_basis(shop, receive):
    account, _ = shop
    link = _ig_link(account)
    receive(f"hi (ref {link.code})")
    out = StringIO()

    call_command(
        "journey_evidence",
        "--account",
        account.slug,
        "--post",
        str(link.post.pk),
        stdout=out,
    )

    data = json.loads(out.getvalue())
    entries = [e for c in data["conversations"] for e in c["entry_points"]]
    assert entries[0]["credit_basis"] == bio.BIO_UNCONFIRMED
    assert entries[0]["bio_tile_id"] is None


# --- Is the bio link set? ---------------------------------------------------------------


def _ig(account):
    return InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="ig1",
        username="mwamba",
        access_token="t",
        verify_token="v",
    )


@pytest.mark.parametrize(
    "website,expected",
    [
        ("https://akilent.test/b/mwamba", True),
        ("http://www.akilent.test/b/mwamba/", True),
        ("https://akilent.test/go/ABC123", False),
        ("", False),
    ],
)
def test_bio_link_check_reads_the_instagram_website(shop, settings, website, expected):
    settings.SITE_URL = "https://akilent.test"
    account, _ = shop
    _ig(account)
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    assert bio.bio_link_set(page) is None  # not checked yet
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"website": website, "id": "1"}

    with patch("apps.publishing.bio.requests.get", return_value=resp) as get:
        assert bio.check_instagram_website(page)

    assert get.call_args.kwargs["params"]["fields"] == "website"
    assert bio.bio_link_set(page) is expected


def test_a_failed_check_keeps_the_last_reading(shop):
    account, _ = shop
    _ig(account)
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")

    with patch(
        "apps.publishing.bio.requests.get", return_value=MagicMock(status_code=400)
    ):
        assert not bio.check_instagram_website(page)

    assert page.ig_website_checked_at is None


# --- Pages and the release gate --------------------------------------------------------


def test_unreleased_pages_are_not_reachable(shop):
    account, client = shop
    bio.create_page(account, "mwamba", "Mwamba Kitchen")

    assert Client().get("/b/mwamba").status_code == 404
    assert client.get(reverse("publishing:bio")).status_code == 404


def test_released_public_page_lists_active_tiles(shop, released):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    shown = bio.add_tile(page, _ig_link(account, "Fresh bread"))
    hidden = bio.add_tile(page, _ig_link(account, "Old cake"))
    BioTile.objects.filter(pk=hidden.pk).update(archived_at=timezone.now())

    html = Client().get("/b/mwamba").content.decode()

    assert "Fresh bread" in html and f"/go/{shown.code}" in html
    assert "Old cake" not in html


def test_owner_creates_the_page_and_adds_a_post(shop, released):
    account, client = shop
    with patch("apps.publishing.bio.check_instagram_website", return_value=False):
        resp = client.post(
            reverse("publishing:bio"),
            {"action": "create", "slug": "mwamba", "title": "Mwamba Kitchen"},
        )
    assert resp.status_code == 302
    page = BioPage.objects.get(account=account)
    assert page.activated_at is not None
    variant = _post(account).variants.get()

    client.post(reverse("publishing:bio-tile-add", args=[variant.pk]))

    assert page.tiles.get().link.variant == variant


def test_a_member_cant_create_the_page(shop, released):
    account, _ = shop
    user = User.objects.create_user("member", "m@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    client = Client()
    client.force_login(user)

    client.post(
        reverse("publishing:bio"),
        {"action": "create", "slug": "mwamba", "title": "Mwamba Kitchen"},
    )

    assert not BioPage.objects.exists()


def test_a_facebook_channel_cant_be_added(shop, released):
    account, client = shop
    bio.create_page(account, "mwamba", "Mwamba Kitchen")
    variant = _post(account, platforms=("facebook",)).variants.get()

    resp = client.post(reverse("publishing:bio-tile-add", args=[variant.pk]))

    assert resp.status_code == 404
    assert not BioTile.objects.exists()


def test_results_card_offers_add_to_bio_page_only_when_released(shop, settings):
    account, client = shop
    bio.create_page(account, "mwamba", "Mwamba Kitchen")
    post = _post(account)
    link_for_variant(post.variants.get())
    url = reverse("publishing:post-detail", args=[post.pk])

    assert "Add to bio page" not in client.get(url).content.decode()
    settings.RELEASED_FEATURES = frozenset({bio.FLAG})
    assert "Add to bio page" in client.get(url).content.decode()


def test_a_member_cant_add_or_change_tiles(shop, released):
    account, owner = shop
    with patch("apps.publishing.bio.check_instagram_website", return_value=False):
        owner.post(
            reverse("publishing:bio"),
            {"action": "create", "slug": "mwamba", "title": "Mwamba Kitchen"},
        )
    variant = _post(account).variants.get()
    owner.post(reverse("publishing:bio-tile-add", args=[variant.pk]))
    tile = BioPage.objects.get(account=account).tiles.get()

    user = User.objects.create_user("member", "m@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    member = Client()
    member.force_login(user)
    member.post(
        reverse("publishing:bio-tile-update", args=[tile.pk]), {"action": "archive"}
    )
    other = _post(account, caption="Another").variants.get()
    link_for_variant(other)  # the Results card shows bio options with a chat link
    member.post(reverse("publishing:bio-tile-add", args=[other.pk]))

    tile.refresh_from_db()
    assert tile.archived_at is None
    assert BioPage.objects.get(account=account).tiles.count() == 1
    detail = member.get(reverse("publishing:post-detail", args=[other.post_id]))
    assert b"An Owner or Admin can add this post" in detail.content


def test_a_direct_link_while_the_page_was_off_is_unconfirmed(shop, receive):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    link = _ig_link(account)
    bio.set_active(page, False)
    while_off = receive(f"hi (ref {link.code})")
    bio.set_active(page, True)
    after = receive(f"hello again (ref {link.code})", phone="+260970000002")

    page.refresh_from_db()
    assert len(page.off_periods) == 1 and page.off_periods[0][1] is not None
    assert bio.credit_basis(while_off, page) == bio.BIO_UNCONFIRMED
    assert bio.credit_basis(after, page) == bio.DIRECT_LINK


def test_tiles_move_up_and_down(shop, released):
    account, _ = shop
    page = bio.create_page(account, "mwamba", "Mwamba Kitchen")
    first = bio.add_tile(page, _ig_link(account, "One"))
    second = bio.add_tile(page, _ig_link(account, "Two"))
    order = [t.pk for t in bio.active_tiles(page)]

    assert bio.move_tile(BioTile.objects.get(pk=order[1]), -1)

    moved = [t.pk for t in bio.active_tiles(page)]
    assert moved == order[::-1]
    assert {first.pk, second.pk} == set(moved)
