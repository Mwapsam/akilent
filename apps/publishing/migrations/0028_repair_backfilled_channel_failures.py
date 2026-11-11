"""Give each channel of an old failed post its own outcome.

0005 made the channels of every existing post by copying the post's status onto each
one, but not its error. An old post that failed on Instagram therefore shows every
channel as failed with "No reason was recorded", including a Facebook channel that went
live (it has a post ID). The old error reads "Instagram: …; Facebook: …", so each
channel can take back its own part.
"""

import re

from django.db import migrations

_PREFIX = re.compile(r"(?:^|;\s*)(Instagram|Facebook):\s*", re.IGNORECASE)
NO_RECORD = (
    "This post failed before Akilent saved each channel's result, so there's no "
    "reason for this channel. Retry it to see what happens now."
)


def split_error(error: str) -> dict:
    """{"instagram": "…", "facebook": "…"} from "Instagram: …; Facebook: …"."""
    parts = {}
    matches = list(_PREFIX.finditer(error or ""))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(error)
        parts[m.group(1).lower()] = error[m.end() : end].strip().rstrip(";").strip()
    return parts


def repair(apps, schema_editor):
    Variant = apps.get_model("publishing", "ContentPostVariant")
    rows = Variant.objects.filter(status="failed", last_error="").select_related("post")
    for variant in rows.iterator():
        post = variant.post
        if variant.publication_id:
            # It went live: the post's failure was another channel's.
            variant.status = "published"
            variant.published_at = (
                variant.published_at or post.published_at or post.updated_at
            )
            variant.save(update_fields=["status", "published_at"])
            continue
        parts = split_error(post.error)
        reason = parts.get(variant.platform) or (
            post.error.strip() if post.error.strip() and not parts else NO_RECORD
        )
        variant.last_error = reason[:2000]
        variant.last_error_type = (
            "validation" if "ratio" in reason.lower() else "unknown"
        )
        variant.last_error_at = variant.last_error_at or post.updated_at
        variant.save(update_fields=["last_error", "last_error_type", "last_error_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0027_review_fixes"),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
