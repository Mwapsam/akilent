"""The block editor: sections in, email-safe HTML out, built on the server every time."""

import json

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email import blocks
from apps.email.models import EmailTemplate, EmailTemplateVersion


def doc(*items, **extra):
    return {"format": blocks.FORMAT, "blocks": list(items), **extra}


# ---- the format ------------------------------------------------------------------------------


def test_text_is_escaped_blanks_survive_and_bold_works():
    html, text = blocks.render(
        doc(
            {"type": "text", "text": "Hi {{ first_name }}, <b>x</b> **thanks**\nLine 2"}
        )
    )
    assert "{{ first_name }}" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x" not in html
    assert "<strong>thanks</strong>" in html and "<br>" in html
    assert "Hi {{ first_name }}, <b>x</b> thanks" in text


def test_template_code_is_removed_from_words():
    cleaned = blocks.clean(
        doc({"type": "heading", "text": "{% load evil %}Hello{# note #}"})
    )
    assert cleaned["blocks"][0]["text"] == "Hello"


@pytest.mark.parametrize(
    "href", ["javascript:alert(1)", "data:text/html,x", "//evil.test", "not a link"]
)
def test_unsafe_or_broken_links_are_refused(href):
    with pytest.raises(blocks.BlocksError):
        blocks.clean(doc({"type": "button", "label": "Go", "href": href}))


@pytest.mark.parametrize(
    "href,stored",
    [
        ("https://acme.test/pay", "https://acme.test/pay"),
        ("www.acme.test", "https://www.acme.test"),
        ("{{payment_link}}", "{{ payment_link }}"),
        ("mailto:hi@acme.test", "mailto:hi@acme.test"),
    ],
)
def test_links_that_work_in_email(href, stored):
    assert (
        blocks.clean(doc({"type": "button", "label": "Go", "href": href}))["blocks"][0][
            "href"
        ]
        == stored
    )


def test_a_button_needs_a_label_and_a_link():
    with pytest.raises(blocks.BlocksError, match="label"):
        blocks.clean(doc({"type": "button", "label": "", "href": "https://x.test"}))
    with pytest.raises(blocks.BlocksError, match="link"):
        blocks.clean(doc({"type": "button", "label": "Pay", "href": ""}))


def test_uploaded_images_get_an_absolute_address():
    html, _ = blocks.render(
        doc({"type": "image", "src": "/media/email_assets/logo.png", "alt": "Logo"}),
        base_url="https://app.akilent.test/",
    )
    assert 'src="https://app.akilent.test/media/email_assets/logo.png"' in html


def test_two_columns_stack_without_media_queries():
    html, text = blocks.render(
        doc(
            {
                "type": "columns",
                "columns": [
                    {"heading": "Left", "text": "One"},
                    {
                        "heading": "Right",
                        "button": {"label": "Buy", "href": "https://x.test"},
                    },
                ],
            }
        )
    )
    assert (
        html.count('class="ak-col"') == 2
        and "display:inline-block" in html
        and "@media" not in html
    )
    assert "Left" in text and "Buy: https://x.test" in text


def test_style_is_limited_to_safe_values():
    cleaned = blocks.clean(
        doc(
            style={
                "background": "red;}</style><script>",
                "font": "Comic Sans",
                "accent": "#00ff00",
            }
        )
    )
    assert cleaned["style"]["background"] == blocks.DEFAULT_STYLE["background"]
    assert cleaned["style"]["font"] == "sans"
    assert cleaned["style"]["accent"] == "#00FF00"


def test_unknown_sections_and_too_many_are_refused():
    with pytest.raises(blocks.BlocksError):
        blocks.clean(doc({"type": "video"}))
    with pytest.raises(blocks.BlocksError):
        blocks.clean(doc(*[{"type": "divider"}] * (blocks.MAX_BLOCKS + 1)))


def test_repeated_ids_are_made_unique():
    cleaned = blocks.clean(
        doc({"id": "a", "type": "divider"}, {"id": "a", "type": "divider"})
    )
    assert len({b["id"] for b in cleaned["blocks"]}) == 2


def test_sample_values_cover_every_blank_and_keep_the_owners():
    d = doc(
        {"type": "text", "text": "{{ first_name }} {{ order_number }}"},
        {"type": "button", "label": "Pay", "href": "{{ pay_link }}"},
    )
    values = blocks.sample_values(blocks.clean(d), {"first_name": "Ada"}, "Acme")
    assert values["first_name"] == "Ada" and values["company_name"] == "Acme"
    assert values["order_number"] and values["pay_link"] == "https://example.com"


# ---- what the editor opens -------------------------------------------------------------------


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


def _template(account, **fields):
    defaults = {
        "name": "T",
        "slug": f"t-{EmailTemplate.objects.count()}",
        "subject": "Hi",
        "html_body": "",
        "text_body": "",
    }
    return EmailTemplate.objects.create(account=account, **{**defaults, **fields})


@pytest.mark.django_db
def test_an_empty_template_starts_from_a_starter(account):
    opened = blocks.document_for(_template(account))
    assert [b["type"] for b in opened["blocks"]] == [
        "heading",
        "text",
        "button",
        "footer",
    ]
    assert "Acme" in opened["blocks"][-1]["text"]


@pytest.mark.django_db
def test_an_old_drag_and_drop_design_is_kept_as_one_html_section(account):
    t = _template(
        account,
        html_body="<p>Old design</p>",
        builder_mode="blocks",
        content_blocks={"pages": [{}]},
    )
    opened = blocks.document_for(t)
    assert opened["imported"] == "classic"
    assert opened["blocks"] == [
        {"id": opened["blocks"][0]["id"], "type": "html", "html": "<p>Old design</p>"}
    ]


@pytest.mark.django_db
def test_html_edited_by_hand_wins_over_an_older_block_document(account):
    saved = blocks.clean(doc({"type": "text", "text": "From blocks"}))
    t = _template(
        account,
        content_blocks=saved,
        builder_mode="raw",
        html_body="<p>Edited by hand</p>",
    )
    opened = blocks.document_for(t)
    assert (
        opened["imported"] == "raw"
        and opened["blocks"][0]["html"] == "<p>Edited by hand</p>"
    )

    html, _ = blocks.render(saved)
    t.html_body = html
    t.save()
    assert (
        blocks.document_for(t)["blocks"][0]["type"] == "text"
    )  # unchanged HTML: the sections open


# ---- the views ---------------------------------------------------------------------------------


@pytest.fixture
def owner_client(client, account, monkeypatch):
    monkeypatch.setattr(
        "apps.billing.limits.LimitChecker.has_feature", lambda self, name: True
    )
    client.force_login(account.owner)
    return client


@pytest.mark.django_db
def test_the_editor_page_opens_with_its_sections(owner_client, account):
    t = _template(account)
    page = owner_client.get(f"/email/templates/{t.pk}/?mode=blocks")
    assert page.status_code == 200
    body = page.content.decode()
    assert (
        'id="email-blocks-config"' in body
        and "email-blocks.js" in body
        and "grapes" not in body
    )
    config = json.loads(
        body.split('id="email-blocks-config" type="application/json">', 1)[1].split(
            "</script>", 1
        )[0]
    )
    assert (
        config["doc"]["format"] == blocks.FORMAT
        and config["sampleVariables"]["company_name"] == "Acme"
    )


@pytest.mark.django_db
def test_saving_builds_the_html_on_the_server_and_ignores_html_from_the_browser(
    owner_client, account
):
    t = _template(account)
    d = doc(
        {"type": "heading", "text": "Hello {{ first_name }}"},
        {"type": "button", "label": "Pay", "href": "{{ pay_link }}"},
    )
    resp = owner_client.post(
        f"/email/templates/{t.pk}/edit/",
        {
            "name": "Receipt",
            "subject": "Thanks",
            "builder_mode": "blocks",
            "content_blocks": json.dumps(d),
            "html_body": "<script>alert(1)</script>",
            "text_body": "nope",
            "sample_variables": "{}",
            "snapshot": "1",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 200 and resp.json()["saved"] is True
    t.refresh_from_db()
    assert t.builder_mode == "blocks" and t.name == "Receipt"
    assert (
        "<script>" not in t.html_body
        and "Hello {{ first_name }}" in t.html_body
        and 'href="{{ pay_link }}"' in t.html_body
    )
    assert "Pay: {{ pay_link }}" in t.text_body
    assert blocks.is_document(t.content_blocks)
    assert EmailTemplateVersion.objects.filter(template=t).count() == 1, (
        "Save keeps a version"
    )


@pytest.mark.django_db
def test_background_saves_keep_no_version(owner_client, account):
    t = _template(account)
    owner_client.post(
        f"/email/templates/{t.pk}/edit/",
        {
            "subject": "Hi",
            "builder_mode": "blocks",
            "content_blocks": json.dumps(doc({"type": "divider"})),
            "autosave": "1",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert not EmailTemplateVersion.objects.filter(template=t).exists()


@pytest.mark.django_db
def test_an_unusable_email_is_not_saved_and_says_why(owner_client, account):
    t = _template(account, html_body="<p>Before</p>")
    resp = owner_client.post(
        f"/email/templates/{t.pk}/edit/",
        {
            "subject": "Hi",
            "builder_mode": "blocks",
            "autosave": "1",
            "content_blocks": json.dumps(
                doc({"type": "button", "label": "Pay", "href": "javascript:x"})
            ),
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 400 and "isn't a link" in resp.json()["error"]
    t.refresh_from_db()
    assert t.html_body == "<p>Before</p>"


@pytest.mark.django_db
def test_preview_and_test_render_the_sections(owner_client, account):
    t = _template(account)
    payload = {
        "subject": "Hi {{ first_name }}",
        "variables": {"first_name": "Ada"},
        "blocks": doc({"type": "text", "text": "Hello {{ first_name }}"}),
    }
    resp = owner_client.post(
        f"/email/templates/{t.pk}/preview/",
        json.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert "Hello Ada" in resp.json()["html"] and resp.json()["subject"] == "Hi Ada"

    bad = owner_client.post(
        f"/email/templates/{t.pk}/preview/",
        json.dumps({"blocks": doc({"type": "video"})}),
        content_type="application/json",
    )
    assert bad.status_code == 400


@pytest.mark.django_db
def test_the_image_picker_lists_this_businesss_images_only(owner_client, account):
    resp = owner_client.get("/email/templates/assets/?format=json")
    assert resp.status_code == 200 and resp.json() == {"assets": []}


@pytest.mark.django_db
def test_html_changed_elsewhere_is_never_overwritten_by_an_older_block_design(account):
    """The API can update ``html`` without touching ``builder_mode``: the editor must open that HTML."""
    saved = blocks.clean(doc({"type": "text", "text": "From blocks"}))
    html, _ = blocks.render(saved)
    t = _template(account, content_blocks=saved, builder_mode="blocks", html_body=html)
    assert blocks.document_for(t)["blocks"][0]["type"] == "text"

    t.html_body = "<p>Pushed by the API</p>"
    t.save()
    opened = blocks.document_for(t)
    assert (
        opened["imported"] == "raw"
        and opened["blocks"][0]["html"] == "<p>Pushed by the API</p>"
    )


@pytest.mark.django_db
def test_a_design_with_uploaded_images_still_opens_as_sections(owner_client, account):
    t = _template(account)
    d = doc({"type": "image", "src": "/media/email_assets/logo.png", "alt": "Logo"})
    owner_client.post(
        f"/email/templates/{t.pk}/edit/",
        {
            "subject": "Hi",
            "builder_mode": "blocks",
            "content_blocks": json.dumps(d),
            "autosave": "1",
        },
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    body = owner_client.get(f"/email/templates/{t.pk}/?mode=blocks").content.decode()
    config = json.loads(
        body.split('id="email-blocks-config" type="application/json">', 1)[1].split(
            "</script>", 1
        )[0]
    )
    assert (
        config["doc"]["blocks"][0]["type"] == "image"
        and "imported" not in config["doc"]
    )
