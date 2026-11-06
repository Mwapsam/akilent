"""The inbox list as it renders — mostly the things that were wrong on a phone.

Each of these was a visible defect at 360px that no existing test noticed,
because nothing here raises: a mislabelled channel, a "+" where an initial
should be, and a wait time printed twice all render perfectly happily.
"""

from datetime import timedelta

import pytest
from django.conf import settings
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


def _conversation(
    account,
    *,
    channel="whatsapp",
    first="",
    phone="+260971234567",
    email=None,
    waited=timedelta(minutes=17),
    assigned_to=None,
    status=Conversation.Status.OPEN,
):
    contact = Contact.objects.create(
        account=account, first_name=first, phone=phone, email=email
    )
    conv = Conversation.objects.create(
        account=account,
        contact=contact,
        channel=channel,
        assigned_to=assigned_to,
        status=status,
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction="inbound",
        body="Price for 3?",
        timestamp=timezone.now() - waited,
    )
    return conv


def _inbox(client, view="all"):
    return client.get(f"/inbox/?view={view}").content.decode()


class TestAgo:
    NOW = timezone.now()

    def _ago(self, delta):
        return Template("{% load ui %}{{ t|ago:now }}").render(
            Context({"t": self.NOW - delta, "now": self.NOW})
        )

    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(seconds=20), "now"),
            (timedelta(minutes=7), "7m"),
            (timedelta(minutes=59), "59m"),
            (timedelta(hours=5, minutes=5), "5h"),
            (timedelta(days=2, hours=3), "2d"),
            (timedelta(days=6, hours=23), "6d"),
        ],
    )
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
        # Just the list, up to its own closing tag: other parts of the page (the
        # welcome tour) can name Email as a channel.
        start = body.index('<ul class="inbox-list"')
        row = body[start : body.index("</ul>", start)]
        assert 'data-channel="sms"' in row and "SMS" in row
        assert (
            ">Email<" not in row
            and "Email\n" not in row.split("inbox-meta", 1)[1][:400]
        )

    def test_a_phone_only_contact_gets_a_glyph_not_a_plus(self, logged_in):
        """The initial used to be the name's first character, and for a contact
        known only by number that is "+"."""
        client, account, _ = logged_in
        _conversation(account, first="", phone="+260971234567")
        body = _inbox(client)
        avatar = body[body.index("inbox-avatar") : body.index("inbox-avatar") + 400]
        assert "<svg" in avatar
        assert ">+<" not in avatar.replace(" ", "").replace("\n", "")

    def test_a_named_contact_keeps_their_initial(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Chanda")
        body = _inbox(client)
        avatar = body[body.index("inbox-avatar") : body.index("inbox-avatar") + 200]
        assert "C" in avatar and "<svg" not in avatar

    def test_the_wait_is_stated_once_not_twice(self, logged_in):
        """It used to read "17 minutes ago" beside a "Waiting 17 minutes"
        badge — the same fact twice, in the width the name needed."""
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(minutes=17))
        body = _inbox(client, "needs_attention")
        row = body[body.index('class="inbox-list"') :]
        assert row.count("17m") == 1
        assert "Waiting 17" not in row and "minutes" not in row

    def test_a_waiting_row_times_the_wait_and_marks_it_urgent(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(hours=1))
        body = _inbox(client, "needs_attention")
        assert 'class="inbox-time is-urgent"' in body
        assert "Waiting since" in body, "the precise time must still be one hover away"

    def test_a_long_waiting_row_escalates_to_overdue(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(hours=5))
        body = _inbox(client, "needs_attention")
        assert 'class="inbox-time is-overdue"' in body
        assert "is-urgent" not in body
        assert "overdue" in body, "the precise time's tooltip must say so"

    def test_a_conversation_assigned_to_you_says_you(self, logged_in):
        client, account, user = logged_in
        _conversation(account, first="Chanda", assigned_to=user)
        body = _inbox(client)
        meta = body[body.index("inbox-assignee") : body.index("inbox-assignee") + 400]
        assert "You" in meta and "amara" not in meta

    def test_someone_elses_assignment_shows_their_name(self, logged_in):
        client, account, _ = logged_in
        other = User.objects.create_user(
            "tembo", "t@example.com", "pw", first_name="Tembo"
        )
        Membership.objects.create(
            user=other, account=account, role=Membership.Role.MEMBER
        )
        _conversation(account, first="Chanda", assigned_to=other)
        meta = _inbox(client)
        meta = meta[meta.index("inbox-assignee") : meta.index("inbox-assignee") + 400]
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
    def test_the_views_are_one_menu_with_the_current_view_on_its_button(
        self, logged_in
    ):
        """Six tabs never fit beside a conversation; one menu does, at every width."""
        client, _, _ = logged_in
        body = client.get("/inbox/?view=assigned").content.decode()
        assert "inbox-tabs" not in body
        button = body[body.index('class="inbox-views-btn"') :]
        button = button[: button.index("</button>")]
        assert "Assigned to me" in button
        assert body.count('role="menuitemradio" aria-checked="true"') == 1

    def test_the_polled_fragment_renders_the_same_rows(self, logged_in):
        """The 8s poll re-renders _inbox_body.html on its own; the row changes
        must survive that path too, including knowing who "You" is."""
        client, account, user = logged_in
        _conversation(account, first="Chanda", assigned_to=user)
        frag = client.get(
            "/inbox/feed/?view=all", HTTP_HX_REQUEST="true"
        ).content.decode()
        assert "inbox-row" in frag
        assert (
            "You"
            in frag[frag.index("inbox-assignee") : frag.index("inbox-assignee") + 400]
        )

    def test_the_unassigned_tab_only_shows_open_unowned_conversations(self, logged_in):
        client, account, user = logged_in
        _conversation(account, first="Nobody")
        _conversation(account, first="Owned", phone="+260971234568", assigned_to=user)
        _conversation(
            account,
            first="ClosedUnowned",
            phone="+260971234569",
            status=Conversation.Status.CLOSED,
        )
        body = _inbox(client, "unassigned")
        row = body[body.index('class="inbox-list"') :]
        assert "Nobody" in row
        assert "Owned" not in row and "ClosedUnowned" not in row

    def test_the_closed_tab_shows_closed_conversations(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Resolved", status=Conversation.Status.CLOSED)
        _conversation(account, first="StillOpen", phone="+260971234570")
        body = _inbox(client, "closed")
        row = body[body.index('class="inbox-list"') :]
        assert "Resolved" in row and "StillOpen" not in row

    def test_the_overdue_toggle_combines_with_any_tab(self, logged_in):
        client, account, user = logged_in
        _conversation(
            account, first="JustWaiting", assigned_to=user, waited=timedelta(minutes=30)
        )
        _conversation(
            account,
            first="LongWaiting",
            phone="+260971234571",
            assigned_to=user,
            waited=timedelta(hours=5),
        )
        plain_row = _inbox(client, "assigned")
        plain_row = plain_row[plain_row.index('class="inbox-list"') :]
        assert "JustWaiting" in plain_row and "LongWaiting" in plain_row

        overdue_body = client.get("/inbox/?view=assigned&overdue=1").content.decode()
        row = overdue_body[overdue_body.index('class="inbox-list"') :]
        assert "LongWaiting" in row and "JustWaiting" not in row

    def test_an_unassigned_overdue_conversation_is_flagged_in_the_row(self, logged_in):
        client, account, _ = logged_in
        _conversation(account, first="Chanda", waited=timedelta(hours=5))
        row = _inbox(client, "unassigned")
        row = row[row.index('class="inbox-list"') :]
        assert 'title="Nobody is assigned to this conversation"' in row
        assert ">Unassigned<" in row


@pytest.mark.django_db
class TestConversationHeaderStatus:
    """The status beside the contact's name in the conversation header.

    On a phone it used to render "Waiting for you · 2 hours, 20 minutes" as one
    pill that wrapped onto three lines inside itself, in a ~75px column.
    """

    def _status(self, client, conv):
        body = client.get(f"/inbox/{conv.public_id}/").content.decode()
        # The header's status HTML ships in the page's chat config as JSON.
        import json
        import re

        config = json.loads(
            re.search(
                r'<script id="chat-config" type="application/json">(.*?)</script>',
                body,
                re.S,
            ).group(1)
        )
        return config["statusHtml"]

    def test_badges_never_wrap_inside_themselves(self, logged_in):
        client, account, _ = logged_in
        conv = _conversation(
            account, first="Sam", waited=timedelta(hours=2, minutes=20)
        )
        status = self._status(client, conv)
        badges = [b for b in status.split('class="badge') if b.strip()][1:]
        assert badges, "no status badge rendered"
        assert all(
            b.startswith(" badge-") and "whitespace-nowrap" in b.split(">", 1)[0]
            for b in badges
        )

    def test_the_badge_is_short_and_the_sentence_is_its_title(self, logged_in):
        """The status sits beside the name on one row now, at every width."""
        client, account, _ = logged_in
        conv = _conversation(
            account, first="Sam", waited=timedelta(hours=2, minutes=20)
        )
        status = self._status(client, conv)
        assert ">Waiting · 2h</span>" in status
        assert 'title="Waiting for you since' in status and "sm:inline" not in status
        # The state comes before the channel, so the channel is what gives way.
        assert status.index("Waiting · 2h") < status.index("channel-tag")

    def test_the_header_is_one_row_with_the_status_beside_the_name(self, logged_in):
        """A second header row for the status cost the thread its height."""
        client, account, _ = logged_in
        conv = _conversation(account, first="Sam")
        body = client.get(f"/inbox/{conv.public_id}/").content.decode()
        # The pane's styles live in assets/app.css (no longer inline in the pane).
        css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
        assert 'grid-template-areas: "back avatar title actions";' in css
        assert '"back avatar status status"' not in css
        title = body[body.index('class="cv-h-title"') :]
        title = title[: title.index('class="cv-h-actions')]
        assert 'class="cv-h-name' in title and 'class="cv-h-status"' in title


@pytest.mark.django_db
class TestInboxLayout:
    def _list(self, body):
        return body[body.index('class="inbox-list"') :]

    def test_the_channel_filter_and_views_sit_in_the_bar_and_ride_the_poll(
        self, logged_in
    ):
        """Controls that act on the list sit in the one bar above it. The poll's answer
        carries them out of band, or the 8s refresh would leave their counts behind."""
        client, account, _ = logged_in
        _conversation(account, first="Chanda")
        body = _inbox(client)
        bar = body[
            body.index('id="inbox-bar"') : body.index(
                "</header>", body.index('id="inbox-bar"')
            )
        ]
        assert 'name="channel"' in bar and 'class="inbox-views-btn"' in bar
        assert "Follow-ups" in bar and "Set-up" in bar
        assert "hx-swap-oob" not in bar  # only the poll's copy swaps out of band
        frag = client.get(
            "/inbox/feed/?view=all", HTTP_HX_REQUEST="true"
        ).content.decode()
        oob = frag[frag.index('id="inbox-views" hx-swap-oob="true"') :]
        assert 'name="channel"' in oob and 'class="inbox-views-btn"' in oob

    def test_a_waiting_row_has_no_waiting_badge_but_still_says_so_to_screen_readers(
        self, logged_in
    ):
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
        contact = Contact.objects.create(
            account=account, first_name="Empty", phone="+260970000000"
        )
        Conversation.objects.create(
            account=account, contact=contact, channel="whatsapp"
        )
        row = self._list(_inbox(client))
        assert "No messages yet" in row
        assert ">Closed<" not in row

    def test_a_quiet_conversation_says_quiet_not_closed(self, logged_in):
        """Closed is the stored status. An open conversation the customer hasn't written in
        for a day, with us having spoken last, is Quiet, and the badge says why."""
        client, account, _ = logged_in
        conv = _conversation(account, first="Joseph", waited=timedelta(days=2))
        Message.objects.create(
            account=account,
            conversation=conv,
            direction="outbound",
            status="delivered",
            body="See you then",
            timestamp=timezone.now() - timedelta(days=1, hours=23),
        )
        row = self._list(_inbox(client))
        assert ">Quiet</span>" in row and ">Closed<" not in row
        assert "No message from the customer in over 24 hours" in row
