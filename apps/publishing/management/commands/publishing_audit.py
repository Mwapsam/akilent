"""Read-only pre-deploy audit of publishing data (Step 4.7D).

Variant-only publishing refuses posts that have no variants, so before deploying run:

    python manage.py publishing_audit

It changes nothing. Exit code 1 means something needs a decision before deploy.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
)

# Posts in these states will be picked up by the publishing pipeline.
_ACTIVE_POST_STATUSES = (
    ContentPost.Status.SCHEDULED,
    ContentPost.Status.PUBLISHING,
    ContentPost.Status.APPROVED,
)


class Command(BaseCommand):
    help = "Report publishing data that needs attention before a deploy (read-only)"

    def handle(self, *args, **options):
        now = timezone.now()
        blockers = 0

        without_variants = ContentPost.objects.annotate(n=Count("variants")).filter(n=0)
        active_orphans = without_variants.filter(status__in=_ACTIVE_POST_STATUSES)
        self._line("Posts without variants", without_variants.count())
        self._line(
            "  …of which scheduled/publishing/approved (BLOCKER: cannot publish)",
            active_orphans.count(),
        )
        for post in active_orphans[:20]:
            self.stdout.write(
                f"    post={post.pk} account={post.account_id} status={post.status}"
            )
        blockers += active_orphans.count()

        self.stdout.write("\nVariants by status:")
        for row in (
            ContentPostVariant.objects.values("status")
            .annotate(n=Count("pk"))
            .order_by("status")
        ):
            self._line(f"  {row['status']}", row["n"])

        stuck_publishing = ContentPostVariant.objects.filter(
            status=ContentPostVariant.Status.PUBLISHING,
            updated_at__lte=now - timedelta(hours=6),
        ).count()
        old_processing = ContentPostVariant.objects.filter(
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at__lte=now - timedelta(hours=48),
        ).count()
        reconciling = ContentPostVariant.objects.filter(
            status=ContentPostVariant.Status.RECONCILING
        ).count()
        self.stdout.write("")
        self._line("Variants PUBLISHING for 6+ hours", stuck_publishing)
        self._line("Variants PROCESSING for 48+ hours", old_processing)
        self._line("Variants awaiting reconciliation", reconciling)
        self._line(
            "Facebook connections needing reconnect",
            FacebookPageConnection.objects.filter(
                status=FacebookPageConnection.Status.EXPIRED
            ).count(),
        )
        # Pages connected before the long-lived token exchange got a token that expired
        # within hours; they must be reconnected once after this release.
        self._line(
            "Active Facebook connections (reconnect once if connected before this release)",
            FacebookPageConnection.objects.filter(
                status=FacebookPageConnection.Status.ACTIVE
            )
            .exclude(access_token__isnull=True)
            .count(),
        )

        if blockers:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{blockers} active post(s) have no variants. Recreate or cancel "
                    "them before deploying."
                )
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nNo deploy blockers found."))

    def _line(self, label: str, value) -> None:
        self.stdout.write(f"{label}: {value}")
