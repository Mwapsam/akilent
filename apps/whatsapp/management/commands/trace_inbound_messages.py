"""Read-only: how far did each inbound WhatsApp message get through the pipeline?

    poetry run python manage.py trace_inbound_messages --account <id|slug> [--json]

For every inbound MessageLog row of ONE account it reports the stage reached and a
verdict, so messages that never appeared in the inbox can be explained one by one:

    projected                    the inbox has it
    logged_not_projected         webhook event was processed and a MessageLog exists, but no
                                 inbox message (projection failed, or it predates projection)
    event_not_processed          the stored webhook event was never marked processed
    event_error                  the stored webhook event recorded an error
    logged_without_stored_event  a MessageLog exists but no webhook event mentions its id
                                 (replayed by hand, or the event log was pruned)

It cannot see messages that never reached Akilent at all (Meta never delivered the
webhook); those leave no row here. It also lists message webhook events that are still
unprocessed. WebhookEventLog has no account column, so that list spans all tenants.

No message content or phone numbers are printed, and nothing is written.
"""
import json
from collections import Counter

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from apps.whatsapp.models import MessageLog, WebhookEventLog

_ERROR_LIMIT = 160
_ORPHAN_LIMIT = 50


def _iso(value):
    return value.isoformat() if value else None


def trace_account(account) -> dict:
    Message = apps.get_model("conversations", "Message")  # no cross-app model import

    rows = []
    logs = (
        MessageLog.objects.filter(account_id=account.id, direction=MessageLog.Direction.INBOUND)
        .order_by("timestamp", "id")
    )
    for log in logs:
        events = []
        if log.message_id:
            events = list(
                WebhookEventLog.objects.filter(payload__icontains=log.message_id)
                .values("processed", "attempts", "error_message")
            )
        spine = (
            Message.objects.filter(whatsapp_message=log).select_related("conversation").first()
        )
        errors = [e["error_message"] for e in events if e["error_message"]]

        if spine is not None:
            verdict = "projected"
        elif not events:
            verdict = "logged_without_stored_event"
        elif any(e["processed"] for e in events):
            verdict = "logged_not_projected"
        elif errors:
            verdict = "event_error"
        else:
            verdict = "event_not_processed"

        rows.append({
            "message_id": log.message_id,
            "timestamp": _iso(log.timestamp),
            "logged_at": _iso(log.created_at),
            "event_stored": bool(events),
            "event_count": len(events),
            "event_attempts": max((e["attempts"] for e in events), default=0),
            "event_error": errors[0][:_ERROR_LIMIT] if errors else None,
            "projected": spine is not None,
            "conversation_id": spine.conversation.public_id if spine is not None else None,
            "verdict": verdict,
        })

    orphans = [
        {"event_id": e["id"], "created_at": _iso(e["created_at"]), "attempts": e["attempts"],
         "error": (e["error_message"] or "")[:_ERROR_LIMIT] or None}
        for e in WebhookEventLog.objects.filter(event_type="message", processed=False)
        .order_by("-created_at").values("id", "created_at", "attempts", "error_message")[:_ORPHAN_LIMIT]
    ]
    return {
        "account": {"id": account.id, "slug": account.slug},
        "summary": dict(Counter(r["verdict"] for r in rows)),
        "messages": rows,
        "unprocessed_message_events": orphans,
        "unprocessed_message_events_scope": "all tenants (WebhookEventLog has no account column)",
    }


class Command(BaseCommand):
    help = "Read-only trace of inbound WhatsApp messages through the pipeline."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, help="Account id or slug.")
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    def handle(self, *args, **options):
        Account = apps.get_model("accounts", "Account")
        ref = options["account"]
        account = (Account.objects.filter(id=int(ref) if ref.isdigit() else None).first()
                   or Account.objects.filter(slug=ref).first())
        if account is None:
            raise CommandError(f"No account matches {ref!r}")

        data = trace_account(account)
        if options["json"]:
            self.stdout.write(json.dumps(data, indent=2, sort_keys=True))
            return

        self.stdout.write(f"Account {account.slug} (id {account.id}): "
                          f"{len(data['messages'])} inbound message(s)")
        for verdict, n in sorted(data["summary"].items()):
            self.stdout.write(f"  {verdict}: {n}")
        self.stdout.write("")
        self.stdout.write(f"{'message_id':<44} {'logged_at':<26} {'event':<6} {'tries':<5} verdict")
        for r in data["messages"]:
            self.stdout.write(
                f"{(r['message_id'] or '-'):<44} {(r['logged_at'] or '-')[:25]:<26} "
                f"{'yes' if r['event_stored'] else 'no':<6} {r['event_attempts']:<5} {r['verdict']}"
                + (f"  [{r['event_error']}]" if r["event_error"] else "")
            )
        self.stdout.write("")
        self.stdout.write(f"Unprocessed message events ({data['unprocessed_message_events_scope']}): "
                          f"{len(data['unprocessed_message_events'])}")
        for e in data["unprocessed_message_events"]:
            self.stdout.write(f"  event {e['event_id']} {e['created_at']} attempts={e['attempts']}"
                              + (f" error={e['error']}" if e["error"] else ""))
