"""Template drift detector (UI quality programme, Phase 0.10).

Finds markup that bypasses the design system: colours that don't follow the theme,
type and heights that break on phones, tables that overflow, controls only a mouse
can reveal. It reports *unapproved* findings: a line listed in
``apps/core/tests/drift_exceptions.toml`` (path + rule + reason) is not counted.

The test (``apps/core/tests/test_template_drift.py``) holds each template directory to
a baseline (``drift_baseline.json``) that may only go down; an area sweep takes its
directory to zero. A count is only worth something if the detector is right, so every
rule is pinned by positive and negative cases in the test.

    python -m apps.core.template_drift            # counts per directory and rule
    python -m apps.core.template_drift --write    # re-record the baseline
"""

from __future__ import annotations

import fnmatch
import json
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
BASELINE = Path(__file__).resolve().parent / "tests" / "drift_baseline.json"
EXCEPTIONS = Path(__file__).resolve().parent / "tests" / "drift_exceptions.toml"

HEX = r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b"

# Off-palette hues. blue/teal/amber/coral/gray are brand ramps redefined in
# assets/app.css; these are Tailwind's stock hues, which docs/brand-colors.md bans.
STOCK_HUES = "indigo|purple|violet|fuchsia|pink|rose|sky|cyan|emerald|lime"

RULES = {
    "hex-class": "raw hex in an arbitrary-value utility class - use a token",
    "hex-style": "raw hex in a style attribute or <style> block - use a token",
    "px-text": "text-[Npx] - use the type scale (text-xs is the floor)",
    "stock-hue": "Tailwind stock hue outside the brand palette",
    "gray-text": "text-gray-400/500 as text - below 4.5:1 on white; use text-ink-subtle",
    "table-unwrapped": "<table> not in .table-wrap / .table-scroll / overflow-x-auto, and not .table--cards",
    "viewport-height": "min-h-screen / h-screen / bare 100vh - use min-h-viewport or svh/dvh",
    "hover-only": "revealed only on hover (opacity-0 + group-hover) with no pointer-fine: scope",
}


@dataclass(frozen=True)
class Finding:
    path: str  # relative to templates/, forward slashes
    line: int
    rule: str
    text: str

    @property
    def area(self) -> str:
        return self.path.split("/", 1)[0] if "/" in self.path else "(root)"


def _blank(match: re.Match) -> str:
    """Replace a stripped region with blank lines so line numbers survive."""
    return "\n" * match.group(0).count("\n")


def strip_non_markup(src: str) -> str:
    """Remove what is not rendered markup: template comments and script bodies.

    Template variable/tag *expressions* are replaced too, so a hex inside
    {{ pillar.color }} or a {% if %} test is never mistaken for markup.
    """
    src = re.sub(
        r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", _blank, src, flags=re.S
    )
    src = re.sub(r"\{#.*?#\}", _blank, src, flags=re.S)
    src = re.sub(r"<!--.*?-->", _blank, src, flags=re.S)
    src = re.sub(
        r"(<script\b[^>]*>).*?(</script>)",
        lambda m: m.group(1) + _blank(m) + m.group(2),
        src,
        flags=re.S | re.I,
    )
    src = re.sub(r"\{\{.*?\}\}", lambda m: "{{}}" + _blank(m), src, flags=re.S)
    return src


_CLASS_ATTR = re.compile(
    r"""(?:\bclass|:class|x-bind:class)\s*=\s*("[^"]*"|'[^']*')""", re.S
)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*("[^"]*"|'[^']*')""", re.S)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S | re.I)
_TABLE = re.compile(r"<table\b([^>]*)>", re.I)
_WRAPPER = re.compile(
    r"table-wrap|table-scroll|overflow-x-auto|overflow-auto|table--cards"
)


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def scan_source(src: str, path: str = "x.html") -> list[Finding]:
    clean = strip_non_markup(src)
    found: list[Finding] = []

    def add(pos: int, rule: str, text: str) -> None:
        found.append(
            Finding(path, _line_of(clean, pos), rule, " ".join(text.split())[:120])
        )

    for m in _CLASS_ATTR.finditer(clean):
        value, start = m.group(1), m.start(1)
        if re.search(r"-\[" + HEX, value):
            add(start, "hex-class", value)
        if re.search(r"\btext-\[\d+(?:\.\d+)?px\]", value):
            add(start, "px-text", value)
        if re.search(
            r"(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border|ring|from|to|via|fill|stroke)-(?:"
            + STOCK_HUES
            + r")-\d{2,3}\b",
            value,
        ):
            add(start, "stock-hue", value)
        if re.search(r"(?<![\w:-])text-gray-(?:400|500)\b", value):
            add(start, "gray-text", value)
        if re.search(r"(?<![\w-])(?:min-h-screen|h-screen)\b", value):
            add(start, "viewport-height", value)
        if (
            re.search(r"group-hover:opacity-100", value)
            and re.search(r"(?<![\w-])(?:[a-z]+:)?opacity-0\b", value)
            and "pointer-fine:opacity-0" not in value
        ):
            add(start, "hover-only", value)

    for m in _STYLE_ATTR.finditer(clean):
        if re.search(HEX, m.group(1)):
            add(m.start(1), "hex-style", m.group(1))
        if re.search(r"\b100vh\b", m.group(1)) and not re.search(r"[sd]vh", m.group(1)):
            add(m.start(1), "viewport-height", m.group(1))

    for m in _STYLE_BLOCK.finditer(clean):
        body, base = m.group(1), m.start(1)
        for decl in re.finditer(r"[^;{}]+", body):
            text = decl.group(0)
            if (
                re.search(r":\s*[^;]*" + HEX, text)
                and "var(" not in text.split(":", 1)[0]
            ):
                add(base + decl.start(), "hex-style", text)
            if re.search(r"\b100vh\b", text) and not re.search(
                r"[sd]vh", body[decl.end() : decl.end() + 80]
            ):
                add(base + decl.start(), "viewport-height", text)

    for m in _TABLE.finditer(clean):
        attrs = m.group(1)
        if re.search(r'role\s*=\s*["\']presentation', attrs) or "table--cards" in attrs:
            continue
        before = clean[max(0, m.start() - 600) : m.start()]
        opening_tags = re.findall(r"<(?!/)[a-z][^>]*>", before, re.I)[-3:]
        if not any(_WRAPPER.search(t) for t in opening_tags) and not _WRAPPER.search(
            attrs
        ):
            add(m.start(), "table-unwrapped", m.group(0))
    return found


def load_exceptions(path: Path = EXCEPTIONS) -> list[dict]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("exception", [])
    for e in entries:
        missing = {"path", "rule", "reason"} - set(e)
        if missing:
            raise ValueError(f"drift exception {e!r} is missing {sorted(missing)}")
        if e["rule"] != "*" and e["rule"] not in RULES:
            raise ValueError(f"drift exception names unknown rule {e['rule']!r}")
        if not str(e["reason"]).strip():
            raise ValueError(f"drift exception for {e['path']} has an empty reason")
    return entries


def _excepted(f: Finding, exceptions: list[dict]) -> bool:
    return any(
        fnmatch.fnmatch(f.path, e["path"]) and e["rule"] in ("*", f.rule)
        for e in exceptions
    )


def scan_templates(
    root: Path = TEMPLATES, exceptions: list[dict] | None = None
) -> list[Finding]:
    exceptions = load_exceptions() if exceptions is None else exceptions
    out: list[Finding] = []
    for file in sorted(root.rglob("*.html")):
        rel = file.relative_to(root).as_posix()
        for f in scan_source(file.read_text(encoding="utf-8"), rel):
            if not _excepted(f, exceptions):
                out.append(f)
    return out


def counts(findings: list[Finding]) -> dict[str, int]:
    c = Counter(f"{f.area}::{f.rule}" for f in findings)
    return dict(sorted(c.items()))


def main(argv: list[str]) -> int:
    findings = scan_templates()
    table = counts(findings)
    if "--write" in argv:
        BASELINE.write_text(
            json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {BASELINE} ({sum(table.values())} findings)")
        return 0
    for key, n in table.items():
        print(f"{n:5d}  {key}")
    print(f"{sum(table.values()):5d}  total")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
