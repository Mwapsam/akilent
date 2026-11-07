"""Campaigns plan, Phase F: the composer reads like the rest of the app.

- templates as a list to read (readable name, kind, text), with search past three;
- a blank's answer is a segmented choice, Customer detail or Fixed text, and the server reads
  the kind chosen, with or without scripts (an older form's var__=literal still works);
- Review says what it costs without a price; the test send is a quiet disclosure;
- Sending hours' Save is navy; the final Send stays the page's one yellow button.
"""

import pytest

from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign
from apps.whatsapp.tests.test_campaign_composer import composer  # noqa: F401
from apps.whatsapp.tests.test_campaign_results import (  # noqa: F401
    _wa_urls,
    shop,
)

pytestmark = pytest.mark.django_db


def _more_templates(account, n, **extra):
    for i in range(n):
        MessageTemplate.objects.create(
            account=account,
            name=f"offer_{i}",
            whatsapp_template_name=f"offer_{i}",
            content=f"Offer number {i}",
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            **extra,
        )


# --- Templates -------------------------------------------------------------------------


@_wa_urls
def test_templates_read_as_a_list_with_their_text(composer):  # noqa: F811
    client, account, template, _ = composer
    page = client.get("/campaigns/new/").content.decode()
    option = page[page.index(f'data-tpl-option="{template.pk}"') :]
    option = option[: option.index("</label>")]
    assert "Download app" in option  # the readable name, not download_app
    assert "Update · EN" in option
    assert "Hi [name], get our app" in option
    assert 'data-search="download app download_app hi [name], get our app"' in page
    assert "data-tpl-search" not in page  # three or fewer: no search box


@_wa_urls
def test_past_three_templates_there_is_a_search(composer):  # noqa: F811
    client, account, *_ = composer
    _more_templates(account, 3)
    page = client.get("/campaigns/new/").content.decode()
    assert "data-tpl-search" in page and "data-tpl-none" in page


# --- Fill in the blanks ----------------------------------------------------------------


def _post(client, template, clist, **fields):
    return client.post(
        "/campaigns/new/",
        {
            "contact_list": clist.pk,
            "template_id": template.pk,
            "action": "draft",
            **fields,
        },
    )


@_wa_urls
def test_fixed_text_chosen_wins_over_the_detail_menu(composer):  # noqa: F811
    """Without scripts both the menu and the text box are sent: the kind chosen decides."""
    client, account, template, clist = composer
    key = f"{template.pk}__name"
    _post(
        client,
        template,
        clist,
        name="Fixed",
        **{
            f"src__{key}": "literal",
            f"var__{key}": "first_name",
            f"lit__{key}": "friend",
            f"fb__{key}": "there",
        },
    )
    assert WhatsAppCampaign.objects.get(name="Fixed").variable_mapping == {
        "name": "friend"
    }


@_wa_urls
def test_a_customer_detail_chosen_uses_the_menu(composer):  # noqa: F811
    client, account, template, clist = composer
    key = f"{template.pk}__name"
    _post(
        client,
        template,
        clist,
        name="Detail",
        **{
            f"src__{key}": "detail",
            f"var__{key}": "full_name",
            f"lit__{key}": "ignored",
            f"fb__{key}": "friend",
        },
    )
    campaign = WhatsAppCampaign.objects.get(name="Detail")
    assert campaign.variable_mapping == {"name": "contact.full_name"}
    assert campaign.variable_fallbacks == {"name": "friend"}


@_wa_urls
def test_a_blank_that_isnt_a_detail_starts_as_fixed_text(composer):  # noqa: F811
    client, account, template, _ = composer
    template.variables = ["code"]
    template.content = "Use {{1}} at checkout"
    template.save()
    page = client.get("/campaigns/new/").content.decode()
    key = f"{template.pk}__code"
    kind = page[page.index(f'data-blank="{key}"') :]
    kind = kind[kind.index("data-blank-kind") : kind.index("</div>")]
    assert kind.index('value="literal"') < kind.index("checked")


@_wa_urls
def test_a_draft_with_fixed_text_reopens_on_fixed_text(composer):  # noqa: F811
    client, account, template, clist = composer
    key = f"{template.pk}__name"
    _post(
        client,
        template,
        clist,
        name="Draft",
        **{f"src__{key}": "literal", f"lit__{key}": "friend"},
    )
    draft = WhatsAppCampaign.objects.get(name="Draft")
    page = client.get(f"/campaigns/new/?draft={draft.pk}").content.decode()
    import json
    import re

    kinds = json.loads(
        re.search(r'id="composer-kinds"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    assert kinds[key] == "literal"
    assert 'value="friend"' in page or '"friend"' in page


# --- Review ----------------------------------------------------------------------------


def _review(client, template, clist):
    html = client.get(
        "/campaigns/estimate/",
        {
            "contact_list": clist.pk,
            "template_id": template.pk,
            f"var__{template.pk}__name": "first_name",
        },
        HTTP_HX_REQUEST="true",
    ).content.decode()
    return html[html.index('id="review-summary"') :]


@_wa_urls
def test_review_says_what_it_costs_without_a_price(composer):  # noqa: F811
    client, account, template, clist = composer
    cost = _review(client, template, clist)
    cost = cost[
        cost.index("data-review-cost") : cost.index(
            "</div>", cost.index("data-review-cost")
        )
    ]
    assert (
        "1 utility message" in cost
        and "charged by Meta to your WhatsApp account" in cost
    )
    assert "$" not in cost

    template.category = MessageTemplate.Category.MARKETING
    template.save(update_fields=["category"])
    assert "1 marketing message" in _review(client, template, clist)


@_wa_urls
def test_review_names_the_message_readably(composer):  # noqa: F811
    client, account, template, clist = composer
    assert "Download app · Update" in _review(client, template, clist)


# --- Controls and buttons --------------------------------------------------------------


@_wa_urls
def test_the_test_send_is_a_quiet_disclosure(composer):  # noqa: F811
    client, *_ = composer
    page = client.get("/campaigns/new/").content.decode()
    assert '<details class="cv-disclosure mt-4" data-test-send>' in page
    assert 'class="campaign-composer"' in page


@_wa_urls
def test_only_send_is_yellow(composer):  # noqa: F811
    client, account, template, clist = composer
    settings_page = client.get("/campaigns/settings/").content.decode()
    assert 'class="btn btn-secondary btn-sm">Save' in settings_page
    html = client.get(
        "/campaigns/estimate/",
        {
            "contact_list": clist.pk,
            "template_id": template.pk,
            f"var__{template.pk}__name": "first_name",
        },
        HTTP_HX_REQUEST="true",
    ).content.decode()
    slot = html[html.index('id="send-slot"') :]
    assert 'class="btn btn-primary btn-sm"' in slot[: slot.index("</button>")]
