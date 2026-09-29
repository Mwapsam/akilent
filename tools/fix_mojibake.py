"""Find and fix double-encoded UTF-8 (mojibake) in text files.

Dry run:  poetry run python fix_mojibake.py templates static/js
Apply:    poetry run python fix_mojibake.py templates static/js --apply
"""

import sys
from pathlib import Path

import ftfy

EXTS = {".html", ".js", ".css", ".txt", ".md"}


def show(line: str) -> str:
    """Printable preview: control chars (like the U+0090 in broken '═') become \\x escapes."""
    line = line.strip()[:90]
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in line)


apply = "--apply" in sys.argv
roots = [Path(a) for a in sys.argv[1:] if a != "--apply"] or [Path("templates")]

changed = 0
for root in roots:
    for path in sorted(root.rglob("*")):
        if path.suffix not in EXTS or not path.is_file():
            continue
        # newline="" keeps the file's existing line endings on Windows
        with Path.open(path, encoding="utf-8", newline="") as f:
            original = f.read()
        fixed = ftfy.fix_encoding(original)
        if fixed == original:
            continue
        changed += 1
        diffs = [
            (a, b) for a, b in zip(original.splitlines(), fixed.splitlines()) if a != b
        ]
        print(f"\n{path}  ({len(diffs)} lines)")
        for a, b in diffs[:3]:
            print(f"  - {show(a)}")
            print(f"  + {show(b)}")
        if apply:
            with Path.open(path, "w", encoding="utf-8", newline="") as f:
                f.write(fixed)

print(
    f"\n{changed} file(s) {'fixed' if apply else 'need fixing (dry run, add --apply)'}"
)
