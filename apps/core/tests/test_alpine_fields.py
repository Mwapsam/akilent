"""Alpine components declare every private field they set.

Alpine stores a field that isn't on the component on the outermost x-data instead, and
base.html has ``<body x-data>``: one object shared by every component on every page, kept
across boosted navigation. A conversation's ``this._destroyed = true`` landed there, and
every conversation opened after it never polled for new messages.
"""

import re
from pathlib import Path

from django.conf import settings

SCRIPTS = Path(settings.BASE_DIR) / "static" / "js"
WRITE = re.compile(r"this\.(_[A-Za-z]\w*)\s*=(?!=)")


def _app_scripts():
    for path in sorted(SCRIPTS.glob("*.js")):
        if not path.name.endswith(".min.js"):
            yield path


def test_every_private_field_set_on_this_is_declared():
    missing = []
    for path in _app_scripts():
        source = path.read_text(encoding="utf-8")
        for name in sorted(set(WRITE.findall(source))):
            if not re.search(rf"(^|[\s{{,]){re.escape(name)}\s*:", source, re.M):
                missing.append(f"{path.name}: {name}")
    assert not missing, (
        "Declare these on their component (Alpine would otherwise store them on "
        f"<body x-data>, shared by every page): {missing}"
    )
