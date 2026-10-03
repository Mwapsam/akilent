"""
Tests for apps.chatbot.analytics — pure DB reads, no HTTP.

Covers:
  - session_summary counts and identification_rate
  - action_summary grouping and success_rate
  - tickets_from_chat counts
  - window filtering (events outside the window are excluded)
  - account isolation (another chatbot's data doesn't bleed in)
"""

from datetime import timedelta

from django.utils import timezone

from apps.chatbot.analytics import (
    action_summary,
    period_range,
    session_summary,
    tickets_from_chat,
)
from apps.chatbot.models import ChatActionExecution, ChatbotAction, ChatSession

# ── Helpers ────────────────────────────────────────────────────────────────────


def _window():
    """Returns (start, end) wide enough to cover any fixture created in this test."""
    return period_range(30)


# ── Session summary ────────────────────────────────────────────────────────────


class TestSessionSummary:
    def test_empty(self, chatbot, db):
        start, end = _window()
        s = session_summary(chatbot, start, end)
        assert s["total"] == 0
        assert s["identified"] == 0
        assert s["identification_rate"] == 0

    def test_counts_total_sessions(self, chatbot, db):
        ChatSession.objects.create(chatbot=chatbot)
        ChatSession.objects.create(chatbot=chatbot)
        start, end = _window()
        s = session_summary(chatbot, start, end)
        assert s["total"] == 2

    def test_counts_identified(self, chatbot, account, db):
        from apps.contacts.models import Contact

        contact = Contact.objects.create(account=account, email="a@example.com")
        ChatSession.objects.create(chatbot=chatbot, contact=contact)
        ChatSession.objects.create(chatbot=chatbot)  # unidentified
        start, end = _window()
        s = session_summary(chatbot, start, end)
        assert s["total"] == 2
        assert s["identified"] == 1
        assert s["identification_rate"] == 50

    def test_excludes_sessions_outside_window(self, chatbot, db):
        session = ChatSession.objects.create(chatbot=chatbot)
        # Push started_at before the window.
        ChatSession.objects.filter(pk=session.pk).update(
            started_at=timezone.now() - timedelta(days=60)
        )
        start, end = _window()
        s = session_summary(chatbot, start, end)
        assert s["total"] == 0

    def test_account_isolation(self, chatbot, chatbot_no_domains, db):
        ChatSession.objects.create(chatbot=chatbot_no_domains)
        start, end = _window()
        s = session_summary(chatbot, start, end)
        assert s["total"] == 0


# ── Action summary ─────────────────────────────────────────────────────────────


class TestActionSummary:
    def test_empty(self, chatbot, db):
        start, end = _window()
        assert action_summary(chatbot, start, end) == []

    def test_counts_per_status(self, chatbot, action, session, db):
        for status in (
            ChatActionExecution.Status.SUCCESS,
            ChatActionExecution.Status.SUCCESS,
            ChatActionExecution.Status.FAILED,
            ChatActionExecution.Status.DENIED,
        ):
            ChatActionExecution.objects.create(
                session=session, action=action, status=status
            )

        start, end = _window()
        rows = action_summary(chatbot, start, end)
        assert len(rows) == 1
        r = rows[0]
        assert r["slug"] == action.slug
        assert r["total"] == 4
        assert r["success"] == 2
        assert r["failed"] == 1
        assert r["denied"] == 1
        assert r["success_rate"] == 50

    def test_excludes_outside_window(self, chatbot, action, session, db):
        exec_ = ChatActionExecution.objects.create(
            session=session, action=action, status=ChatActionExecution.Status.SUCCESS
        )
        ChatActionExecution.objects.filter(pk=exec_.pk).update(
            created_at=timezone.now() - timedelta(days=60)
        )
        start, end = _window()
        assert action_summary(chatbot, start, end) == []

    def test_account_isolation(self, chatbot, chatbot_no_domains, session, db):
        other_action = ChatbotAction.objects.create(
            chatbot=chatbot_no_domains, slug="other", label="Other", is_enabled=True
        )
        other_session = ChatSession.objects.create(chatbot=chatbot_no_domains)
        ChatActionExecution.objects.create(
            session=other_session,
            action=other_action,
            status=ChatActionExecution.Status.SUCCESS,
        )
        start, end = _window()
        assert action_summary(chatbot, start, end) == []


# ── Tickets from chat ──────────────────────────────────────────────────────────


class TestTicketsFromChat:
    def test_zero_when_no_tickets(self, chatbot, db):
        start, end = _window()
        assert tickets_from_chat(chatbot, start, end) == 0

    def test_counts_website_chat_events(self, chatbot, account, db):
        from apps.support.models import SupportEvent, SupportTicket

        ticket = SupportTicket.objects.create(
            account=account,
            subject="From chat",
            status=SupportTicket.NEW,
            priority="p3",
        )
        SupportEvent.objects.create(
            ticket=ticket,
            event_type=SupportEvent.CREATED_FROM_CONVERSATION,
            metadata={"channel": "website_chat", "conversation_id": 1},
        )
        start, end = _window()
        assert tickets_from_chat(chatbot, start, end) == 1

    def test_excludes_non_website_chat(self, chatbot, account, db):
        from apps.support.models import SupportEvent, SupportTicket

        ticket = SupportTicket.objects.create(
            account=account,
            subject="From WA",
            status=SupportTicket.NEW,
            priority="p3",
        )
        SupportEvent.objects.create(
            ticket=ticket,
            event_type=SupportEvent.CREATED_FROM_CONVERSATION,
            metadata={"channel": "whatsapp", "conversation_id": 2},
        )
        start, end = _window()
        assert tickets_from_chat(chatbot, start, end) == 0
