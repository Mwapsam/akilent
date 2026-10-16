"""Reset staging publishing data for clean variant-based testing.

Only for staging environment. Preserves users, accounts, platform connections,
workspace configuration, and other unrelated data. Safely invalidates queued
Celery tasks referencing deleted records.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.publishing.models import ContentPost, ContentPostVariant


class Command(BaseCommand):
    help = "Reset staging publishing data (posts, variants, approval logs)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm deletion without prompting",
        )

    def handle(self, *args, **options):
        # Safety check: only allow in non-production environments
        import os

        from django.conf import settings

        # Check multiple environment indicators
        environment = getattr(
            settings, "ENVIRONMENT", os.getenv("ENVIRONMENT", "")
        ).lower()
        debug = getattr(settings, "DEBUG", False)

        # Reject if environment is explicitly production
        if environment == "production":
            raise CommandError(
                "Cannot reset publishing data in production. "
                "Set ENVIRONMENT='staging' or 'development' to proceed."
            )

        # Warn if no explicit environment and DEBUG is off (likely production)
        if not environment and not debug:
            raise CommandError(
                "Safety check failed: could not determine environment. "
                "Set ENVIRONMENT env var to 'staging' or 'development' explicitly."
            )

        confirm = options.get("confirm", False)

        if not confirm:
            self.stdout.write(
                "\n⚠️  This will DELETE all ContentPosts and ContentPostVariants."
            )
            self.stdout.write("   Queued Celery tasks may fail gracefully.\n")
            response = input("Type 'yes' to confirm: ").strip().lower()
            if response != "yes":
                self.stdout.write("Cancelled.")
                return

        with transaction.atomic():
            # Delete all variants (cascade to approval logs, etc.)
            variant_count, _ = ContentPostVariant.objects.all().delete()
            self.stdout.write(
                self.style.SUCCESS(f"✓ Deleted {variant_count} variant records")
            )

            # Delete all posts
            post_count, _ = ContentPost.objects.all().delete()
            self.stdout.write(
                self.style.SUCCESS(f"✓ Deleted {post_count} post records")
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\n✓ Publishing data reset complete.\n"
                "  Celery tasks referencing deleted posts will be handled gracefully.\n"
                "  Restart workers if desired: celery -A config worker restart\n"
            )
        )
