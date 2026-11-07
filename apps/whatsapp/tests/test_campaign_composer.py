"""Campaigns plan, Phase B: the composer's bugs.

- the start time outside sending hours is the workspace's local time (it printed UTC
  labelled as local: 08:00 in Lusaka read "06:00 (Africa/Lusaka)");
- "Send now" outside sending hours says when it really starts, in step 3, on Review and after;
- authentication (one-time code) templates aren't offered and can't be sent;
- "Fill in the blanks": a name's fallback starts filled in ("there"), and only the chosen
  kind of answer is sent.
"""

import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.messages import get_messages

from apps.accounts.models import BusinessHours
from apps.contacts.models import Contact
from apps.whatsapp.campaigns import CampaignError, _check_sendable
from apps.whatsapp.models import (
    CampaignSendWindow,
    MessageTemplate,
    WhatsAppCampaign,
    WhatsAppContact,
)
from apps.whatsapp.tests.test_campaign_results import _wa_urls, shop  # noqa: F401

pytestmark = pytest.mark.django_db
LUSAKA = ZoneInfo("Africa/Lusaka")
FRI_22 = datetime.datetime(2026, 11, 6, 22, 0, tzinfo=LUSAKA)  # sending hours closed
FRI_10 = datetime.datetime(2026, 11, 6, 10, 0, tzinfo=LUSAKA)  # open


@pytest.fixture
def composer(shop):  # noqa: F811
    from apps.whatsapp.models import WhatsAppBusinessNumber

    client, account, template, clist = shop
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka", schedule={})
    # Sending hours 08:00-20:00 (off by default under test settings).
    CampaignSendWindow.objects.create(account=account)
    WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="PN1", waba_id="WABA1", is_active=True
    )
    contact = Contact.objects.create(
        account=account, phone="+260971111111", first_name="Thandi"
    )
    WhatsAppContact.objects.create(
        account=account,
        phone_number=contact.phone,
        contact=contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    clist.contacts.add(contact)
    template.category = MessageTemplate.Category.UTILITY
    template.variables = ["name"]
    template.content = "Hi {{1}}, get our app"
    template.save()
    return client, account, template, clist


def _estimate(client, template, clist, at, **extra):
    with patch("django.utils.timezone.now", return_value=at):
        return client.get(
            "/campaigns/estimate/",
            {"contact_list": clist.pk, "template_id": template.pk, **extra},
            HTTP_HX_REQUEST="true",
        ).content.decode()


# --- When it starts --------------------------------------------------------------------


@_wa_urls
def test_outside_sending_hours_the_start_is_local_time(composer):
    client, _, template, clist = composer
    html = _estimate(client, template, clist, FRI_22)
    assert "messages start at Sat 08:00 (Africa/Lusaka)" in html
    assert "06:00" not in html


@_wa_urls
def test_send_now_says_when_it_really_starts(composer):
    client, _, template, clist = composer
    html = _estimate(client, template, clist, FRI_22)
    note = html[html.index('id="when-now-note"') :]
    assert "Your sending hours are closed now, so it starts Sat 08:00" in note
    review = html[html.index('id="review-summary"') :]
    review = review[: review.index("</dl>")]
    assert "Starts Sat 08:00 (Africa/Lusaka)" in review and ">Now<" not in review

    html = _estimate(client, template, clist, FRI_10)
    note = html[html.index('id="when-now-note"') :]
    assert "It starts going out as soon as you press send." in note
    assert ">Now</dd>" in html


@_wa_urls
def test_sending_outside_hours_says_so_after(
    composer, django_capture_on_commit_callbacks
):
    client, account, template, clist = composer
    with (
        patch("django.utils.timezone.now", return_value=FRI_22),
        django_capture_on_commit_callbacks(execute=True),
    ):
        resp = client.post(
            "/campaigns/new/",
            {
                "name": "Launch",
                "contact_list": clist.pk,
                "template_id": template.pk,
                f"var__{template.pk}__name": "first_name",
            },
        )
    assert resp.status_code == 302
    said = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("starts Sat 7 Nov 2026, 08:00 (Africa/Lusaka)" in m for m in said), said


# --- Authentication templates ----------------------------------------------------------


@_wa_urls
def test_authentication_templates_are_not_offered_or_sent(composer):
    client, account, template, clist = composer
    otp = MessageTemplate.objects.create(
        account=account,
        name="account_confirmation",
        whatsapp_template_name="account_confirmation",
        content="Your code is {{1}}",
        category=MessageTemplate.Category.AUTHENTICATION,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    page = client.get("/campaigns/new/").content.decode()
    assert "account_confirmation" not in page and template.name in page

    with pytest.raises(CampaignError, match="one-time code"):
        _check_sendable(otp)

    resp = client.post(
        "/campaigns/new/",
        {"name": "Codes", "contact_list": clist.pk, "template_id": otp.pk},
    )
    assert resp.status_code == 400
    assert not WhatsAppCampaign.objects.filter(name="Codes").exists()


# --- Fill in the blanks ----------------------------------------------------------------


@_wa_urls
def test_a_names_fallback_starts_filled_in(composer):
    client, _, template, _ = composer
    page = client.get("/campaigns/new/").content.decode()
    key = f"{template.pk}__name"
    blank = page[page.index(f'data-blank="{key}"') :]
    blank = blank[: blank.index("</select>")]
    assert "data-blank-source" in blank
    import json
    import re

    fbs = json.loads(
        re.search(r'id="composer-fbs"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    assert fbs[key] == "there"
    # Phase F: the kind of answer is a segmented control, a customer detail chosen at first.
    kind = page[page.index(f'data-blank="{key}"') :]
    kind = kind[kind.index("data-blank-kind") : kind.index("</div>")]
    assert f'name="src__{key}" value="detail"' in kind and ">Fixed text</label>" in kind
    assert kind.index("checked") < kind.index('value="literal"')


@_wa_urls
def test_only_the_chosen_kind_of_answer_counts(
    composer, django_capture_on_commit_callbacks
):
    client, account, template, clist = composer
    key = f"{template.pk}__name"
    base = {"contact_list": clist.pk, "template_id": template.pk}

    # A customer detail with its fallback left empty still falls back to "there".
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            "/campaigns/new/",
            {**base, "name": "A", f"var__{key}": "first_name", f"fb__{key}": ""},
        )
    a = WhatsAppCampaign.objects.get(name="A")
    assert a.variable_mapping == {"name": "contact.first_name"}
    assert a.variable_fallbacks == {"name": "there"}

    # Fixed text left empty is a blank not filled in: nothing is sent.
    resp = client.post(
        "/campaigns/new/",
        {**base, "name": "B", f"var__{key}": "literal", f"lit__{key}": ""},
    )
    assert resp.status_code == 400
    assert b"Fill in what goes in every blank" in resp.content
    assert not WhatsAppCampaign.objects.filter(name="B").exists()
