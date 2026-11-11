"""Add the media of existing posts to the media library (Phase 3, ``cs_media_library``).

    manage.py media_library_backfill [--dry-run]

Files uploaded before the library was released aren't in it. This records every image
and video URL on a post that is stored in Akilent (our own media host), once per
business. Files elsewhere are left out: the library only holds what Akilent stores.
It also decodes names an earlier run kept URL-encoded ("My%20photo.jpg"), and fills
what files are missing: a picture's small thumbnail for the grid, and file sizes
(media_library.fill_details; ``--no-details`` skips it). Safe to run again.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.publishing.constraints import media_kind
from apps.publishing.media_library import name_from_url, record, url_digest
from apps.publishing.models import ContentPost, MediaAsset


class Command(BaseCommand):
    help = "Record existing posts' media in the media library."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-details", action="store_true")

    def handle(self, *args, dry_run=False, no_details=False, **options):
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
        renamed = 0
        for asset in MediaAsset.objects.filter(name__contains="%").iterator():
            # Only names taken straight from the URL; a name someone typed is theirs.
            if asset.name != asset.url.rsplit("/", 1)[-1][:200]:
                continue
            renamed += 1
            if not dry_run:
                asset.name = name_from_url(asset.url)[:200]
                asset.save(update_fields=["name"])
        from django.db.models import Q

        from apps.publishing.media_library import fill_details

        missing = MediaAsset.objects.filter(
            Q(size_bytes__isnull=True) | Q(kind="image", thumb_url="")
        ).select_related("account")
        filled = 0
        if not no_details:
            for asset in missing.iterator():
                if dry_run or fill_details(asset):
                    filled += 1
        verb = "Would add" if dry_run else "Added"
        self.stdout.write(f"{verb} {added} file(s) to the media library.")
        if renamed:
            fixed = "Would fix" if dry_run else "Fixed"
            self.stdout.write(f"{fixed} the names of {renamed} file(s).")
        if filled:
            done = "Would fill" if dry_run else "Filled"
            self.stdout.write(f"{done} the thumbnail or size of {filled} file(s).")
