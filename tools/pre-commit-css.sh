#!/bin/sh
# Rebuild the compiled Tailwind CSS when a commit touches anything it is built
# from, and stage the result with that same commit.
#
# Why this exists: static/css/app.css is committed and is what deploys, and the
# build is purge-scanned — a utility class that appears in no other template is
# simply absent from the compiled output until a rebuild. So a commit adding
# `mt-auto` to one template leaves the CSS stale, and the CI guard fails on the
# *next* build rather than at the moment the mistake was made.
#
# Installed by tools/install-hooks.sh. Never blocks a commit: if the Tailwind
# CLI is missing it warns and gets out of the way, because the CI guard is still
# there as the real gate.

set -e

CLI="tools/tailwindcss.exe"
[ -x "$CLI" ] || CLI="tools/tailwindcss"

# The paths assets/app.css scans (its @source rules), plus the stylesheet itself.
staged=$(git diff --cached --name-only --diff-filter=ACMR \
  -- assets/app.css templates apps static/js)
[ -n "$staged" ] || exit 0

if [ ! -x "$CLI" ]; then
  echo "pre-commit: no Tailwind CLI at tools/tailwindcss[.exe] — skipping the" >&2
  echo "            CSS rebuild. static/css/app.css may now be stale; CI will" >&2
  echo "            catch it. See tools/install-hooks.sh." >&2
  exit 0
fi

# The build reads the working tree, not the index. If a scanned file has
# unstaged edits, the CSS about to be committed can contain rules for classes
# that are not in this commit — which fails the CI guard from the other
# direction ("STALE in the committed CSS"). Worth a word rather than a block:
# the commit is still almost certainly what the author wanted.
unstaged=$(git diff --name-only -- assets/app.css templates apps static/js)
if [ -n "$unstaged" ]; then
  echo "pre-commit: note — these files are scanned by the CSS build but have" >&2
  echo "            unstaged changes, so the rebuilt CSS reflects your working" >&2
  echo "            tree rather than this commit:" >&2
  echo "$unstaged" | sed 's/^/              /' >&2
fi

"$CLI" -i assets/app.css -o static/css/app.css --minify >/dev/null
git add static/css/app.css
echo "pre-commit: rebuilt and staged static/css/app.css"
