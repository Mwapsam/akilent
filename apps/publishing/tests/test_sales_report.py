"""Phase 1 (``cs_sales_report``): which posts sold, ranked only on confirmed credits and a
minimum sample, revenue per currency, and what no post is credited with kept apart.
Plus the bio page polish: look, order and tile stats."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order
from apps.commerce.services import create_order
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.attribution import record
from apps.conversations.models import (
    Conversation,
    ConversationAttribution,
    ConversationEntryPoint,
    Message,
)
from apps.core import channels as ch
from apps.crm.models import Lead
from apps.publishing import bio, sales
from apps.publishing.models import (
    BioPage,
    BioTile,
    ContentPost,
    ContentPostVariant,
    LinkClick,
    TrackedLink,
)

pytestmark = pytest.mark.django_db

EXPLICIT = ConversationAttribution.Method.EXPLICIT


@pytest.fixture(autouse=True)
def _channels(settings):
    settings.WHATSAPP_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    ch.invalidate_channel_cache()
    yield
    ch.invalidate_channel_cache()


@pytest.fixture(autouse=True)
def _no_workflows():
    with patch("apps.commerce.services._enroll_workflows"):
        yield


class Shop:
    def __init__(self, name="shop"):
        self.account = Account.objects.create(company_name=name)
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        self.client = Client()
        self.client.force_login(self.user)
        self._n = 0

    def post(self, caption, platform="facebook"):
        post = ContentPost.objects.create(
            account=self.account,
            platforms=[platform],
            media_type="text" if platform == "facebook" else "image",
            caption=caption,
            status=ContentPost.Status.PUBLISHED,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform=platform,
            status=ContentPostVariant.Status.PUBLISHED,
            publication_id=f"{platform}_{post.pk}",
            published_at=timezone.now(),
        )
        return post

    def chat(self, post, *, at=None, tile=None, kind="tracked_link"):
        """A chat started from ``post`` (or from no post when None)."""
        self._n += 1
        contact = Contact.objects.create(
            account=self.account, phone=f"+26097200{self._n:04d}"
        )
        conversation = Conversation.objects.create(
            account=self.account, contact=contact, channel=Conversation.Channel.WHATSAPP
        )
        Message.objects.create(
            account=self.account,
            conversation=conversation,
            direction=Message.Direction.INBOUND,
            body="Hi",
            timestamp=at or timezone.now(),
        )
        if post is not None:
            variant = post.variants.get()
            link = TrackedLink.objects.filter(variant=variant).first()
            entry = ConversationEntryPoint.objects.create(
                account=self.account,
                conversation=conversation,
                kind=kind,
                post=post,
                variant=variant,
                link=link,
                bio_tile=tile,
            )
            if at:
                ConversationEntryPoint.objects.filter(pk=entry.pk).update(matched_at=at)
        return conversation, contact

    def chats(self, post, n, **kw):
        return [self.chat(post, **kw) for _ in range(n)]

    def lead(self, chat):
        conversation, contact = chat
        lead = Lead.objects.create(account=self.account, contact=contact)
        record(lead, conversation, EXPLICIT)
        return lead

    def order(self, chat, amount="10.00", currency="USD", *, paid=True, paid_at=None):
        conversation, contact = chat
        order = create_order(
            self.account,
            contact,
            [{"name": "Sandals", "unit_price": Decimal(amount), "quantity": 1}],
            currency=currency,
            conversation_id=conversation.public_id if conversation else "",
        )
        if paid:
            order.status = Order.Status.PAID
            order.paid_at = paid_at or timezone.now()
            order.save(update_fields=["status", "paid_at"])
        return order

    def report(self, days=30):
        return sales.report(self.account, reporting.last_days(days))


@pytest.fixture
def shop():
    return Shop()


def _row(report, post):
    for row in report["ranked"] + report["too_early"]:
        if row["post"].pk == post.pk:
            return row
    return None


# --- Ranking ---------------------------------------------------------------------------


def test_posts_are_ranked_by_paid_orders_with_a_minimum_sample(shop):
    sandals = shop.post("Sandals")
    hats = shop.post("Hats")
    bags = shop.post("Bags")
    sandal_chats = shop.chats(sandals, 5)
    hat_chats = shop.chats(hats, 8)
    bag_chats = shop.chats(bags, 4)
    for c in sandal_chats[:3]:
        shop.order(c)
    shop.order(hat_chats[0])
    for c in bag_chats:  # sells best, but too few chats to judge
        shop.order(c)

    report = shop.report()

    assert [r["post"] for r in report["ranked"]] == [sandals, hats]
    assert [r["rank"] for r in report["ranked"]] == [1, 2]
    assert report["ranked"][0]["confirmed"]["paid_orders"] == 3
    assert report["ranked"][0]["confirmed"]["chats"] == 5  # exactly the minimum
    assert [r["post"] for r in report["too_early"]] == [bags]
    assert "rank" not in report["too_early"][0]


def test_leads_and_unpaid_orders_break_ties(shop):
    a, b = shop.post("A"), shop.post("B")
    a_chats, b_chats = shop.chats(a, 5), shop.chats(b, 5)
    shop.order(b_chats[0], paid=False)
    shop.lead(a_chats[0])

    report = shop.report()

    assert [r["post"] for r in report["ranked"]] == [b, a]
    assert report["ranked"][0]["confirmed"]["orders"] == 1
    assert report["ranked"][0]["confirmed"]["paid_orders"] == 0
    assert report["ranked"][1]["confirmed"]["leads"] == 1


def test_revenue_is_per_currency_never_summed(shop):
    post = shop.post("Sandals")
    chats = shop.chats(post, 5)
    shop.order(chats[0], "10.00", "USD")
    shop.order(chats[1], "250.00", "ZMW")
    shop.order(chats[2], "5.50", "USD")

    report = shop.report()

    assert report["ranked"][0]["confirmed"]["revenue"] == [
        {"currency": "USD", "total": Decimal("15.50")},
        {"currency": "ZMW", "total": Decimal("250.00")},
    ]
    assert report["credited"] == [
        {"currency": "USD", "total": Decimal("15.50")},
        {"currency": "ZMW", "total": Decimal("250.00")},
    ]


def test_a_guess_is_credited_to_no_post_and_shown_apart(shop):
    post = shop.post("Sandals")
    chat = shop.chat(post)
    conversation, contact = chat
    guessed = shop.order((None, contact), "40.00", "USD")  # no chat named: a guess
    assert guessed.attribution.is_guess
    walk_in = Contact.objects.create(account=shop.account, phone="+260979999999")
    shop.order((None, walk_in), "7.00", "USD")  # no chat at all

    report = shop.report()

    assert _row(report, post)["confirmed"]["paid_orders"] == 0
    assert report["credited"] == []
    n = report["not_credited"]
    assert [(r["currency"], r["total"]) for r in n["guessed_revenue"]] == [
        ("USD", Decimal("40.00"))
    ]
    assert [(r["currency"], r["total"]) for r in n["untracked_revenue"]] == [
        ("USD", Decimal("7.00"))
    ]


def test_only_the_period_counts(shop):
    post = shop.post("Sandals")
    old = timezone.now() - timedelta(days=40)
    shop.chats(post, 5, at=old)
    recent = shop.chats(post, 2)
    shop.order(recent[0], paid_at=old)  # paid before the period: not counted

    row = _row(shop.report(), post)

    assert row["confirmed"]["chats"] == 2
    assert row["confirmed"]["paid_orders"] == 0
    assert _row(shop.report(days=90), post)["confirmed"]["chats"] == 7


def test_another_business_is_never_counted(shop):
    other = Shop("other")
    other_post = other.post("Theirs")
    other.order(other.chat(other_post))

    assert shop.report()["ranked"] == shop.report()["too_early"] == []


def test_no_report_when_the_business_cant_publish(shop, settings):
    settings.FACEBOOK_ENABLED = False
    settings.INSTAGRAM_ENABLED = False
    ch.invalidate_channel_cache()

    assert shop.report() is None


# --- Instagram credit labels -----------------------------------------------------------


def test_unconfirmed_instagram_credits_are_shown_but_never_rank(shop):
    """No bio page: Instagram link chats are "via bio link, post not confirmed"."""
    post = shop.post("Reel", platform="instagram")
    TrackedLink.objects.create(
        account=shop.account, post=post, variant=post.variants.get(), code="IGLINK"
    )
    chats = shop.chats(post, 6)
    shop.order(chats[0], "30.00", "USD")

    report = shop.report()

    assert report["ranked"] == []
    row = report["too_early"][0]
    assert row["confirmed"]["chats"] == 0
    (unconfirmed,) = row["unconfirmed"]
    assert unconfirmed["basis"] == bio.BIO_UNCONFIRMED
    assert unconfirmed["chats"] == 6 and unconfirmed["paid_orders"] == 1
    assert report["credited"] == []
    assert report["unconfirmed"] == [
        {
            "label": bio.LABELS[bio.BIO_UNCONFIRMED],
            "revenue": [{"currency": "USD", "total": Decimal("30.00")}],
        }
    ]


def test_bio_tile_chats_are_confirmed_and_direct_links_are_not(shop):
    post = shop.post("Reel", platform="instagram")
    link = TrackedLink.objects.create(
        account=shop.account, post=post, variant=post.variants.get(), code="IGLINK"
    )
    page = BioPage.objects.create(
        account=shop.account,
        slug="shop",
        title="Shop",
        activated_at=timezone.now() - timedelta(days=1),
    )
    tile = BioTile.objects.create(page=page, link=link, code="TILE01", label="Reel")
    tile_chats = shop.chats(post, 5, tile=tile)
    direct = shop.chats(post, 2)
    shop.order(tile_chats[0])
    shop.order(direct[0])

    report = shop.report()

    (row,) = report["ranked"]
    assert row["confirmed"]["chats"] == 5 and row["confirmed"]["paid_orders"] == 1
    (unconfirmed,) = row["unconfirmed"]
    assert unconfirmed["basis"] == bio.DIRECT_LINK
    assert unconfirmed["chats"] == 2 and unconfirmed["paid_orders"] == 1


def test_comment_replies_are_confirmed(shop):
    post = shop.post("Reel", platform="instagram")
    shop.chats(post, 5, kind=ConversationEntryPoint.Kind.IG_COMMENT)

    assert shop.report()["ranked"][0]["confirmed"]["chats"] == 5


# --- The page --------------------------------------------------------------------------


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({sales.FLAG})


def test_the_page_is_hidden_until_released(shop):
    assert shop.client.get("/publishing/sales/").status_code == 404


def test_the_page_shows_ranked_early_and_not_credited(shop, released):
    ranked = shop.post("Sandals are back")
    early = shop.post("Hats")
    for c in shop.chats(ranked, 5)[:1]:
        shop.order(c, "12.00", "ZMW")
    shop.chats(early, 1)

    resp = shop.client.get(reverse("publishing:sales"))
    html = resp.content.decode()

    assert resp.status_code == 200
    assert "Sandals are back" in html and "Too early to judge" in html
    assert f'data-sales-post="{ranked.pk}"' in html
    assert f'data-sales-post="{early.pk}"' in html
    assert "ZMW 12.00" in html
    assert 'data-sales-total="guessed"' in html
    assert 'href="/publishing/sales/"' in html  # the Sales tab


def test_the_page_says_when_nothing_can_be_tracked(shop, released, settings):
    settings.FACEBOOK_ENABLED = False
    settings.INSTAGRAM_ENABLED = False
    ch.invalidate_channel_cache()

    html = shop.client.get(reverse("publishing:sales")).content.decode()

    assert "data-sales-unavailable" in html


# --- Tiles, trends and funnels ------------------------------------------------------------


def _kpi(report, key):
    return next(k for k in report["kpis"] if k["key"] == key)


def test_the_tiles_compare_with_the_period_before(shop):
    post = shop.post("Sandals")
    shop.chats(post, 5, at=timezone.now() - timedelta(days=40))  # the 30 days before
    shop.chats(post, 99, at=timezone.now() - timedelta(days=70))  # older still: ignored
    recent = shop.chats(post, 2)
    shop.order(recent[0], "10.00", "USD")

    report = shop.report()

    chats = _kpi(report, "chats")
    assert chats["value"] == 2
    assert chats["change"] == {"pct": 60, "up": False, "flat": False}
    assert _kpi(report, "leads")["change"] is None  # nothing either side: no 0%
    assert report["revenue"] == [
        {
            "currency": "USD",
            "total": Decimal("10.00"),
            "change": {"new": True, "up": True},
        }
    ]
    # The row and the ranking still only count this period.
    assert _row(report, post)["confirmed"]["chats"] == 2


def test_a_trend_has_a_point_per_day_and_adds_up_to_the_tile(shop):
    post = shop.post("Sandals")
    for days_ago in (0, 0, 2, 5):
        shop.chat(post, at=timezone.now() - timedelta(days=days_ago, minutes=5))

    trend = _kpi(shop.report(days=7), "chats")["trend"]

    values = [s["value"] for s in trend["slots"]]
    assert len(values) == 7 and sum(values) == 4 and values[-1] == 2
    assert len(trend["points"].split()) == 7
    assert trend["end"] == (100.0, 2.0)  # the busiest point sits at the top


def test_the_report_reads_the_same_whatever_the_number_of_chats(shop):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    post = shop.post(
        "Sandals", platform="instagram"
    )  # Instagram asks about the bio page
    for c in shop.chats(post, 2):
        shop.order(c)
    shop.report()  # warms the channel cache
    with CaptureQueriesContext(connection) as few:
        shop.report()
    for c in shop.chats(post, 8):
        shop.order(c)
    with CaptureQueriesContext(connection) as many:
        shop.report()

    assert len(many) == len(few), [q["sql"][:80] for q in many.captured_queries]


def test_a_post_row_draws_its_funnel(shop, released):
    post = shop.post("Sandals")
    chats = shop.chats(post, 4)
    shop.lead(chats[0])
    shop.lead(chats[1])
    shop.order(chats[0])

    report = shop.report()
    html = shop.client.get(reverse("publishing:sales")).content.decode()

    assert [(s["label"], s["pct"]) for s in _row(report, post)["funnel"]] == [
        ("Chats", 100),
        ("Leads", 50),
        ("Orders", 25),
        ("Paid orders", 25),
    ]
    assert 'class="sf-fill" style="width: 50%"' in html
    assert 'data-kpi="chats"' in html and 'data-kpi="revenue"' in html
    assert "New <span" in html  # nothing in the 30 days before


# --- Bio page polish ---------------------------------------------------------------------


@pytest.fixture
def bio_shop(shop, settings):
    settings.RELEASED_FEATURES = frozenset({bio.FLAG})
    page = BioPage.objects.create(
        account=shop.account, slug="shop", title="Shop", activated_at=timezone.now()
    )
    tiles = []
    for i in range(4):
        post = shop.post(f"Post {i}", platform="instagram")
        link = TrackedLink.objects.create(
            account=shop.account, post=post, variant=post.variants.get(), code=f"L{i}"
        )
        tiles.append(
            BioTile.objects.create(
                page=page, link=link, code=f"T{i}", label=f"Post {i}"
            )
        )
    return shop, page, tiles


def _order(page):
    return [t.label for t in bio.active_tiles(page)]


def test_tiles_move_up_and_down_within_their_group(bio_shop):
    shop, page, tiles = bio_shop
    tiles[0].pinned = True
    tiles[0].save()
    start = _order(page)
    assert start[0] == "Post 0"  # pinned first

    second = BioTile.objects.get(label=start[1])
    assert not bio.move_tile(second, -1)  # can't jump above the pinned tile
    assert bio.move_tile(second, +1)
    assert _order(page) == [start[0], start[2], start[1], start[3]]

    last = BioTile.objects.get(label=start[3])
    assert not bio.move_tile(last, +1)


def test_reorder_through_the_page(bio_shop):
    shop, page, _ = bio_shop
    start = _order(page)
    tile = BioTile.objects.get(label=start[1])

    shop.client.post(
        reverse("publishing:bio-tile-update", args=[tile.pk]), {"action": "up"}
    )

    assert _order(page)[0] == start[1]
    public = Client().get(f"/b/{page.slug}").content.decode()
    assert public.index(start[1]) < public.index(start[0])


def test_tile_stats_count_people_and_chats_in_the_window(bio_shop):
    shop, page, tiles = bio_shop
    tile = tiles[1]
    LinkClick.objects.create(link=tile.link, bio_tile=tile)
    LinkClick.objects.create(link=tile.link, bio_tile=tile)
    LinkClick.objects.create(link=tile.link, bio_tile=tile, is_bot=True)
    old = LinkClick.objects.create(link=tile.link, bio_tile=tile)
    LinkClick.objects.filter(pk=old.pk).update(
        clicked_at=timezone.now() - timedelta(days=45)
    )
    shop.chats(tile.link.post, 2, tile=tile)

    stats = bio.tile_stats(page, timezone.now() - timedelta(days=30))

    assert stats[tile.pk] == {"clicks": 2, "chats": 2}
    assert stats[tiles[0].pk] == {"clicks": 0, "chats": 0}
    html = shop.client.get(reverse("publishing:bio")).content.decode()
    assert "2 clicks · 2 chats in 30 days" in html


def test_text_on_any_colour_meets_aa_contrast():
    greys = [f"#{v:02x}{v:02x}{v:02x}" for v in range(256)]
    hues = [
        f"#{r:02x}{g:02x}{b:02x}"
        for r in range(0, 256, 51)
        for g in range(0, 256, 51)
        for b in range(0, 256, 51)
    ]
    for color in greys + hues + ["#1f7a5c", "#777777"]:
        assert bio.contrast(color, bio.text_on(color)) >= 4.5, color


def _png(size=(10, 10)):
    buf = BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    return SimpleUploadedFile("me.png", buf.getvalue(), content_type="image/png")


def _settings(shop, **extra):
    data = {
        "action": "settings",
        "title": "Shop",
        "max_tiles": 12,
        "archive_after_days": 30,
        "is_active": "on",
    }
    data.update(extra)
    return shop.client.post(reverse("publishing:bio"), data)


def test_look_settings_are_saved_and_shown(bio_shop, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    shop, page, _ = bio_shop

    _settings(
        shop,
        custom_colors="on",
        background_color="#1F7A5C",
        button_color="#ffff00",
        avatar=_png(),
    )

    page.refresh_from_db()
    assert page.background_color == "#1f7a5c" and page.button_color == "#ffff00"
    assert page.avatar_url.endswith(".png")
    html = Client().get(f"/b/{page.slug}").content.decode()
    assert f'src="{page.avatar_url}"' in html
    assert f"background: #1f7a5c; color: {bio.LIGHT_TEXT}" in html
    assert f"background: #ffff00; color: {bio.DARK_TEXT}" in html


def test_unticking_own_colours_goes_back_to_the_sites(bio_shop):
    shop, page, _ = bio_shop
    page.background_color = "#000000"
    page.save()

    _settings(shop, background_color="#123456", button_color="#654321")

    page.refresh_from_db()
    assert page.background_color == page.button_color == ""


def test_a_bad_colour_is_refused(bio_shop):
    shop, page, _ = bio_shop

    resp = _settings(shop, custom_colors="on", background_color="red;}")

    page.refresh_from_db()
    assert page.background_color == ""
    assert resp.status_code == 302


@pytest.mark.parametrize(
    "upload,error",
    [
        (
            SimpleUploadedFile("x.png", b"not a picture", content_type="image/png"),
            "a picture we can read",
        ),
        (
            SimpleUploadedFile("x.gif", b"GIF89a", content_type="image/gif"),
            "JPEG, PNG or WebP",
        ),
    ],
)
def test_a_bad_avatar_is_refused(bio_shop, upload, error):
    shop, page, _ = bio_shop

    resp = shop.client.post(
        reverse("publishing:bio"),
        {
            "action": "settings",
            "title": "Shop",
            "max_tiles": 12,
            "archive_after_days": 30,
            "avatar": upload,
        },
        follow=True,
    )

    page.refresh_from_db()
    assert page.avatar_url == ""
    assert error in resp.content.decode()


def test_an_avatar_over_2_mb_is_refused(bio_shop):
    _, page, _ = bio_shop
    big = SimpleUploadedFile(
        "x.png", b"0" * (bio.AVATAR_MAX_BYTES + 1), content_type="image/png"
    )

    with pytest.raises(bio.BioError, match="under 2 MB"):
        bio.save_avatar(page, big)
