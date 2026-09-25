"""Tell the team something: a plain email to the right teammates, with a link.

Deliberately email, not a new in-app notification system: it works today with the system
mailer, needs no new tables, and reaches people who are not looking at Akilent. Recipients
are always members of the business itself: "owners" (owners and admins), "assignee" (whoever
the conversation is assigned to, falling back to owners so nobody is silently skipped), or one
named teammate's email address. Nothing here can email a customer or an outsider.
"""
from __future__ import annotations

import logging

from django.conf import settings

from apps.accounts.models import Membership

logger = logging.getLogger(__name__)

MAX_TEXT = 1000
_MANAGER_ROLES = (Membership.Role.OWNER, Membership.Role.ADMIN)


class NotifyError(ValueError):
    """A notification that cannot be addressed, worded for a business owner."""


def absolute_url(path: str) -> str:
    """A full link to ``path`` on this site (workflows run outside any request)."""
    domain = getattr(settings, "BASE_DOMAIN", "") or "localhost"
    local = domain.startswith(("localhost", "127.", "0.0.0.0"))
    return f"{'http' if local else 'https'}://{domain}{path}"


def _members(account):
    return Membership.objects.filter(account=account, user__is_active=True).select_related("user").order_by("id")


def recipients(account, to: str, *, conversation=None) -> list:
    """The teammates ``to`` means: "owners", "assignee", or a member's email address."""
    to = (to or "owners").strip()
    if to == "owners":
        return [m.user for m in _members(account) if m.role in _MANAGER_ROLES]
    if to == "assignee":
        if conversation is not None and conversation.assigned_to_id and conversation.assigned_to.is_active:
            return [conversation.assigned_to]
        return recipients(account, "owners")
    if "@" in to:
        member = _members(account).filter(user__email__iexact=to).first()
        if member is None:
            raise NotifyError(f"{to} isn't on your team.")
        return [member.user]
    raise NotifyError("Choose who to tell: your owners, the assigned teammate, or a teammate's email.")


def notify_team(account, *, to: str, subject: str, text: str, path: str = "", conversation=None) -> int:
    """Email ``text`` (with a link to ``path``) to the chosen teammates. Returns how many were sent.

    A delivery failure for one person is logged and skipped: one bad address must not stop the
    rest, or the workflow that asked.
    """
    from apps.email.services.send import send_system_email

    text = (text or "").strip()
    if not text:
        raise NotifyError("Write what the team should be told.")
    body = text[:MAX_TEXT]
    if path:
        body += f"\n\nOpen it: {absolute_url(path)}"
    body += "\n\nYou're getting this because an automation in Akilent was set up to tell your team."

    sent = 0
    for user in recipients(account, to, conversation=conversation):
        if not user.email:
            continue
        try:
            send_system_email(to_email=user.email, subject=subject[:200], text_body=body)
            sent += 1
        except Exception:
            logger.exception("notify_team: could not email user=%s", user.pk)
    return sent
