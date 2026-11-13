"""Classify older comments with the current audience classifier.

Signals only: no moderation, no messages, emails or leads. Safe to run twice: a
comment that already has a run for the current version and text is skipped.

    python manage.py audience_backfill --days 90
    python manage.py audience_backfill --account 12 --dry-run
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.audience import services
from apps.audience.classifier import CLASSIFIER_VERSION
from apps.audience.models import AudienceSignalRun


def _missing(qs, fk: str):
    has_run = AudienceSignalRun.objects.filter(
        **{fk: OuterRef("pk")},
        source=AudienceSignalRun.Source.RULE,
        classifier_version=CLASSIFIER_VERSION,
    )
    return (
        qs.filter(direction="inbound")
        .filter(
            body__regex=r"\S"
        )  # has some text; blank and whitespace-only are skipped
        .annotate(_has=Exists(has_run))
        .filter(_has=False)
        .order_by("timestamp")
    )


class Command(BaseCommand):
    help = "Classify older Instagram/Facebook comments into audience signals (no side effects)."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, help="Only this account id")
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--limit", type=int, default=5000, help="Per channel")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from apps.facebook.models.comment import FacebookComment
        from apps.instagram.models.comment import Comment

        since = timezone.now() - datetime.timedelta(days=opts["days"])
        ig = Comment.objects.filter(timestamp__gte=since).select_related(
            "thread__instagram_account"
        )
        fb = FacebookComment.objects.filter(timestamp__gte=since).select_related(
            "thread"
        )
        if opts["account"]:
            ig = ig.filter(thread__instagram_account__account_id=opts["account"])
            fb = fb.filter(thread__account_id=opts["account"])

        totals = {}
        for label, qs, fk in (
            ("instagram", ig, "ig_comment"),
            ("facebook", fb, "fb_comment"),
        ):
            todo = _missing(qs, fk)[: opts["limit"]]
            if opts["dry_run"]:
                totals[label] = todo.count()
                continue
            done = 0
            for comment in todo.iterator(chunk_size=500):
                if services.classify_comment(comment) is not None:
                    done += 1
            totals[label] = done

        if not services.enabled() and not opts["dry_run"]:
            self.stdout.write(
                self.style.WARNING("AUDIENCE_SIGNALS_ENABLED is off: nothing written.")
            )
        verb = "would classify" if opts["dry_run"] else "classified"
        self.stdout.write(
            f"{verb} {totals['instagram']} Instagram and {totals['facebook']} Facebook "
            f"comments ({CLASSIFIER_VERSION}, last {opts['days']} days)."
        )
