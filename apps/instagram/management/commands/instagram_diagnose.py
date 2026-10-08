"""Explain why Instagram replies on one inbox conversation fail.

    python manage.py instagram_diagnose conv_7de3fc9fe1e6ccf9ce3233f3

Read-only: prints which Instagram account and customer id a reply would use,
what the stored token says about itself, and whether that token can see the
customer. Never prints a token.
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.conversations.models import Conversation
from apps.instagram.models import InstagramBusinessAccount, InstagramConversation
from apps.instagram.models.message import InstagramMessage

GRAPH_IG = "https://graph.instagram.com"


class Command(BaseCommand):
    help = "Diagnose Instagram reply failures for one inbox conversation."

    def add_arguments(self, parser):
        parser.add_argument("public_id")

    def handle(self, *args, **options):
        conversation = Conversation.objects.filter(
            public_id=options["public_id"]
        ).first()
        if conversation is None:
            raise CommandError("No conversation with that id.")
        out = self.stdout.write

        out(f"Conversation {conversation.public_id} channel={conversation.channel}")
        links = conversation.channel_conversations.filter(
            channel=Conversation.Channel.INSTAGRAM
        ).order_by("pk")
        out(f"Instagram links (first one is used for replies): {links.count()}")
        for cc in links:
            ig_convo = (
                InstagramConversation.objects.select_related(
                    "instagram_account", "instagram_contact"
                )
                .filter(pk=cc.object_id)
                .first()
            )
            if ig_convo is None:
                out(f"  link {cc.pk}: points at missing InstagramConversation")
                continue
            out(
                f"  link {cc.pk}: ig_conversation={ig_convo.pk} open={ig_convo.is_open} "
                f"account_pk={ig_convo.instagram_account_id} "
                f"customer_igsid={ig_convo.instagram_contact.instagram_scoped_id}"
            )

        used = conversation.instagram_conversation
        if used is None:
            raise CommandError("No Instagram conversation linked.")
        igsid = used.instagram_contact.instagram_scoped_id

        out("\nInstagram accounts on this business:")
        for iba in InstagramBusinessAccount.objects.filter(
            account=conversation.account
        ).order_by("pk"):
            token = iba.access_token or ""
            out(
                f"  pk={iba.pk} ig_id={iba.instagram_business_account_id} "
                f"@{iba.username} active={iba.is_active} expired={iba.token_expired} "
                f"token={token[:4]}…({len(token)} chars) "
                f"{'<- used for replies' if iba.pk == used.instagram_account_id else ''}"
            )

        out("\nWhich business account received this customer's DMs (from webhooks):")
        received_by = set()
        for msg in InstagramMessage.objects.filter(
            conversation__instagram_contact=used.instagram_contact,
            direction=InstagramMessage.Direction.INBOUND,
        ).order_by("-pk")[:10]:
            meta = msg.metadata or {}
            received_by.add(
                (
                    (meta.get("recipient") or {}).get("id", "?"),
                    (meta.get("sender") or {}).get("id", "?"),
                )
            )
        for recipient, sender in sorted(received_by):
            out(f"  business ig_id={recipient}  customer sender_id={sender}")

        account = used.instagram_account
        version = getattr(settings, "INSTAGRAM_GRAPH_VERSION", "v21.0")
        token = account.access_token or ""
        out("\nWhat the reply token says about itself (GET /me):")
        out("  " + _get(f"{GRAPH_IG}/{version}/me", token, "id,user_id,username"))
        out(f"\nCan the reply token see the customer (GET /{igsid})?")
        out("  " + _get(f"{GRAPH_IG}/{version}/{igsid}", token, "name,username"))
        out(
            "\nReplies are sent to "
            f"POST /{account.instagram_business_account_id}/messages "
            f"recipient={igsid}"
        )


def _get(url: str, token: str, fields: str) -> str:
    try:
        resp = requests.get(
            url, params={"fields": fields, "access_token": token}, timeout=10
        )
    except requests.RequestException as exc:
        return f"request failed: {exc}"
    return f"HTTP {resp.status_code} {resp.text[:400]}"
