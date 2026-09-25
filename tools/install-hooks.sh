#!/bin/sh
# Install this repo's git hooks. Run once per clone:
#
#     sh tools/install-hooks.sh
#
# .git/hooks is not version controlled, so the hook bodies live in tools/ where
# they can be reviewed and changed like any other file, and this script just
# points git at them.

set -e

hooks_dir=$(git rev-parse --git-path hooks)
mkdir -p "$hooks_dir"

if [ -e "$hooks_dir/pre-commit" ] && ! grep -q "pre-commit-css.sh" "$hooks_dir/pre-commit" 2>/dev/null; then
  echo "A pre-commit hook already exists and is not this one:" >&2
  echo "  $hooks_dir/pre-commit" >&2
  echo "Add this line to it by hand instead of overwriting it:" >&2
  echo '  sh "$(git rev-parse --show-toplevel)/tools/pre-commit-css.sh"' >&2
  exit 1
fi

cat > "$hooks_dir/pre-commit" <<'HOOK'
#!/bin/sh
sh "$(git rev-parse --show-toplevel)/tools/pre-commit-css.sh"
HOOK

chmod +x "$hooks_dir/pre-commit"
echo "Installed pre-commit hook -> tools/pre-commit-css.sh"
