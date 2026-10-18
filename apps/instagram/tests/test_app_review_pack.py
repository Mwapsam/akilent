"""The permissions the connect flow asks for are exactly the ones submitted for review.

Meta rejects a submission whose requested permissions don't match what the app uses.
"""

import re
from pathlib import Path

from django.conf import settings

from apps.instagram.oauth import SCOPES


def test_review_pack_lists_exactly_the_requested_scopes():
    pack = Path(settings.BASE_DIR, "docs", "ops", "instagram-app-review.md").read_text(
        encoding="utf-8"
    )
    table = set(re.findall(r"^\| `([a-z_]+)` \|", pack, flags=re.MULTILINE))
    assert table == set(SCOPES.split(","))
