"""The permissions each connect flow asks for are exactly the ones submitted for review.

Meta rejects a submission whose requested permissions don't match what the app uses.
"""

import re
from pathlib import Path

from django.conf import settings

from apps.instagram.oauth import SCOPES
from apps.publishing.views import _FB_SCOPES


def _table_scopes(heading: str) -> set[str]:
    pack = Path(settings.BASE_DIR, "docs", "ops", "instagram-app-review.md").read_text(
        encoding="utf-8"
    )
    section = pack.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE))


def test_review_pack_lists_exactly_the_instagram_scopes():
    assert _table_scopes("Instagram permissions") == set(SCOPES.split(","))


def test_review_pack_lists_exactly_the_facebook_scopes():
    assert _table_scopes("Facebook Page permissions") == set(_FB_SCOPES.split(","))
