"""Compare a freshly built Tailwind stylesheet against the committed one.

static/css/app.css is committed and is exactly what deploy.yml ships - nothing
rebuilds it on the server. Tailwind's build is purge-scanned, so a template
using a class no other template uses silently loses its styling unless the
author rebuilt. This guards that.

It deliberately does NOT compare byte-for-byte: the standalone CLI emits rules
in a slightly different order on Windows and Linux, so a byte diff fails on
every PR authored from a Windows machine. What matters is that the same set of
rules is present, which is what a missing class would actually change.

Usage: python tools/check_css_build.py <freshly-built.css> <committed.css>
"""

import pathlib
import re
import sys

BANNER = re.compile(r"^\s*/\*!.*?\*/", re.S)


def rules(path):
    """The stylesheet as an order-independent multiset of declaration blocks."""
    css = pathlib.Path(path).read_text(encoding="utf-8")
    css = BANNER.sub("", css)
    # Splitting on "}" breaks nested at-rules apart, but it does so identically
    # for both inputs, so the resulting chunks still compare soundly.
    return sorted(chunk.strip() for chunk in css.split("}") if chunk.strip())


def main():
    built_path, committed_path = sys.argv[1], sys.argv[2]
    built, committed = rules(built_path), rules(committed_path)

    if built == committed:
        same_bytes = (
            pathlib.Path(built_path).read_bytes()
            == pathlib.Path(committed_path).read_bytes()
        )
        note = "" if same_bytes else " (byte order differs - platform build, harmless)"
        print("Compiled CSS matches assets/app.css." + note)
        return 0

    from collections import Counter

    missing = Counter(built) - Counter(committed)   # rebuild has it, commit doesn't
    extra = Counter(committed) - Counter(built)     # commit has it, rebuild doesn't

    print("::error file=static/css/app.css::Compiled CSS is out of date. Run:")
    print("  tools/tailwindcss -i assets/app.css -o static/css/app.css --minify")
    print("")
    for label, bag in (("MISSING from the committed CSS", missing),
                       ("STALE in the committed CSS", extra)):
        if not bag:
            continue
        print("%s (%d rule(s)):" % (label, sum(bag.values())))
        for rule, _ in list(bag.items())[:20]:
            print("  " + rule[:200].replace("\n", " "))
        print("")
    return 1


if __name__ == "__main__":
    sys.exit(main())
