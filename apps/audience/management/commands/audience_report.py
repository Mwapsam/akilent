"""Read-only audience diagnostics for one account (spec §10 data quality, §11 pilot sample).

    python manage.py audience_report --account 12 --days 28

Prints: how many comments and commenters, how many have signals, the "general" share
(an investigation trigger, not a gate), the language mix, people per topic, and the
comment-level coverage and outcomes (a private reply to the comment opened a
conversation; leads and paid orders credited to the comment's post). Writes nothing.
"""

from __future__ import annotations

import datetime
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.utils import timezone

from apps.audience import services
from apps.audience.models import AudienceSignalRun, Channel
from apps.audience.taxonomy import label, precedence_rank


class Command(BaseCommand):
    help = "Audience signals for an account: coverage, language mix, topics, outcomes (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, required=True)
        parser.add_argument("--days", type=int, default=28)
        parser.add_argument("--limit", type=int, default=20000, help="Per channel")

    def handle(self, *args, **opts):
        from apps.accounts.models import Account
        from apps.conversations.models import ConversationAttribution
        from apps.facebook.models.comment import FacebookComment
        from apps.instagram.models.comment import Comment

        account = Account.objects.filter(pk=opts["account"]).first()
        if account is None:
            raise CommandError("No such account.")
        since = timezone.now() - datetime.timedelta(days=opts["days"])

        rows = []  # (channel, pk, body, commenter, conversation_id, post_id)
        for channel, qs, contact in (
            (
                Channel.INSTAGRAM,
                Comment.objects.filter(thread__instagram_account__account=account),
                "instagram_contact_id",
            ),
            (
                Channel.FACEBOOK,
                FacebookComment.objects.filter(thread__account=account),
                "facebook_contact_id",
            ),
        ):
            for c in (
                qs.filter(direction="inbound", timestamp__gte=since)
                .exclude(moderation_state="deleted")
                .select_related("thread__content_variant")[: opts["limit"]]
            ):
                root = c.comment_id == c.thread.comment_id
                variant = c.thread.content_variant
                rows.append(
                    (
                        channel,
                        c.pk,
                        c.body or "",
                        (channel, getattr(c, contact)),
                        c.thread.conversation_id if root else None,
                        variant.post_id if variant else None,
                    )
                )
        out = self.stdout.write
        out(f"{account} · last {opts['days']} days")
        if not rows:
            out("No inbound comments in this window.")
            return

        signals = services.signals_for_rows((r[0], r[1], r[2]) for r in rows)
        fk_ids = {"instagram": [], "facebook": []}
        for r in rows:
            fk_ids[r[0]].append(r[1])
        languages = Counter()
        classified = 0
        for channel, fk in (("instagram", "ig_comment"), ("facebook", "fb_comment")):
            if not fk_ids[channel]:
                continue
            runs = AudienceSignalRun.objects.filter(
                **{f"{fk}__in": fk_ids[channel]}, source="rule"
            )
            classified += runs.values(fk).distinct().count()
            for row in runs.values("language").annotate(n=Count("id")):
                languages[row["language"] or "unknown"] += row["n"]

        people = {r[3] for r in rows}
        per_topic: dict[str, set] = {}
        general_only = 0
        linked: dict[str, dict] = {}
        for r in rows:
            cats = {s.category for s in signals.get((r[0], r[1]), [])}
            if cats <= {"general"}:
                general_only += 1
            for cat in cats:
                per_topic.setdefault(cat, set()).add(r[3])
                if r[4] and r[5]:
                    linked.setdefault(cat, {})[r[3]] = (r[4], r[5])

        out(f"Comments: {len(rows)} from {len(people)} people")
        out(
            f"With signals: {classified} of {len(rows)} (run audience_backfill for the rest)"
        )
        out(
            f"Nothing specific (general only): {general_only} "
            f"({round(100 * general_only / len(rows))}%)"
        )
        out(
            "Language (rule runs): "
            + ", ".join(f"{k} {v}" for k, v in languages.most_common())
        )
        out("")
        out("Topic: people · linked to a conversation · leads · paid orders")
        for cat in sorted(per_topic, key=precedence_rank):
            pairs = set(linked.get(cat, {}).values())
            leads = orders = 0
            if pairs:
                q = Q()
                for conv, post in pairs:
                    q |= Q(conversation_id=conv, source_post_id=post)
                attributions = ConversationAttribution.objects.filter(q)
                leads = attributions.filter(lead__isnull=False).count()
                orders = attributions.filter(order__status="paid").count()
            out(
                f"  {label(cat)}: {len(per_topic[cat])} · {len(linked.get(cat, {}))} "
                f"· {leads} · {orders}"
            )
        out("")
        out(
            "Linked = a private reply to the person's own comment opened a conversation; "
            "outcomes count only leads/orders credited to that comment's post (lower bound)."
        )
