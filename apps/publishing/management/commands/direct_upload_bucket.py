"""Read-only check of the media bucket for direct uploads (Phase 0c, ``cs_direct_upload``).

    manage.py direct_upload_bucket

The browser posts files straight to the bucket, so it needs a CORS rule allowing POST
from the app's origin, and a lifecycle rule deleting uploads never confirmed (under
``<media>/pending/``) after a day. This command reports whether both exist and prints
the rules to add. It changes nothing: putting a CORS configuration replaces the whole
existing one, so the rules are merged by hand.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.publishing import direct_upload


def _origin() -> str:
    parsed = urlparse(settings.SITE_URL)
    return f"{parsed.scheme}://{parsed.netloc}"


def required_cors() -> dict:
    return {
        "AllowedOrigins": [_origin()],
        "AllowedMethods": ["POST"],
        "AllowedHeaders": ["*"],
        "MaxAgeSeconds": 3000,
    }


def required_lifecycle() -> dict:
    return {
        "ID": "akilent-direct-upload-pending",
        "Filter": {"Prefix": f"{direct_upload._root()}/pending/"},
        "Status": "Enabled",
        "Expiration": {"Days": 1},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }


def _has_cors(rules: list) -> bool:
    return any(
        "POST" in r.get("AllowedMethods", [])
        and (
            _origin() in r.get("AllowedOrigins", [])
            or "*" in r.get("AllowedOrigins", [])
        )
        for r in rules
    )


def _has_lifecycle(rules: list) -> bool:
    prefix = required_lifecycle()["Filter"]["Prefix"]
    for r in rules:
        rule_prefix = (r.get("Filter") or {}).get("Prefix", r.get("Prefix"))
        days = (r.get("Expiration") or {}).get("Days")
        if (
            r.get("Status") == "Enabled"
            and rule_prefix == prefix
            and days is not None
            and days <= 1
        ):
            return True
    return False


class Command(BaseCommand):
    help = "Check the media bucket's CORS and lifecycle rules for direct uploads."

    def handle(self, *args, **options):
        from botocore.exceptions import ClientError

        bucket = settings.AWS_STORAGE_BUCKET_NAME
        if not settings.USE_S3_STORAGE or not bucket:
            self.stdout.write("S3 storage isn't in use; direct uploads aren't offered.")
            return
        client = direct_upload._client()

        def rules(call, key):
            try:
                return call(Bucket=bucket).get(key) or []
            except ClientError as exc:
                code = (exc.response.get("Error") or {}).get("Code", "")
                if code in ("NoSuchCORSConfiguration", "NoSuchLifecycleConfiguration"):
                    return []
                raise

        cors_ok = _has_cors(rules(client.get_bucket_cors, "CORSRules"))
        life_ok = _has_lifecycle(
            rules(client.get_bucket_lifecycle_configuration, "Rules")
        )
        self.stdout.write(f"Bucket: {bucket}")
        self.stdout.write(
            f"CORS POST from {_origin()}: {'ok' if cors_ok else 'MISSING'}"
        )
        self.stdout.write(
            f"Pending uploads deleted after a day: {'ok' if life_ok else 'MISSING'}"
        )
        if not cors_ok:
            self.stdout.write("\nAdd this CORS rule (keep the existing ones):")
            self.stdout.write(json.dumps(required_cors(), indent=2))
        if not life_ok:
            self.stdout.write("\nAdd this lifecycle rule (keep the existing ones):")
            self.stdout.write(json.dumps(required_lifecycle(), indent=2))
        if cors_ok and life_ok:
            self.stdout.write("Ready for direct uploads.")
