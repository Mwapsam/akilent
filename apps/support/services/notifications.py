"""Best-effort notification side-effects for support events.

Each channel is wrapped independently so one failure never blocks another.
All functions are called via transaction.on_commit() — never directly inside
an atomic block — so notifications only fire after the DB row is committed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def on_ticket_created(ticket) -> None:
    _slack(
        f":ticket: New support ticket *{ticket.ticket_number}* — {ticket.subject} "
        f"(T{ticket.customer_tier} / {ticket.priority.upper()})"
    )
    _email_submitted_by(ticket)


def on_escalation(ticket) -> None:
    _slack(
        f":escalate: *{ticket.ticket_number}* escalated to "
        f"{ticket.support_level.upper()} — {ticket.subject}"
    )


def on_sla_breach(ticket) -> None:
    _slack(
        f":alarm: SLA breached on *{ticket.ticket_number}* "
        f"(T{ticket.customer_tier}/{ticket.priority.upper()}) — {ticket.subject}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _slack(message: str) -> None:
    try:
        from apps.billing.slack import post_message  # reuse existing Slack utility

        post_message(message)
    except Exception:
        logger.exception("support notifications: slack post failed")


def _email_submitted_by(ticket) -> None:
    if ticket.submitted_by is None:
        return
    email = ticket.submitted_by.email
    if not email:
        return
    try:
        from apps.email.services.html_email import build_transactional_html
        from apps.email.services.send import send_system_email

        subject = f"Support request received — {ticket.ticket_number}"
        text_body = (
            f"Hi,\n\n"
            f"We've received your support request:\n\n"
            f"  #{ticket.ticket_number}: {ticket.subject}\n\n"
            f"We'll be in touch shortly.\n\n"
            f"Akilent Support"
        )
        from django.urls import reverse

        from apps.email.services.html_email import site_email_context

        portal_url = site_email_context()["base_url"] + reverse(
            "support:portal-ticket", kwargs={"ticket_number": ticket.ticket_number}
        )
        send_system_email(
            to_email=email,
            subject=subject,
            text_body=text_body,
            html_body=build_transactional_html(
                "Thanks for getting in touch. We've received your request and will reply "
                "as soon as we can.",
                subject,
                eyebrow="Support",
                heading="We've received your request",
                preheader=f"#{ticket.ticket_number}: {ticket.subject}",
                details=[
                    ("Ticket", f"#{ticket.ticket_number}"),
                    ("Subject", ticket.subject),
                ],
                cta_url=portal_url,
                cta_label="View your request",
            ),
            sender_label="Support",
        )
    except Exception:
        logger.exception(
            "support notifications: email to submitted_by failed ticket=%s", ticket.pk
        )
