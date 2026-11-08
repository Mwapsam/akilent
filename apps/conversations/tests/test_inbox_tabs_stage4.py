"""Inbox pages plan, Stage 4: Comments and Follow-ups as Inbox tabs, and "Why didn't it
reply?" as a drawer from a conversation.

* Comments: one flat list, newest first, Not answered by default; replies nested under
  their comment; posts with no comments left out; "Load more" pages on by time; hidden
  ones folded; bulk Hide (with an Undo that unhides at Meta) and permanent Delete; the
  queries don't grow with the number of comments or replies.
* Follow-ups: rows are J/K rows; Later's presets in the workspace's time zone, including
  "This evening"; the email setting is a switch.
* Why didn't it reply?: opens in a drawer from the conversation's ⋯ menu; the page's
  picker searches conversations.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.facebook.models.comment import FacebookComment, FacebookCommentThread

pytestmark = pytest.mark.django_db

HIDE = "apps.facebook.providers.meta.MetaFacebookProvider.hide_comment"
DELETE = "apps.facebook.providers.meta.MetaFacebookProvider.delete_comment"


@pytest.fixture
def page():
    from apps.facebook.tests.helpers import make_page

    account, user, conn = make_page()
    return account, user, conn


def _thread(account, conn, n, *, at=None, replies=0, outbound_reply=False, post="p1"):
    from apps.facebook.models.contact import FacebookContact

    contact, _ = FacebookContact.objects.get_or_create(
        account=account, psid=f"u{n}", defaults={"name": f"Visitor {n}"}
    )
    at = at or timezone.now() - datetime.timedelta(minutes=n)
    thread = FacebookCommentThread.objects.create(
        account=account,
        page_id=conn.page_id,
        post_id=post,
        facebook_contact=contact,
        comment_id=f"c{n}",
        body=f"Comment {n}",
        received_at=at,
    )
    FacebookComment.objects.create(
        thread=thread,
        facebook_contact=contact,
        comment_id=f"c{n}",
        body=f"Comment {n}",
        timestamp=at,
    )
    for r in range(replies):
        FacebookComment.objects.create(
            thread=thread,
            facebook_contact=contact,
            comment_id=f"c{n}-r{r}",
            body=f"Reply {n}.{r}",
            timestamp=at + datetime.timedelta(seconds=r + 1),
        )
    if outbound_reply:
        FacebookComment.objects.create(
            thread=thread,
            facebook_contact=contact,
            comment_id=f"c{n}-us",
            body="Thanks!",
            direction="outbound",
            timestamp=at + datetime.timedelta(seconds=30),
        )
    return thread


def _fields_ok():
    """The test thread helper needs these model fields; skip loudly if they moved."""
    names = {f.name for f in FacebookComment._meta.get_fields()}
    return {"direction", "timestamp", "moderation_state"} <= names


# --- Comments ------------------------------------------------------------------------


def test_comments_default_to_not_answered_with_replies_nested(client, page):
    account, user, conn = page
    assert _fields_ok()
    _thread(account, conn, 1, replies=2)
    _thread(account, conn, 2, outbound_reply=True)
    client.force_login(user)
    body = client.get("/inbox/comments/").content.decode()
    assert "Comment 1" in body and "Reply 1.0" in body and "Reply 1.1" in body
    assert "Comment 2" not in body  # answered, so not in "Not answered"
    assert 'aria-current="page">Not answered<' in body
    everything = client.get("/inbox/comments/?filter=all").content.decode()
    assert "Comment 2" in everything and "Answered" in everything


def test_comments_are_one_list_newest_first(client, page):
    account, user, conn = page
    _thread(account, conn, 3, post="p-a")
    _thread(account, conn, 1, post="p-b")
    _thread(account, conn, 2, post="p-a")
    client.force_login(user)
    body = client.get("/inbox/comments/").content.decode()
    assert body.index("Comment 1") < body.index("Comment 2") < body.index("Comment 3")
    assert "data-row" in body


def test_load_more_pages_on_by_time(client, page):
    from apps.conversations import comments

    account, user, conn = page
    for n in range(1, comments.FEED_PAGE + 6):
        _thread(account, conn, n)
    client.force_login(user)
    body = client.get("/inbox/comments/").content.decode()
    assert f"Comment {comments.FEED_PAGE}<" in body
    assert f"Comment {comments.FEED_PAGE + 1}<" not in body
    assert "Load more" in body

    rows, cursor = comments.comment_feed(account)
    assert len(rows) == comments.FEED_PAGE and cursor is not None
    more = client.get(
        "/inbox/comments/",
        {"before": cursor.isoformat()},
        headers={"HX-Request": "true"},
    ).content.decode()
    assert "<html" not in more  # just the next rows
    assert f"Comment {comments.FEED_PAGE + 1}<" in more
    assert "Comment 1<" not in more
    assert "Load more" not in more  # the end


def test_hidden_comments_fold_away(client, page):
    account, user, conn = page
    t = _thread(account, conn, 1)
    t.comments.update(moderation_state="hidden", is_hidden=True)
    _thread(account, conn, 2)
    client.force_login(user)
    body = client.get("/inbox/comments/").content.decode()
    assert "1 hidden · show" in body


def test_one_post_shows_its_header_and_composer(client, page):
    account, user, conn = page
    _thread(account, conn, 1, post="p-x")
    _thread(account, conn, 2, post="p-y")
    client.force_login(user)
    body = client.get("/inbox/comments/?channel=facebook&post=p-x").content.decode()
    assert "data-post-header" in body and "Comment on this post" in body
    assert "Comment 1" in body and "Comment 2" not in body


def test_query_count_does_not_grow_with_comments(client, page):
    account, user, conn = page
    from apps.conversations import comments

    _thread(account, conn, 1, replies=1)
    comments.comment_feed(account)  # warm the per-process site settings cache
    with CaptureQueriesContext(connection) as small:
        comments.comment_feed(account)
    for n in range(2, 15):
        _thread(account, conn, n, replies=3, post=f"p{n}")
    with CaptureQueriesContext(connection) as big:
        rows, _ = comments.comment_feed(account)
    assert len(rows) == 14
    assert len(big) == len(small)


def test_bulk_hide_with_an_undo_that_unhides_at_meta(client, page):
    account, user, conn = page
    a, b = _thread(account, conn, 1), _thread(account, conn, 2)
    ca, cb = a.comments.get(), b.comments.get()
    client.force_login(user)
    with patch(HIDE, return_value=(True, "")) as hide:
        r = client.post(
            "/inbox/comments/bulk/",
            {"action": "hide", "items": [f"facebook:{ca.pk}", f"facebook:{cb.pk}"]},
        )
    assert r.status_code == 302
    assert [c.args[-1] for c in hide.call_args_list] == [True, True]
    ca.refresh_from_db()
    assert ca.is_hidden
    from django.contrib.messages import get_messages

    msgs = list(get_messages(r.wsgi_request))
    assert "Hid 2 comments." in str(msgs[0])
    assert "/undo/?t=" in msgs[0].extra_tags
    path = msgs[0].extra_tags[msgs[0].extra_tags.index("/undo/") :].split()[0]
    with patch(HIDE, return_value=(True, "")) as unhide:
        client.post(path)
    assert [c.args[-1] for c in unhide.call_args_list] == [False, False]
    ca.refresh_from_db()
    cb.refresh_from_db()
    assert not ca.is_hidden and not cb.is_hidden


def test_bulk_hide_reports_partial_failures(client, page):
    account, user, conn = page
    a, b = _thread(account, conn, 1), _thread(account, conn, 2)
    client.force_login(user)
    with patch(HIDE, side_effect=[(True, ""), (False, "rate limited")]):
        r = client.post(
            "/inbox/comments/bulk/",
            {
                "action": "hide",
                "items": [
                    f"facebook:{a.comments.get().pk}",
                    f"facebook:{b.comments.get().pk}",
                ],
            },
        )
    from django.contrib.messages import get_messages

    text = str(next(iter(get_messages(r.wsgi_request))))
    assert "Hid 1 comment." in text and "1 couldn't be hidden" in text


def test_bulk_delete_is_permanent_and_says_so(client, page):
    account, user, conn = page
    a = _thread(account, conn, 1)
    client.force_login(user)
    body = client.get("/inbox/comments/").content.decode()
    assert "Permanently delete" in body and "This can\\'t be undone." in body
    with patch(DELETE, return_value=(True, "")):
        client.post(
            "/inbox/comments/bulk/",
            {"action": "delete", "items": [f"facebook:{a.comments.get().pk}"]},
        )
    assert a.comments.get().moderation_state == "deleted"


def test_bulk_ignores_other_workspaces_comments(client, page):
    from apps.facebook.tests.helpers import make_page

    account, user, conn = page
    other_account, _, other_conn = make_page(page_id="other-page")
    theirs = _thread(other_account, other_conn, 9)
    client.force_login(user)
    with patch(HIDE, return_value=(True, "")) as hide:
        client.post(
            "/inbox/comments/bulk/",
            {"action": "hide", "items": [f"facebook:{theirs.comments.get().pk}"]},
        )
    assert not hide.called
    assert not theirs.comments.get().is_hidden


# --- Follow-ups ------------------------------------------------------------------------


def test_later_presets_follow_the_workspace_clock():
    from apps.conversations import followups

    account = object()
    zone = ZoneInfo("Africa/Lusaka")
    morning = datetime.datetime(2026, 11, 8, 8, 0, tzinfo=zone)
    night = datetime.datetime(2026, 11, 8, 20, 0, tzinfo=zone)
    with patch.object(followups, "business_zone", return_value=zone):
        early = dict(followups.later_choices(account, morning))
        late = dict(followups.later_choices(account, night))
    assert early["evening"] == "This evening, 18:00"
    assert early["tomorrow"] == "Tomorrow, 9:00"
    assert early["next_week"] == "Next week, Mon 9 Nov, 9:00"  # 8 Nov 2026 is a Sunday
    assert "evening" not in late


def test_this_evening_resolves_to_six_in_the_workspace_zone():
    from apps.conversations import followups

    zone = ZoneInfo("Africa/Lusaka")
    now = datetime.datetime(2026, 11, 8, 10, 0, tzinfo=zone)
    with (
        patch.object(followups, "business_zone", return_value=zone),
        patch.object(followups.timezone, "now", return_value=now),
    ):
        due = followups.resolve_due_at("evening", account=object())
        assert due.astimezone(zone).hour == 18 and due.astimezone(zone).day == 8
    monday = datetime.datetime(2026, 11, 9, 10, 0, tzinfo=zone)
    with (
        patch.object(followups, "business_zone", return_value=zone),
        patch.object(followups.timezone, "now", return_value=monday),
    ):
        nxt = followups.resolve_due_at("next_week", account=object()).astimezone(zone)
        assert (nxt.day, nxt.hour, nxt.weekday()) == (16, 9, 0)  # Monday a week on
    with (
        patch.object(followups, "business_zone", return_value=zone),
        patch.object(
            followups.timezone,
            "now",
            return_value=datetime.datetime(2026, 11, 8, 19, 0, tzinfo=zone),
        ),
        pytest.raises(ValueError),
    ):
        followups.resolve_due_at("evening", account=object())


def test_followups_page_rows_and_switch(client):
    from django.contrib.auth.models import User

    from apps.accounts.models import Account, Membership
    from apps.contacts.models import Contact
    from apps.conversations.models import FollowUp

    account = Account.objects.create(company_name="Mwamba Kitchen")
    user = User.objects.create_user("me", "me@example.com", "pw", first_name="Ada")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    contact = Contact.objects.create(
        account=account, phone="+260971000001", first_name="Thandi"
    )
    FollowUp.objects.create(
        account=account,
        contact=contact,
        due_at=timezone.now() - datetime.timedelta(minutes=5),
        source="manual",
        assigned_to=user,
    )
    client.force_login(user)
    body = client.get("/inbox/followups/").content.decode()
    assert "data-row data-followup-row" in body
    assert 'role="switch"' in body
    assert "Tomorrow, 9:00" in body


# --- Why didn't it reply? --------------------------------------------------------------


def test_why_not_opens_as_a_drawer_and_searches(client):
    from django.contrib.auth.models import User

    from apps.accounts.models import Account, Membership
    from apps.automation.tests.test_why_not import say

    account = Account.objects.create(company_name="Acme")
    user = User.objects.create_user("me", "me@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    contact, message = say(account, "Do you deliver?")
    contact.first_name = "Bupe"
    contact.save(update_fields=["first_name"])
    conversation = message.conversation
    client.force_login(user)
    url = f"/automations/why-not/?conversation={conversation.public_id}"
    drawer = client.get(
        url, headers={"HX-Request": "true", "HX-Target": "overlay-body"}
    )
    assert drawer.status_code == 200
    html = drawer.content.decode()
    assert "<html" not in html and "Do you deliver?" in html
    assert "Open as a page" in html
    page = client.get(url).content.decode()
    assert 'id="why-search"' in page
    found = client.get(
        "/automations/why-not/", {"pick": "1", "q": "bup"}
    ).content.decode()
    assert conversation.public_id in found
    none = client.get(
        "/automations/why-not/", {"pick": "1", "q": "zzzz-nobody"}
    ).content.decode()
    assert "No conversation matches" in none
