"""
Conversation service for website chat sessions.

Mirrors the other channels for the WEBSITE_CHAT channel: a visitor who writes is a customer, so
their first message gives the session a Contact (anonymous until they share details) and a
Conversation in the inbox. Each ChatSession has at most one Conversation (OneToOne).

Identity rule: details a visitor types are *self-reported*. They fill in the visitor's own
contact, but never merge the chat into another existing contact. When the email or phone already
belongs to someone else, it's kept on the visitor's contact as an unverified value for the team
to check.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from apps.chatbot.models import ChatSession
    from apps.contacts.models import Contact
    from apps.conversations.models import Conversation

logger = logging.getLogger(__name__)

CONTACT_SOURCE = "website_chat"
# Where a self-reported detail that already belongs to another contact is kept.
UNVERIFIED_EMAIL_ATTR = "website_chat_email"
UNVERIFIED_PHONE_ATTR = "website_chat_phone"

_HISTORY_LIMIT = 50


def _create_visitor_contact(session: ChatSession) -> Contact:
    from apps.contacts.models import Contact

    first, _, last = (session.visitor_name or "").strip().partition(" ")
    return Contact.objects.create(
        account=session.chatbot.account,
        source=CONTACT_SOURCE,
        first_name=first[:150],
        last_name=last.strip()[:150],
    )


def ensure_visitor_contact(session: ChatSession) -> Contact:
    """The session's contact, creating an anonymous one under a row lock if it has none."""
    from apps.chatbot.models import ChatSession

    if session.contact_id:
        return session.contact  # type: ignore[return-value]
    with transaction.atomic():
        locked = ChatSession.objects.select_for_update().get(pk=session.pk)
        if locked.contact_id is None:
            locked.contact = _create_visitor_contact(session)
            locked.save(update_fields=["contact", "last_activity_at"])
    session.contact = locked.contact
    session.contact_id = locked.contact_id
    return locked.contact  # type: ignore[return-value]


def get_or_create_for_website_chat(session: object) -> Conversation:
    """Return the Conversation for this session, creating it (and the visitor contact) if needed.

    Uses select_for_update inside a transaction so two concurrent first messages can't each
    create a separate Contact or Conversation for the same session.
    """
    from apps.chatbot.models import ChatSession
    from apps.conversations.models import Conversation

    assert isinstance(session, ChatSession)

    # Fast path: conversation already set (no lock needed).
    if session.conversation_id:
        return session.conversation  # type: ignore[return-value]

    created = False
    with transaction.atomic():
        # Re-read the session under a row lock so concurrent calls serialise here.
        locked = ChatSession.objects.select_for_update().get(pk=session.pk)
        if locked.conversation_id:
            # Another concurrent request already created it.
            conversation = locked.conversation
        else:
            if locked.contact_id is None:
                locked.contact = _create_visitor_contact(session)
            conversation = Conversation.objects.create(
                account=session.chatbot.account,
                contact=locked.contact,
                channel=Conversation.Channel.WEBSITE_CHAT,
                # Unread only once the visitor writes (register_inbound): a chat opened by a
                # "visitor opens the chat" greeting isn't waiting on anyone yet.
                is_unread=False,
            )
            locked.conversation = conversation
            locked.save(update_fields=["contact", "conversation", "last_activity_at"])
            created = True

    session.contact = locked.contact
    session.contact_id = locked.contact_id
    session.conversation = conversation
    session.conversation_id = conversation.pk  # type: ignore[union-attr]

    if created:
        # Same as every other channel: a new conversation goes to whoever the account's
        # routing rules say. Best-effort — routing never costs the visitor their message.
        try:
            from apps.conversations.services import route_new_conversation

            route_new_conversation(conversation)
        except Exception:
            logger.exception(
                "routing failed for website chat conversation %s", conversation.pk
            )
    return conversation  # type: ignore[return-value]


def _who(row: dict) -> str:
    from apps.conversations.models import Message

    if row["direction"] == Message.Direction.INBOUND:
        return "visitor"
    meta = row["metadata"] or {}
    if meta.get("sent_by") in ("ai", "automation") or meta.get("source") == "chatbot":
        return "bot"
    if meta.get("auto_kind") == "holding":
        return "bot"
    return "team"


def messages_after(
    session: ChatSession,
    cursor: int | None,
    *,
    after_message_id: int | None = None,
    limit: int = _HISTORY_LIMIT,
) -> tuple[list[dict], int]:
    """Messages for the widget, oldest first, and the cursor to ask from next time.

    The cursor is how many messages of this chat the widget has already been given (never a
    database id). ``cursor`` None or 0 returns the latest ``limit`` messages (a resumed chat);
    ``after_message_id`` returns what was said after that message (the reply to the visitor's
    own message, which the widget already shows). Each item is
    ``{"from": "visitor"|"bot"|"team", "text"}``: no ids, names or metadata.
    """
    if not session.conversation_id:
        return [], 0
    from apps.conversations.models import Message

    qs = Message.objects.filter(conversation_id=session.conversation_id).order_by("pk")
    total = qs.count()
    fields = ("direction", "body", "metadata")
    if after_message_id is not None:
        rows = list(qs.filter(pk__gt=after_message_id).values(*fields)[:limit])
    elif cursor:
        start = max(0, min(cursor, total))
        rows = list(qs.values(*fields)[start:total][:limit])
    else:
        rows = list(qs.values(*fields)[max(0, total - limit) : total])
    return [{"from": _who(r), "text": r["body"]} for r in rows if r["body"]], total


def history(session: ChatSession, *, limit: int = _HISTORY_LIMIT) -> list[dict]:
    """The chat so far for a resumed widget, oldest first."""
    return messages_after(session, None, limit=limit)[0]


def identify_visitor(
    session: ChatSession, *, name: str = "", email: str = "", phone: str = ""
) -> bool:
    """Save what the visitor told us on the session and on their own contact.

    Returns True when they gave a name or an email. A value is written to the contact only when
    the contact doesn't have one yet and no *other* contact in the account already uses it;
    otherwise it is kept as an unverified attribute, so a stranger typing a customer's email can
    never attach themselves to that customer's history.
    """
    from apps.contacts.models import Contact

    name, email, phone = name.strip()[:200], email.strip().lower()[:254], phone.strip()
    now = timezone.now()

    update_fields: list[str] = ["last_activity_at"]
    if name:
        session.visitor_name = name
        update_fields.append("visitor_name")
    if email:
        session.visitor_email = email
        update_fields.append("visitor_email")
    if phone:
        session.visitor_phone = phone[:30]
        update_fields.append("visitor_phone")
    identified = bool(name or email)
    if identified and session.identified_at is None:
        session.identified_at = now
        update_fields.append("identified_at")
    session.last_activity_at = now
    session.save(update_fields=update_fields)

    contact = ensure_visitor_contact(session)
    others = Contact.objects.filter(account_id=contact.account_id).exclude(
        pk=contact.pk
    )
    dirty: list[str] = []
    attributes = dict(contact.attributes or {})

    if name and not (contact.first_name or contact.last_name):
        first, _, last = name.partition(" ")
        contact.first_name, contact.last_name = first[:150], last.strip()[:150]
        dirty += ["first_name", "last_name"]

    if email and (contact.email or "").lower() != email:
        if not contact.email and not others.filter(email__iexact=email).exists():
            contact.email = email
            dirty.append("email")
        elif attributes.get(UNVERIFIED_EMAIL_ATTR) != email:
            attributes[UNVERIFIED_EMAIL_ATTR] = email

    if phone and contact.phone != phone:
        fits = len(phone) <= Contact._meta.get_field("phone").max_length
        if fits and not contact.phone and not others.filter(phone=phone).exists():
            contact.phone = phone
            dirty.append("phone")
        elif attributes.get(UNVERIFIED_PHONE_ATTR) != phone[:30]:
            attributes[UNVERIFIED_PHONE_ATTR] = phone[:30]

    if attributes != (contact.attributes or {}):
        contact.attributes = attributes
        dirty.append("attributes")
    if dirty:
        contact.save(update_fields=[*dirty, "updated_at"])
    return identified
