"""Inbox Phase 3: Content Studio's look, and controls swapped for ones that read and act at once.

- the title says where you are ("Inbox" over the view, the view being its menu);
- yellow is only for the one main action (Send; a template once the window has closed);
- the channel filter, Pause, Run, Start a form and Later are menus whose items post, each with
  the plain form kept for a page without scripts;
- templates are a list to read (name, kind, language, the start of the text), not a select;
- "What automations did" is a disclosure with a count, "Why didn't it reply?" an arrow link;
- 150ms motion for what arrives, never on the 8-second refresh, none with reduced motion.
"""

import datetime
import re

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

pytestmark = pytest.mark.django_db

BG = {"HTTP_HX_REQUEST": "true"}
PANE = {**BG, "HTTP_HX_TARGET": "conversation-pane-inner"}


@pytest.fixture
def shop(client):
    user = User.objects.create_user("o", "o@example.com", "pw", first_name="Ola")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _chat(account, *, hours_ago=1, channel="whatsapp"):
    contact = Contact.objects.create(
        account=account, first_name="Thandi", phone="+260971234567"
    )
    chat = Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )
    Message.objects.create(
        account=account,
        conversation=chat,
        direction="inbound",
        body="Hi",
        timestamp=timezone.now() - datetime.timedelta(hours=hours_ago),
    )
    return chat


def _template(account):
    from apps.whatsapp.models import MessageTemplate

    return MessageTemplate.objects.create(
        account=account,
        name="order_update",
        language_code="en",
        content="Hi {{1}}, your order is on its way and should arrive tomorrow.",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


def _yellow(html):
    return re.findall(r'class="btn btn-primary[^"]*"', html)


# --- Title -----------------------------------------------------------------------------


def test_the_title_is_inbox_over_the_view_and_the_view_is_the_menu(shop):
    client, _ = shop
    page = client.get("/inbox/?view=missed").content.decode()
    title = page[page.index('class="cv-menu inbox-views inbox-title"') :]
    title = title[: title.index("</h1>")]
    assert 'class="cs-crumb">Inbox</span>' in title
    assert '<h1 class="inbox-h1"><button type="button" class="inbox-views-btn"' in title
    assert "Missed" in title


# --- Yellow for the one main action ----------------------------------------------------


def test_an_open_conversation_has_no_yellow_button_but_send(shop):
    """Send is yellow by its own class (.cv-send); every other action is tonal or quieter."""
    client, account = shop
    _template(account)  # the dialog's Send template is in the pane too
    chat = _chat(account)
    pane = client.get(f"/inbox/{chat.public_id}/", **PANE).content.decode()
    panel = client.get(f"/inbox/{chat.public_id}/panel/", **BG).content.decode()

    # The only btn-primary is the closed-window picker's, which stands in for Send.
    inline = pane[pane.index('id="template-inline-list"') :]
    assert len(_yellow(pane)) == 1 and _yellow(inline[: inline.index("</form>")])
    assert _yellow(panel) == []
    dialog = pane[pane.index('id="template-list"') :]
    assert 'class="btn btn-tonal btn-sm" :disabled="!tpl">Send template' in dialog
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    assert (
        '.cv-send:not(.cv-send--rec)[aria-label="Send"] { background: var(--color-amber-400);'
        in css
    )


# --- Menus that post -------------------------------------------------------------------


def test_the_channel_filter_is_a_menu_on_a_get_form(shop):
    client, account = shop
    _chat(account)
    page = client.get("/inbox/?channel=whatsapp").content.decode()
    form = page[page.index('class="cv-menu inbox-filter is-active"') :]
    form = form[: form.index("</form>")]

    assert 'type="submit" name="channel" value="whatsapp"' in form
    assert 'role="menuitemradio" aria-checked="true"' in form
    assert 'type="submit" name="channel" value=""' in form  # All channels
    assert '<span class="truncate">WhatsApp</span>' in form  # the channel on the button
    assert "<noscript>" in form and '<select name="channel"' in form


def test_the_details_menus_post_and_keep_a_plain_form_without_scripts(shop):
    client, account = shop
    chat = _chat(account)
    panel = client.get(f"/inbox/{chat.public_id}/panel/", **BG).content.decode()

    pause = panel[panel.index('value="pause_automation"') :]
    pause = pause[: pause.index("</form>")]
    for hours in ("1", "4", "24", "forever"):
        assert f'type="submit" name="hours" value="{hours}"' in pause
    assert "<noscript>" in panel and 'id="pause-hours"' in panel


def test_later_is_a_menu_and_pick_a_date_opens_in_place(shop):
    from apps.conversations.models import FollowUp

    client, account = shop
    chat = _chat(account)
    FollowUp.objects.create(
        account=account,
        contact=chat.contact,
        conversation=chat,
        due_at=timezone.now() + datetime.timedelta(days=1),
    )
    panel = client.get(f"/inbox/{chat.public_id}/panel/", **BG).content.decode()

    later = panel[panel.index('aria-label="Later"') :]
    later = later[: later.index("</noscript>")]
    assert 'type="submit" name="when" value="tomorrow"' in later
    assert "data-pick-date" in later and "later = 'custom'" in later
    # The custom time posts when=custom only once chosen.
    assert (
        '<input type="hidden" name="when" value="custom" :disabled="later !== \'custom\'" />'
        in panel
    )


def test_what_automations_did_is_a_disclosure_with_a_count_and_why_is_an_arrow_link(
    shop,
):
    client, account = shop
    chat = _chat(account)
    panel = client.get(f"/inbox/{chat.public_id}/panel/", **BG).content.decode()

    summary = panel[panel.index('<details class="cv-disclosure"') :]
    summary = summary[: summary.index("</summary>")]
    assert 'What automations did<span class="inbox-tab-count">0</span>' in summary
    assert 'class="cv-arrow-link">Why didn\'t it reply?' in panel


# --- Templates to read -----------------------------------------------------------------


def test_templates_are_a_list_with_their_kind_and_text(shop):
    client, account = shop
    _template(account)
    pane = client.get(
        f"/inbox/{_chat(account, hours_ago=30).public_id}/", **PANE
    ).content.decode()
    picker = pane[pane.index('id="template-inline-list"') :]
    picker = picker[: picker.index("</fieldset>")]

    assert '<input type="radio" name="template_id"' in picker and "required" in picker
    assert "order_update" in picker and "· en" in picker
    assert "your order is on its way" in picker
    assert "<select" not in picker


# --- Motion ----------------------------------------------------------------------------


def test_only_a_search_animates_the_list_never_the_refresh(shop):
    client, account = shop
    _chat(account)
    page = client.get("/inbox/").content.decode()
    search = page[page.index('class="inbox-search"') :]
    search = search[: search.index(">", search.index("hx-on::after-request"))]
    assert "classList.add('is-fresh')" in search
    poll = page[page.index('id="inbox-body"') :]
    assert "is-fresh" not in poll[: poll.index(">")]
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    reduced = css[css.rindex("@media (prefers-reduced-motion: reduce) {") :]
    assert "#inbox-body.is-fresh .inbox-row { animation: none; }" in reduced
