"""Stage 3 shared pieces: the time formatter, readable template names and samples, the
status vocabulary, the settings list with Add in a modal, and Undo."""

import datetime
import re
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership, Team
from apps.core.display import readable_name, when_text

LUSAKA = ZoneInfo("Africa/Lusaka")  # UTC+2
UTC = datetime.UTC


def _at(*args, tz=UTC):
    return datetime.datetime(*args, tzinfo=tz)


# ── when_text ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "then, text",
    [
        (_at(2026, 11, 8, 11, 59, 30), "just now"),
        (_at(2026, 11, 8, 11, 55), "5 min ago"),
        (_at(2026, 11, 8, 9, 0), "3 hours ago"),
        (_at(2026, 11, 8, 11, 0), "1 hour ago"),
        (_at(2026, 11, 7, 12, 0), "yesterday"),
        (_at(2026, 10, 27, 12, 0), "12 days ago"),
        (_at(2026, 10, 9, 12, 0), "30 days ago"),
        (_at(2026, 10, 8, 11, 29), "8 Oct, 13:29"),  # Lusaka time
        (_at(2025, 12, 1, 8, 0), "1 Dec 2025, 10:00"),
    ],
)
def test_when_text(then, text):
    assert when_text(then, LUSAKA, now=_at(2026, 11, 8, 12, 0)) == text


def test_yesterday_follows_the_workspace_calendar_not_utc():
    """23:59 → 00:01 in Lusaka is "yesterday" there, though it's 2 minutes."""
    now = _at(2026, 11, 8, 0, 1, tz=LUSAKA)
    then = _at(2026, 11, 7, 23, 59, tz=LUSAKA)
    assert when_text(then, LUSAKA, now=now) == "yesterday"
    # In UTC those are 22:01 and 21:59 on the same day.
    assert when_text(then, UTC, now=now) == "2 min ago"


def test_the_new_year_shows_the_year():
    now = _at(2027, 1, 2, 10, 0, tz=LUSAKA)
    assert (
        when_text(_at(2026, 11, 20, 9, 0, tz=LUSAKA), LUSAKA, now=now)
        == "20 Nov 2026, 09:00"
    )
    assert (
        when_text(_at(2026, 12, 31, 23, 0, tz=LUSAKA), LUSAKA, now=now) == "2 days ago"
    )


def test_a_future_time_shows_its_date():
    now = _at(2026, 11, 8, 12, 0)
    assert when_text(_at(2026, 11, 8, 12, 5), LUSAKA, now=now) == "8 Nov, 14:05"


# ── Template names ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, shown",
    [
        ("order_update", "Order update"),
        ("hello_world", "Hello world"),
        ("3p_direct_integration_test_template", "3p direct integration test template"),
        ("Already Nice", "Already Nice"),
        ("", ""),
    ],
)
def test_readable_name(raw, shown):
    assert readable_name(raw) == shown


@pytest.fixture
def owner(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(
        company_name="Acme",
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user, account


@pytest.fixture
def owner_client(client, owner):
    client.force_login(owner[0])
    return client


def test_template_display_name_is_display_only(owner):
    """Every send reads ``name``; ``display_name`` is never stored or sent."""
    from apps.whatsapp.models import MessageTemplate

    t = MessageTemplate.objects.create(
        account=owner[1], name="order_update", content="Hi"
    )
    assert t.display_name == "Order update" and t.name == "order_update"
    assert not t.is_sample
    assert MessageTemplate(account=owner[1], name="hello_world", content="x").is_sample
    t.refresh_from_db()
    assert t.name == "order_update"


def test_a_send_uses_the_raw_template_name(owner):
    """Sends look a template up by its WhatsApp name (api.approved_template_by_name). The
    readable name is a property, never a field, so it can't be stored or sent."""
    from apps.whatsapp import api
    from apps.whatsapp.models import MessageTemplate

    t = MessageTemplate.objects.create(
        account=owner[1],
        name="order_update",
        whatsapp_template_name="order_update",
        content="Hi",
        approval_status="approved",
    )
    assert api.approved_template_by_name(owner[1], "order_update") == t
    assert api.approved_template_by_name(owner[1], "Order update") is None
    assert "display_name" not in {f.name for f in MessageTemplate._meta.get_fields()}


@pytest.mark.parametrize(
    "status, key, label",
    [
        ("approved", "live", "Approved"),
        ("pending", "pending", "Pending Meta approval"),
        ("rejected", "error", "Rejected"),
        ("paused", "error", "Paused by Meta"),
        ("draft", "draft", "Draft"),
    ],
)
def test_template_statuses_use_the_shared_vocabulary(owner, status, key, label):
    from apps.whatsapp.models import MessageTemplate

    t = MessageTemplate(account=owner[1], name="x", content="y", approval_status=status)
    assert (t.display_status["key"], t.display_status["label"]) == (key, label)


def test_a_rejection_reason_is_the_status_reason(owner):
    from apps.whatsapp.models import MessageTemplate

    t = MessageTemplate(
        account=owner[1],
        name="x",
        content="y",
        approval_status="rejected",
        rejection_reason="Too promotional",
    )
    assert t.display_status["reason"] == "Too promotional"


# ── Settings lists and Add in a modal ───────────────────────────────────────

OVERLAY = {"HTTP_HX_REQUEST": "true", "HTTP_X_AKILENT_OVERLAY": "1"}


def test_add_opens_as_a_page_without_javascript(owner_client):
    html = owner_client.get("/inbox/saved-replies/?add=1").content.decode()
    assert "Add a saved reply" in html and 'name="title"' in html


def test_saved_replies_list_shows_the_message_as_a_bubble(owner, owner_client):
    from apps.conversations.models import SavedReply

    SavedReply.objects.create(account=owner[1], title="Hours", body="Open 9-6")
    html = owner_client.get("/inbox/saved-replies/").content.decode()
    assert (
        'class="slist"' in html and 'class="wa-bubble"' in html and "Open 9-6" in html
    )
    assert 'aria-label="Actions for Hours"' in html
    assert 'href="/inbox/saved-replies/?add=1" data-overlay-open="modal"' in html


def test_a_bad_add_in_the_modal_stays_in_the_modal(owner_client):
    from apps.core import htmx

    with patch.object(htmx, "is_overlay", return_value=True):
        resp = owner_client.post("/inbox/saved-replies/", {"title": "", "body": "x"})
    assert resp.status_code == 422
    assert "needs both a title and a message" in resp.content.decode()


def test_a_good_add_in_the_modal_closes_it(owner, owner_client):
    from apps.conversations.models import SavedReply
    from apps.core import htmx

    with patch.object(htmx, "is_overlay", return_value=True):
        resp = owner_client.post(
            "/inbox/saved-replies/", {"title": "Hi", "body": "Hello"}
        )
    assert resp.status_code == 204 and "overlay-close" in resp["HX-Trigger"]
    assert SavedReply.objects.filter(account=owner[1], title="Hi").exists()


def test_adding_a_form_from_the_modal_goes_to_its_questions(owner, owner_client):
    from apps.billing.models import AccountFeatureOverride
    from apps.conversations.models import ConversationForm
    from apps.core import htmx

    AccountFeatureOverride.objects.create(
        account=owner[1], key="automations", grant=True, note="t"
    )
    with patch.object(htmx, "is_overlay", return_value=True):
        resp = owner_client.post("/inbox/forms/", {"name": "Intake"})
    form = ConversationForm.objects.get(account=owner[1], name="Intake")
    assert resp.status_code == 204 and resp["HX-Redirect"] == f"/inbox/forms/{form.pk}/"


def test_teams_are_a_settings_list(owner, owner_client):
    Team.objects.create(account=owner[1], name="Sales")
    html = owner_client.get("/inbox/settings/teams/").content.decode()
    assert 'class="slist"' in html and "0 members" in html
    assert 'href="/inbox/teams/?add=1" data-overlay-open="modal"' in html


# ── Undo ────────────────────────────────────────────────────────────────────


def _undo_path(resp_or_messages) -> str:
    return re.search(r"undo:(\S+)", resp_or_messages).group(1)


def test_deleting_a_saved_reply_can_be_undone(owner, owner_client):
    from apps.conversations.models import SavedReply

    reply = SavedReply.objects.create(account=owner[1], title="Hours", body="Open 9-6")
    resp = owner_client.post(f"/inbox/saved-replies/{reply.pk}/delete/", follow=True)
    assert not SavedReply.objects.filter(pk=reply.pk).exists()  # gone from every read
    assert SavedReply.all_objects.get(pk=reply.pk).deleted_at is not None
    assert 'class="slist-title">Hours<' not in resp.content.decode()
    undo = _undo_path(resp.content.decode())
    owner_client.post(undo, HTTP_HX_REQUEST="true")
    assert SavedReply.objects.filter(pk=reply.pk).exists()


def test_a_deleted_saved_reply_is_not_in_the_composer_menu(owner, owner_client):
    from apps.conversations.models import SavedReply

    SavedReply.objects.create(
        account=owner[1], title="Gone", body="x", deleted_at=timezone.now()
    )
    menu = owner_client.get(
        "/inbox/saved-replies/?menu=1", HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "Gone" not in menu


def test_old_deleted_saved_replies_are_purged(owner):
    from apps.conversations.models import SavedReply
    from apps.conversations.tasks import purge_deleted_saved_replies

    old = SavedReply.objects.create(account=owner[1], title="Old", body="x")
    SavedReply.all_objects.filter(pk=old.pk).update(
        deleted_at=timezone.now() - datetime.timedelta(days=31)
    )
    recent = SavedReply.objects.create(
        account=owner[1], title="Recent", body="x", deleted_at=timezone.now()
    )
    assert purge_deleted_saved_replies() == 1
    assert SavedReply.all_objects.filter(pk=recent.pk).exists()


def test_an_undo_for_someone_else_is_refused(owner, owner_client, client):
    from apps.conversations.models import SavedReply

    reply = SavedReply.objects.create(account=owner[1], title="Hours", body="x")
    resp = owner_client.post(f"/inbox/saved-replies/{reply.pk}/delete/", follow=True)
    undo = _undo_path(resp.content.decode())
    other = User.objects.create_user("mate", "mate@example.com", "x")
    Membership.objects.create(user=other, account=owner[1], role=Membership.Role.MEMBER)
    client.force_login(other)
    client.post(undo, HTTP_HX_REQUEST="true")
    assert not SavedReply.objects.filter(pk=reply.pk).exists()


def test_a_tampered_undo_is_refused(owner, owner_client):
    resp = owner_client.post("/undo/?t=nonsense", HTTP_HX_REQUEST="true")
    assert resp.status_code == 204 and "can%27t%20be%20undone" in resp["X-Toast"]


def test_pausing_a_rule_can_be_undone(owner, owner_client):
    from apps.billing.models import AccountFeatureOverride
    from apps.instagram.models.moderation import ModerationRule

    AccountFeatureOverride.objects.create(
        account=owner[1], key="instagram", grant=True, note="t"
    )
    rule = ModerationRule.objects.create(
        account=owner[1],
        name="Spam",
        match_type="keyword",
        keywords="crypto",
        moderation_action="hide",
    )
    resp = owner_client.post(
        f"/instagram/rules/moderation/{rule.pk}/toggle/", follow=True
    )
    rule.refresh_from_db()
    assert not rule.is_active
    owner_client.post(_undo_path(resp.content.decode()), HTTP_HX_REQUEST="true")
    rule.refresh_from_db()
    assert rule.is_active


def test_hiding_a_comment_is_undone_on_meta_too(owner, owner_client):
    """The Undo calls Meta to unhide (comments.moderate "unhide"), not just the local row."""
    from apps.conversations import undo_handlers

    calls = []
    with patch(
        "apps.conversations.comments.moderate", side_effect=lambda *a: calls.append(a)
    ):
        message = undo_handlers.comment_hide(
            type("R", (), {"user": owner[0]})(), owner[1], 7, {"channel": "instagram"}
        )
    assert calls == [(owner[1], "instagram", 7, "unhide", owner[0])]
    assert message == "Comment visible again."


def test_marking_a_follow_up_done_can_be_undone(owner):
    from apps.conversations import undo_handlers

    with patch("apps.conversations.actions.run_action") as run:
        from apps.conversations.models import FollowUp

        with patch.object(FollowUp.objects, "filter") as f:
            f.return_value.first.return_value = object()
            undo_handlers.followup_done(None, owner[1], 1, {})
    assert run.call_args.args[0] == "reopen_followup"
