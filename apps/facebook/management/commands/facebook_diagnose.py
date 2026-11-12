"""Explain what happened to the latest Facebook webhook events.

    python manage.py facebook_diagnose            # the last 10 events
    python manage.py facebook_diagnose --limit 30

Read-only: for each event, prints the step it stopped at — no business, Page not
connected, no Facebook inbox, a comment the Page made itself, no rule covering
Facebook that matches, or the private reply's send result. Never prints a token.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.billing import api as billing_api
from apps.facebook.models import FacebookCommentThread
from apps.facebook.models.message import FacebookOutboundMessage
from apps.facebook.models.webhook import FacebookWebhookEventLog
from apps.facebook.services.inbound import _extract_comment_changes
from apps.instagram.api import CommentTrigger, RuleChannels, first_matching_trigger


class Command(BaseCommand):
    help = "Diagnose why Facebook comments or comment-to-DM did nothing."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **options):
        out = self.stdout.write
        events = FacebookWebhookEventLog.objects.select_related("account").order_by(
            "-received_at"
        )[: options["limit"]]
        if not events:
            out("No Facebook webhook events stored. Meta is not delivering any.")
            return
        for event in events:
            out("")
            out(
                f"event {event.pk} {event.received_at:%Y-%m-%d %H:%M:%S} "
                f"type={event.event_type} status={event.status} page={event.page_id} "
                f"attempts={event.attempts}"
            )
            if event.error_message:
                out(f"  error: {event.error_message[:300]}")
            self._explain(event, out)

    def _explain(self, event, out):
        if event.account is None:
            out(
                "  STOP: no business has this Page connected (a dashboard Test "
                "sample, or a Page connected elsewhere)."
            )
            return
        from apps.publishing.models import FacebookPageConnection

        account = event.account
        conn = FacebookPageConnection.objects.filter(account=account).first()
        if conn is None or conn.page_id != event.page_id:
            out("  STOP: the business's connected Page is a different one.")
            return
        out(
            f"  business={account.pk} page_status={conn.status} "
            f"subscribed={'yes' if conn.webhook_subscribed_at else 'NO'}"
            f"{' webhook_error=' + conn.webhook_error if conn.webhook_error else ''}"
        )
        if not billing_api.usable(account, "facebook"):
            out(
                "  STOP: the business can't use the 'facebook' feature (add it to "
                "the plan or grant it)."
            )
            return
        if event.event_type != FacebookWebhookEventLog.EventType.COMMENT:
            return

        changes = _extract_comment_changes(event.raw_payload)
        if not changes:
            out("  STOP: no comment in this feed event (a post, reaction or like).")
        for value in changes:
            self._explain_comment(account, conn, value, out)

    def _explain_comment(self, account, conn, value, out):
        commenter = value.get("from") or {}
        comment_id = value.get("comment_id", "")
        post_id = value.get("post_id", "")
        parent = value.get("parent_id", "") or ""
        body = value.get("message", "") or ""
        out(
            f"  comment {comment_id} verb={value.get('verb')} "
            f"from={commenter.get('id', '-')} ({commenter.get('name', '')}) "
            f"reply={'yes' if parent not in {'', post_id} else 'no'} text={body[:80]!r}"
        )
        if value.get("verb") != "add":
            out("  STOP: not a new comment (edit, hide or remove).")
            return
        if not commenter.get("id"):
            out(
                "  STOP: Meta sent no commenter ('from'). Without advanced access to "
                "the Page permissions, Meta leaves it out for people with no role on "
                "the app."
            )
            return
        if commenter["id"] == conn.page_id:
            out(
                "  STOP: the Page itself commented. Comment as a person (not 'as "
                "the Page') to test."
            )
            return
        if parent not in {"", post_id}:
            out("  STOP: a reply to a comment; rules only answer top-level comments.")
            return

        thread = FacebookCommentThread.objects.filter(
            account=account, comment_id=comment_id
        ).first()
        out(
            f"  stored thread: {'yes' if thread else 'NO'}"
            + (
                f" trigger_fired_at={thread.trigger_fired_at}"
                f" conversation={thread.conversation_id}"
                if thread
                else ""
            )
        )

        covering = RuleChannels.covering("facebook")
        active = CommentTrigger.objects.filter(account=account, is_active=True)
        out(
            f"  reply rules: {active.filter(channels__in=covering).count()} cover "
            f"Facebook, {active.exclude(channels__in=covering).count()} are "
            "Instagram-only"
        )
        trigger = first_matching_trigger(
            account,
            "facebook",
            body,
            variant=thread.content_variant if thread else None,
        )
        if trigger is None:
            out(
                "  STOP: no active reply rule covering Facebook matches this text. "
                "Rules made before Facebook was added are Instagram-only: edit the "
                "rule and set 'Applies to comments on' to include Facebook."
            )
            return
        out(f"  matching rule: {trigger.name!r} ({trigger.reply_type})")

        outbound = FacebookOutboundMessage.objects.filter(
            idempotency_key=f"private_reply:fb:{account.pk}:{comment_id}"
        ).first()
        if outbound is None:
            out("  STOP: no reply was queued (the event may not have been processed).")
            return
        out(
            f"  reply: status={outbound.status} attempts={outbound.attempts} "
            f"sent_at={outbound.sent_at}"
        )
        if outbound.last_error:
            out(f"  reply error: {outbound.last_error[:300]}")
