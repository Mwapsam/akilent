"""The deterministic audience classifier: several signals per comment, consuming rules,
spam handling, primary precedence (docs/plans/audience-intelligence-spec.md §3)."""

from __future__ import annotations

import pytest

from apps.audience.classifier import classify


def pairs(text):
    return {(s.category, s.subcategory) for s in classify(text).signals}


def test_one_comment_says_several_things():
    result = classify(
        "I love this dress. Do you have it in size 16, and how much is it?"
    )
    assert pairs(
        "I love this dress. Do you have it in size 16, and how much is it?"
    ) >= {
        ("praise", "product"),
        ("product_enquiry", "size"),
        ("buying_intent", "price"),
        ("buying_intent", "availability"),
    }
    assert result.primary.category == "buying_intent"
    assert result.language == "en"


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Is this available in blue?",
            {("buying_intent", "availability"), ("product_enquiry", "colour")},
        ),
        ("Do you deliver to Leeds?", {("product_enquiry", "delivery")}),
        ("Where are you located?", {("product_enquiry", "location")}),
        ("Back in stock when?", {("product_enquiry", "stock")}),
        ("What fabric is it?", {("product_enquiry", "specs")}),
        ("DM me please", {("buying_intent", "dm_request")}),
        ("How do I order?", {("buying_intent", "ordering")}),
        ("Please make more videos like this", {("content_feedback", "more_content")}),
        (
            "Can you show how it looks when worn?",
            {("content_feedback", "more_content")},
        ),
        ("Got my order today, highly recommend!", {("praise", "satisfaction")}),
        ("Best shop in town", {("praise", "brand")}),
        ("hello", {("general", "greeting")}),
        ("@amy_k", {("general", "chit_chat")}),
        ("random words here", {("general", "other")}),
    ],
)
def test_categories(text, expected):
    assert pairs(text) == expected


def test_love_this_video_is_content_not_product_praise():
    assert pairs("love this video") == {("content_feedback", "appreciation")}


def test_fast_delivery_is_satisfaction_not_a_delivery_question():
    assert pairs("Fast delivery, thank you!") == {("praise", "satisfaction")}


def test_dont_buy_is_a_complaint_not_buying_intent():
    assert pairs("Don't buy from them") == {("complaint", "dissatisfaction")}


def test_where_is_my_order_is_a_delay_complaint_not_ordering():
    assert pairs("Where is my order?? still waiting") == {
        ("complaint", "delivery_delay")
    }


def test_in_order_to_is_not_ordering():
    assert ("buying_intent", "ordering") not in pairs("In order to see it I zoomed in")


def test_too_expensive_is_a_pricing_complaint():
    assert pairs("Way too expensive") == {("complaint", "pricing")}


def test_spam_phrases_consume_their_words():
    # "buy" inside "buy followers" is spam, not a purchase.
    assert pairs("Buy followers cheap") == {("spam_abuse", "spam")}


def test_spam_drops_flattery_but_keeps_commercial_signals():
    assert pairs("Nice pics! DM me for promo 🔥") == {("spam_abuse", "spam")}
    both = pairs("Check my profile. How much is this?")
    assert both == {("spam_abuse", "spam"), ("buying_intent", "price")}
    assert (
        classify("Check my profile. How much is this?").primary.category
        == "buying_intent"
    )


def test_spam_alone_is_primary():
    assert (
        classify("Earn money with crypto https://x.io").primary.category == "spam_abuse"
    )


def test_toxic_does_not_drop_other_signals():
    assert pairs("stupid price, too expensive") >= {
        ("spam_abuse", "toxic"),
        ("complaint", "pricing"),
    }


def test_emoji_praise_has_no_language():
    result = classify("😍😍")
    assert {(s.category, s.subcategory) for s in result.signals} == {
        ("praise", "product")
    }
    assert result.language == ""


def test_greeting_is_dropped_when_something_specific_is_said():
    assert pairs("Hi, how much?") == {("buying_intent", "price")}


def test_curly_apostrophes_are_normalised():
    assert pairs("Don" + chr(0x2019) + "t buy") == {("complaint", "dissatisfaction")}


def test_empty_text_has_no_signals():
    assert classify("").signals == []
    assert classify("   ").signals == []


def test_matched_terms_are_listed_phrases_only():
    terms = {s.category: s.terms for s in classify("How much? price please").signals}
    assert set(terms["buying_intent"]) <= {"how much", "price", "price please"}
