"""The inbox list as it renders — mostly the things that were wrong on a phone.

Each of these was a visible defect at 360px that no existing test noticed,
because nothing here raises: a mislabelled channel, a "+" where an initial
should be, and a wait time printed twice all render perfectly happily.
"""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.template import Context, Template
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("amara", "amara@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


def _conversation(account, *, channel="whatsapp", first="", phone="+260971234567",
                  email=None, waited=timedelta(minutes=17), assigned_to=None):
    contact = Contact.objects.create(account=account, first_name=first, phone=phone, email=email)
    conv = Conversation.objects.create(
        account=account, contact=contact, channel=channel, assigned_to=assigned_to,
    )
    Message.objects.create(
        account=account, conversation=conv, direction="inbound",
        body="Price for 3?", timestamp=timezone.now() - waited,
    )
    return conv


def _inbox(client, view="all"):
    return client.get("/inbox/?view=%s" % view).content.decode()


class TestAgo:
    NOW = timezone.now()

    def _ago(self, delta):
        return Template("{% load ui %}{{ t|ago:now }}").render(
            Context({"t": self.NOW - delta, "now": self.NOW})
        )

    @pytest.mark.parametrize("delta,expected", [
        (timedelta(seconds=20), "now"),
        (timedelta(minutes=7), "7m"),
        (timedelta(minutes=59), "59m"),
        (timedelta(hours=5, minutes=5), "5h"),
        (timedelta(days=2, hours=3), "2d"),
        (timedelta(days=6, hours=23), "6d"),
    ])
    def test_short_forms(self, delta, expected):
        assert self._ago(delta) == expected

    def test_past_a_week_the_date_takes_over(self):
        """At that age the useful fact is when, not how long."""
        out = self._ago(timedelta(days=30))
        assert not out.endswith("d") and any(ch.isdigit() for ch in out)

    def test_nothing_renders_as_nothing(self):
        assert Template("{% load ui %}{{ t|ago }}").render(Context({"t": None})) == ""


@pytest.mark.django_db
class TestRows:
    def test_an_sms_conversation_is_labelled_sms(self, logged_in):
        """channel_tag used to fall through to its email branch for SMS, so a
        text-message conversation was labelled "Email"."""
        client, account, _ = logged_in
        _conversation(account, channel="sms")
        body = _inbox(client)
        row = body[body.index('class="inbox-list"'):]
        assert 'data-channel="sms"' in row and "SMS" in row
        assert ">Email<" not in row and "Email\n" not in row.split("inbox-meta", 1)[1][:400]

    def test_a_phone_only_contact_gets_a_glyph_not_a_plus(self, logged_in):
        """The initial used to be the name's first character, and for a contact
        known only by number that is "+"."""
        client, account, _ = logged_in
        _conversation(account, first="", phone="+260971234567")
        body = _inbox(client)
        avatar = body[body.index("inbox-avatar"):body.index("inbox-avatar") + 400]
        assert "<svg" in avatar
        assert ">+<" not in avatar.replace(" ", "").replace("\n", "")

    def test_a_named_contact_keeps_their_initial(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Chanda")
        body = _inbox(client)
        avatar = body[body.index("inbox-avatar"):body.index("inbox-avatar") + 200]
        assert "C" in avatar and "<svg" not in avatar

    def test_the_wait_is_stated_once_not_twice(self, logged_in):
        """It used to read "17 minutes ago" beside a "Waiting 17 minutes"
        badge — the same fact twice, in the width the name needed."""
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(minutes=17))
        body = _inbox(client, "needs_attention")
        row = body[body.index('class="inbox-list"'):]
        assert row.count("17m") == 1
        assert "Waiting 17" not in row and "minutes" not in row

    def test_a_waiting_row_times_the_wait_and_marks_it_urgent(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(hours=5))
        body = _inbox(client, "needs_attention")
        assert 'class="inbox-time is-urgent"' in body
        assert "Waiting since" in body, "the precise time must still be one hover away"

    def test_a_conversation_assigned_to_you_says_you(self, logged_in):
        client, account, user = logged_in
        _conversation(account, first="Chanda", assigned_to=user)
        body = _inbox(client)
        meta = body[body.index("inbox-assignee"):body.index("inbox-assignee") + 400]
        assert "You" in meta and "amara" not in meta

    def test_someone_elses_assignment_shows_their_name(self, logged_in):
        client, account, _ = logged_in
        other = User.objects.create_user("tembo", "t@example.com", "pw", first_name="Tembo")
        Membership.objects.create(user=other, account=account, role=Membership.Role.MEMBER)
        _conversation(account, first="Chanda", assigned_to=other)
        meta = _inbox(client)
        meta = meta[meta.index("inbox-assignee"):meta.index("inbox-assignee") + 400]
        assert "Tembo" in meta and "You" not in meta

    def test_unread_is_announced_in_text(self, logged_in):
        """The dot is aria-hidden; the state reaches a screen reader as words.
        An aria-label on a bare span, which is what it used to be, is ignored
        by most screen readers."""
        client, account, _ = logged_in
        _conversation(account, first="Chanda")
        assert '<span class="sr-only">Unread.</span>' in _inbox(client)


@pytest.mark.django_db
class TestTabs:
    def test_the_assigned_tab_has_a_short_label_for_phones(self, logged_in):
        """"Assigned to me" was the tab that got cut off at 360px."""
        client, _, _ = logged_in
        body = _inbox(client)
        assert '<span class="sm:hidden">Mine</span>' in body
        assert '<span class="hidden sm:inline">Assigned to me</span>' in body

    def test_the_current_tab_is_scrolled_into_view(self, logged_in):
        """The selected tab can sit past the right edge on a phone."""
        client, _, _ = logged_in
        body = _inbox(client)
        start = body.index('<nav class="inbox-tabs')
        # From the tabs' own opening tag: the sidebar's </nav> comes earlier in
        # the page and would otherwise end the slice before it began.
        nav = body[start:body.index("</nav>", start)]
        assert "$el.scrollLeft" in nav, "the scroll-into-view script is missing or mangled"

    def test_the_polled_fragment_renders_the_same_rows(self, logged_in):
        """The 8s poll re-renders _inbox_body.html on its own; the row changes
        must survive that path too, including knowing who "You" is."""
        client, account, user = logged_in
        _conversation(account, first="Chanda", assigned_to=user)
        frag = client.get("/inbox/feed/?view=all", HTTP_HX_REQUEST="true").content.decode()
        assert "inbox-row" in frag
        assert "You" in frag[frag.index("inbox-assignee"):frag.index("inbox-assignee") + 400]


@pytest.mark.django_db
class TestConversationHeaderStatus:
    """The status line under the contact's name in the conversation header.

    On a phone it used to render "Waiting for you · 2 hours, 20 minutes" as one
    pill that wrapped onto three lines inside itself, in a ~75px column.
    """

    def _status(self, client, conv):
        body = client.get("/inbox/%s/" % conv.public_id).content.decode()
        # The header's status HTML ships in the page's chat config as JSON.
        import json, re
        config = json.loads(re.search(
            r'<script id="chat-config" type="application/json">(.*?)</script>', body, re.S
        ).group(1))
        return config["statusHtml"]

    def test_badges_never_wrap_inside_themselves(self, logged_in):
        client, account, _ = logged_in
        conv = _conversation(account, first="Sam", waited=timedelta(hours=2, minutes=20))
        status = self._status(client, conv)
        badges = [b for b in status.split('class="badge') if b.strip()][1:]
        assert badges, "no status badge rendered"
        assert all(b.startswith(" badge-") and "whitespace-nowrap" in b.split(">", 1)[0] for b in badges)

    def test_phones_get_the_short_form_and_desktop_the_sentence(self, logged_in):
        client, account, _ = logged_in
        conv = _conversation(account, first="Sam", waited=timedelta(hours=2, minutes=20))
        status = self._status(client, conv)
        assert '<span class="sm:hidden">Waiting · 2h</span>' in status
        assert "Waiting for you · 2" in status and "hidden sm:inline" in status

    def test_the_header_gives_the_status_its_own_row_on_a_phone(self, logged_in):
        """The layout fix itself: below sm the status spans under the actions."""
        client, account, _ = logged_in
        conv = _conversation(account, first="Sam")
        body = client.get("/inbox/%s/" % conv.public_id).content.decode()
        assert '"back avatar name actions" "back avatar status status"' in body
        assert 'class="cv-h-status"' in body and 'class="cv-h-name' in body


@pytest.mark.django_db
class TestInboxLayout:
    def _list(self, body):
        return body[body.index('class="inbox-list"'):]

    def test_the_channel_filter_lives_with_the_tabs_not_the_page_header(self, logged_in):
        """Controls that act on the list sit with the list. It also has to be
        inside the polled fragment, or the 8s refresh would not carry it."""
        client, account, _ = logged_in
        _conversation(account, first="Chanda")
        body = _inbox(client)
        toolbar = body[body.index('class="inbox-toolbar"'):body.index('class="inbox-list"')]
        assert 'name="channel"' in toolbar and "inbox-tabs" in toolbar
        header = body[body.index('class="inbox-header"'):body.index("</header>", body.index('class="inbox-header"'))]
        assert 'name="channel"' not in header
        frag = client.get("/inbox/feed/?view=all", HTTP_HX_REQUEST="true").content.decode()
        assert 'name="channel"' in frag

    def test_a_waiting_row_has_no_waiting_badge_but_still_says_so_to_screen_readers(self, logged_in):
        """The amber clock carries "waiting"; a badge on top of it was the
        third time the row said the same thing."""
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(minutes=17))
        row = self._list(_inbox(client, "needs_attention"))
        assert "badge-warning" not in row
        assert '<span class="sr-only">Waiting</span>' in row

    def test_a_conversation_with_no_messages_is_not_labelled_closed(self, logged_in):
        """INDETERMINATE used to fall through the template's final else into
        "Closed" — a brand-new, empty conversation looked finished."""
        client, account, _ = logged_in
        contact = Contact.objects.create(account=account, first_name="Empty", phone="+260970000000")
        Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
        row = self._list(_inbox(client))
        assert "No messages yet" in row
        assert ">Closed<" not in row

    def test_a_quiet_conversation_explains_why_it_is_closed(self, logged_in):
        """Closed covers two cases in conversations.state; the badge says which."""
        client, account, _ = logged_in
        conv = _conversation(account, first="Joseph", waited=timedelta(days=2))
        Message.objects.create(account=account, conversation=conv, direction="outbound",
                               status="delivered", body="See you then",
                               timestamp=timezone.now() - timedelta(days=1, hours=23))
        row = self._list(_inbox(client))
        assert "the customer has not written in over 24 hours" in row
