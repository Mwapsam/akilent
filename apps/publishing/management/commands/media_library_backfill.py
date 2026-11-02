"""Add the media of existing posts to the media library (Phase 3, ``cs_media_library``).

    manage.py media_library_backfill [--dry-run]

Files uploaded before the library was released aren't in it. This records every image
and video URL on a post that is stored in Akilent (our own media host), once per
business. Files elsewhere are left out: the library only holds what Akilent stores.
Safe to run again.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.publishing.constraints import media_kind
from apps.publishing.media_library import record, url_digest
from apps.publishing.models import ContentPost, MediaAsset


class Command(BaseCommand):
    help = "Record existing posts' media in the media library."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, dry_run=False, **options):
        from apps.publishing.services import _is_own_media_url

        added = 0
        posts = ContentPost.objects.exclude(media_urls=[]).select_related("account")
        for post in posts.iterator():
            for url in post.media_urls or []:
                if not isinstance(url, str) or not _is_own_media_url(url):
                    continue
                if MediaAsset.objects.filter(
                    account_id=post.account_id, url_digest=url_digest(url)
                ).exists():
                    continue
                added += 1
                if not dry_run:
                    record(post.account, url, kind=media_kind(url))
        verb = "Would add" if dry_run else "Added"
        self.stdout.write(f"{verb} {added} file(s) to the media library.")
