"""The post page redesign: a short title, and the "From post to paid order" strip."""

from decimal import Decimal
from types import SimpleNamespace

from apps.publishing.results import funnel_for
from apps.publishing.templatetags.publishing_filters import post_headline


def _post(caption):
    return SimpleNamespace(caption=caption)


def test_title_is_the_first_sentence():
    caption = (
        "Need a plumber or electrician in Lusaka? Find vetted pros in minutes on "
        "TaskCentro. Book, pay via MTN MoMo."
    )
    assert post_headline(_post(caption)) == "Need a plumber or electrician in Lusaka?"


def test_long_title_is_cut_at_a_word_never_mid_word():
    caption = "Launch season with confidence using the marketplace to discover the perfect creative partner"
    title = post_headline(_post(caption))
    assert title.endswith("…")
    assert len(title) <= 71
    assert caption.startswith(title[:-1])
    assert caption[len(title) - 1] == " "


def test_empty_caption_is_untitled():
    assert post_headline(_post("")) == "Untitled post"


def _row(platform, link=True, clicks=0, chats=0, leads=0, orders=0, revenue=()):
    return {
        "variant": SimpleNamespace(platform=platform),
        "link": object() if link else None,
        "clicks": clicks,
        "chats": chats,
        "leads": leads,
        "orders": orders,
        "revenue": list(revenue),
    }


def test_funnel_adds_channels_up_with_rates():
    f = funnel_for(
        [
            _row(
                "facebook",
                clicks=8,
                chats=2,
                leads=1,
                orders=1,
                revenue=[{"currency": "ZMW", "total": Decimal("100")}],
            ),
            _row(
                "instagram",
                clicks=2,
                chats=2,
                leads=1,
                revenue=[{"currency": "ZMW", "total": Decimal("50")}],
            ),
        ]
    )
    values = {s["label"]: s["value"] for s in f["steps"]}
    assert values == {"Link clicks": 10, "Chats": 4, "Leads": 2, "Orders": 1}
    rates = {s["label"]: s.get("rate") for s in f["steps"]}
    assert rates["Chats"] == 40 and rates["Leads"] == 50 and rates["Orders"] == 50
    assert f["revenue"] == [{"currency": "ZMW", "total": Decimal("150")}]
    assert f["by_channel"] is True


def test_clicks_but_no_chats_suggests_the_first_message():
    f = funnel_for([_row("facebook", clicks=9)])
    assert "9 people opened WhatsApp" in f["tip"]["title"]
    assert f["steps"][1]["rate"] == 0


def test_no_link_shows_no_click_count():
    f = funnel_for([_row("instagram", link=False, chats=2)])
    assert f["steps"][0]["value"] is None
    assert f["tip"] is None


def test_nothing_to_show():
    assert funnel_for([]) is None
