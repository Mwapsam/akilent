"""apps.conversations.flow_json: the pure, deterministic WhatsApp Flow JSON builder."""

import pytest

from apps.conversations.flow_json import FlowJSONError, build, content_hash

QUESTIONS = [
    {
        "key": "name",
        "label": "What's your name?",
        "field_type": "text",
        "maps_to": "contact.first_name",
    },
    {
        "key": "email",
        "label": "What's your email?",
        "field_type": "email",
        "maps_to": "",
    },
]


def test_build_raises_for_no_questions():
    with pytest.raises(FlowJSONError):
        build([])


def test_build_produces_one_text_input_per_question():
    flow = build(QUESTIONS)
    screen = flow["screens"][0]
    form = screen["layout"]["children"][0]
    inputs = [c for c in form["children"] if c["type"] == "TextInput"]
    assert [i["name"] for i in inputs] == ["name", "email"]
    assert [i["input-type"] for i in inputs] == ["text", "email"]


@pytest.mark.parametrize(
    "field_type,expected",
    [("text", "text"), ("email", "email"), ("phone", "phone"), ("number", "number")],
)
def test_input_type_mapping(field_type, expected):
    flow = build([{"key": "q", "label": "Q?", "field_type": field_type, "maps_to": ""}])
    component = flow["screens"][0]["layout"]["children"][0]["children"][0]
    assert component["input-type"] == expected


def test_an_unknown_field_type_falls_back_to_text():
    flow = build([{"key": "q", "label": "Q?", "field_type": "bogus", "maps_to": ""}])
    component = flow["screens"][0]["layout"]["children"][0]["children"][0]
    assert component["input-type"] == "text"


def test_the_footer_submits_every_question_key():
    flow = build(QUESTIONS)
    footer = flow["screens"][0]["layout"]["children"][0]["children"][-1]
    assert footer["type"] == "Footer"
    assert set(footer["on-click-action"]["payload"]) == {"name", "email"}


def test_content_hash_is_stable_for_identical_input():
    assert content_hash(build(QUESTIONS)) == content_hash(build(list(QUESTIONS)))


def test_content_hash_changes_when_a_label_changes():
    changed = [{**QUESTIONS[0], "label": "Your full name?"}, QUESTIONS[1]]
    assert content_hash(build(QUESTIONS)) != content_hash(build(changed))


def test_content_hash_changes_when_a_field_type_changes():
    changed = [{**QUESTIONS[0], "field_type": "phone"}, QUESTIONS[1]]
    assert content_hash(build(QUESTIONS)) != content_hash(build(changed))


def test_content_hash_is_unaffected_by_maps_to():
    """maps_to only matters once Akilent applies a completed answer -- it's not
    part of what the customer sees, so it shouldn't force a pointless republish."""
    changed = [{**QUESTIONS[0], "maps_to": "contact.attributes.name"}, QUESTIONS[1]]
    assert content_hash(build(QUESTIONS)) == content_hash(build(changed))
