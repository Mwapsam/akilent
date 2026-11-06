"""The AI's narrow no-break spaces (U+202F) made "the Plumbing category" read as
"thePlumbingcategory" in the inbox. ``proposals.plain_spaces`` makes them plain where a proposal
is checked, so what's shown and what's sent are the same; what was sent before shows plainly."""

from apps.ai import proposals

NARROW_NBSP, NBSP, THIN, ZERO_WIDTH, ZWJ = (
    chr(0x202F),
    chr(0xA0),
    chr(0x2009),
    chr(0x200B),
    chr(0x200D),
)


def _reply(text):
    return proposals.validate(
        {
            "version": 1,
            "action": "reply",
            "intent": "other",
            "confidence": 0.9,
            "payload": {"text": text},
        },
        templates={},
        window_open=True,
    )


def test_odd_spaces_become_plain_and_invisible_ones_go():
    raw = f"Try the{NARROW_NBSP}Plumbing{NBSP}category{THIN}now{ZERO_WIDTH}."
    assert proposals.plain_spaces(raw) == "Try the Plumbing category now."


def test_emoji_joiners_are_kept():
    chef = chr(0x1F468) + ZWJ + chr(0x1F373)
    assert proposals.plain_spaces(f"Our {chef} says hi") == f"Our {chef} says hi"


def test_a_proposed_reply_has_plain_spaces():
    text = f"Try the{NARROW_NBSP}Plumbing{NARROW_NBSP}category."
    assert _reply(text)["payload"]["text"] == "Try the Plumbing category."
