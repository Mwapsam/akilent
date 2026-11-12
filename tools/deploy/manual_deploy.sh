#!/usr/bin/env bash
# Manual production deploy, for when GitHub Actions is down. Runs from your machine (Git Bash
# on Windows is fine) and releases the same way .github/workflows/deploy.yml does: the server
# half is tools/deploy/remote_release.sh, piped over SSH. See docs/ops/manual-deploy.md.
#
#   tools/deploy/manual_deploy.sh                 # deploy origin/main (or local main if GitHub is down)
#   tools/deploy/manual_deploy.sh --sha abc1234   # deploy (or roll back to) a specific commit
#   tools/deploy/manual_deploy.sh --dry-run       # show what would happen; touch nothing
#
# Options:
#   --sha REF          commit to deploy (default: origin/main, else local main)
#   --bundle           send the commit over SSH instead of the server fetching from GitHub
#                      (picked automatically when GitHub can't be reached from here)
#   --run-tests        run the test suite locally first; counts as the CI check
#   --skip-ci-check    deploy without a green CI run or local tests (you take the risk)
#   --no-invalidate    skip the CloudFront invalidation
#   --dry-run          print the plan and stop
#   --yes              don't ask for confirmation
#
# Settings come from tools/deploy/manual_deploy.env (gitignored; copy the .example next to it)
# or the environment. Nothing secret is stored in the repository.
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
HERE="$ROOT/tools/deploy"

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

REF="" BUNDLE=0 RUN_TESTS=0 SKIP_CI=0 INVALIDATE=1 DRY=0 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) REF=${2:?--sha needs a commit}; shift 2 ;;
    --bundle) BUNDLE=1; shift ;;
    --run-tests) RUN_TESTS=1; shift ;;
    --skip-ci-check) SKIP_CI=1; shift ;;
    --no-invalidate) INVALIDATE=0; shift ;;
    --dry-run) DRY=1; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# --- Settings -----------------------------------------------------------------------------
CONFIG=${MANUAL_DEPLOY_ENV:-$HERE/manual_deploy.env}
if [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  set -a; . <(tr -d '\r' < "$CONFIG"); set +a
elif [ -z "${DEPLOY_SSH_HOST:-}" ]; then
  die "no settings file. Create it from the template and fill in the server details:
    cp tools/deploy/manual_deploy.env.example tools/deploy/manual_deploy.env
  The values are your GitHub secrets SSH_HOST, SSH_USER, SSH_PORT and DEPLOY_PATH."
fi
missing=""
for name in DEPLOY_SSH_HOST DEPLOY_SSH_USER DEPLOY_PATH; do
  [ -n "${!name:-}" ] || missing="$missing $name"
done
[ -z "$missing" ] || die "empty in $CONFIG:$missing
  They are your GitHub secrets SSH_HOST, SSH_USER and DEPLOY_PATH (Settings > Secrets > Actions)."
DEPLOY_SSH_PORT=${DEPLOY_SSH_PORT:-22}
SITE_URL=${SITE_URL:-https://akilent.com}
GITHUB_REPO=${GITHUB_REPO:-}

SSH_OPTS=(-p "$DEPLOY_SSH_PORT" -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30)
SCP_OPTS=(-P "$DEPLOY_SSH_PORT" -o BatchMode=yes -o ConnectTimeout=15)
if [ -n "${DEPLOY_SSH_KEY:-}" ]; then
  SSH_OPTS+=(-i "$DEPLOY_SSH_KEY"); SCP_OPTS+=(-i "$DEPLOY_SSH_KEY")
fi
TARGET="$DEPLOY_SSH_USER@$DEPLOY_SSH_HOST"
remote() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

# --- Which commit -------------------------------------------------------------------------
say "Checking GitHub"
GITHUB_UP=1
if ! timeout 30 git fetch --quiet origin main 2>/dev/null; then
  GITHUB_UP=0
  warn "can't fetch from GitHub; using your local copy and sending the commit over SSH"
  BUNDLE=1
fi
if [ -z "$REF" ]; then
  if [ "$GITHUB_UP" = 1 ]; then REF=origin/main; else REF=main; fi
fi
SHA=$(git rev-parse --verify "$REF^{commit}" 2>/dev/null) || die "unknown commit: $REF"
SUBJECT=$(git log -1 --format='%s' "$SHA")

if [ "$GITHUB_UP" = 1 ] && ! git merge-base --is-ancestor "$SHA" origin/main; then
  # The server only fetches main, so it couldn't get this commit from GitHub.
  warn "$SHA is not on origin/main (an unpushed or side-branch commit); sending it over SSH"
  BUNDLE=1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  warn "you have uncommitted changes; they are NOT deployed - only commit $SHA is"
fi

# --- What is live -------------------------------------------------------------------------
say "Checking the server"
LIVE=$(remote "grep -m1 '^DEPLOY_SHA=' '$DEPLOY_PATH/.env' | cut -d= -f2-; true") \
  || die "can't reach $TARGET over SSH (check DEPLOY_SSH_* settings and your key)"
LIVE=$(printf '%s' "$LIVE" | tr -d '\r\n ')
DIRECTION="new release"
if [ -n "$LIVE" ] && git cat-file -e "$LIVE^{commit}" 2>/dev/null; then
  if [ "$LIVE" = "$SHA" ]; then
    DIRECTION="redeploy of the live commit"
  elif git merge-base --is-ancestor "$SHA" "$LIVE"; then
    DIRECTION="ROLLBACK ($(git rev-list --count "$SHA..$LIVE") commits back)"
  else
    DIRECTION="$(git rev-list --count "$LIVE..$SHA") new commits"
  fi
fi

# --- CI gate ------------------------------------------------------------------------------
# GitHub's own rule: only a commit CI passed goes out. Checked through the GitHub API when it
# answers; otherwise the local test suite stands in (--run-tests), or you skip it on purpose.
CI_STATE="not checked"
# A dry run reports a failed gate and carries on to the plan; a real run stops.
ci_stop() { if [ "$DRY" = 1 ]; then CI_STATE="$1 - a real run stops here"; else die "$1"; fi; }
if [ "$SKIP_CI" = 1 ]; then
  CI_STATE="skipped (--skip-ci-check)"
elif [ "$RUN_TESTS" = 1 ]; then
  CI_STATE="local tests (run before deploying)"
elif [ "$GITHUB_UP" = 1 ] && [ -n "$GITHUB_REPO" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
  CI_STATE=$(curl -fsS --max-time 20 \
      -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$GITHUB_REPO/actions/runs?head_sha=$SHA&per_page=20" 2>/dev/null \
    | python -c 'import json,sys
runs=[r for r in json.load(sys.stdin).get("workflow_runs",[]) if r.get("name")=="CI"]
print(runs[0].get("conclusion") or runs[0].get("status") if runs else "none")' 2>/dev/null) || CI_STATE="unreachable"
  [ "$CI_STATE" = "success" ] || ci_stop "CI for $SHA: $CI_STATE. Use --run-tests to test locally, or --skip-ci-check to deploy anyway."
else
  ci_stop "can't confirm CI passed for $SHA (needs GitHub plus GITHUB_REPO and GITHUB_TOKEN). Use --run-tests, or --skip-ci-check."
fi

# --- Plan ---------------------------------------------------------------------------------
cat <<PLAN

  Deploy to   $TARGET:$DEPLOY_PATH
  Commit      $SHA
              $SUBJECT
  Live now    ${LIVE:-unknown}
  This is     $DIRECTION
  CI          $CI_STATE
  Code via    $([ "$BUNDLE" = 1 ] && echo "git bundle over SSH" || echo "server fetches from GitHub")
  CloudFront  $([ "$INVALIDATE" = 1 ] && echo "invalidate /*" || echo "skipped")
  Cloudflare  $([ -n "${CLOUDFLARE_ZONE_ID:-}" ] && [ -n "${CLOUDFLARE_API_TOKEN:-}" ] && echo "purge $SITE_URL/" || echo "not purged (no CLOUDFLARE_* settings)")

  The server keeps its current .env (only DEPLOY_SHA changes); the database is snapshotted
  first, and the code rolls back by itself if /healthz doesn't answer within 90 seconds.
PLAN

if [ "$DRY" = 1 ]; then say "Dry run: nothing was changed."; exit 0; fi

if [ "$RUN_TESTS" = 1 ]; then
  say "Running the test suite at $SHA"
  [ "$(git rev-parse HEAD)" = "$SHA" ] || die "--run-tests tests your checkout: check out $SHA first (git checkout $SHA), or deploy HEAD"
  poetry run pytest -q -p no:cacheprovider || die "tests failed; not deploying"
fi

if [ "$YES" != 1 ]; then
  printf '\nType "deploy" to release this to production: '
  read -r answer
  [ "$answer" = "deploy" ] || die "cancelled"
fi

# --- Ship ---------------------------------------------------------------------------------
REMOTE_BUNDLE=""
if [ "$BUNDLE" = 1 ]; then
  say "Packing $SHA"
  LOCAL_BUNDLE=$(mktemp -t akilent-deploy-XXXXXX).bundle
  git update-ref refs/manual-deploy/release "$SHA"
  # Only what the server is missing when we know what it has; the whole history otherwise.
  if [ -n "$LIVE" ] && git cat-file -e "$LIVE^{commit}" 2>/dev/null && [ "$LIVE" != "$SHA" ] \
     && git merge-base --is-ancestor "$LIVE" "$SHA"; then
    git bundle create "$LOCAL_BUNDLE" refs/manual-deploy/release "^$LIVE" 2>/dev/null
  else
    git bundle create "$LOCAL_BUNDLE" refs/manual-deploy/release 2>/dev/null
  fi
  git update-ref -d refs/manual-deploy/release
  REMOTE_BUNDLE="/tmp/akilent-deploy-$SHA.bundle"
  scp "${SCP_OPTS[@]}" "$LOCAL_BUNDLE" "$TARGET:$REMOTE_BUNDLE" >/dev/null
  rm -f "$LOCAL_BUNDLE"
fi

say "Releasing on the server (this takes a few minutes)"
started=$(date +%s)
set +e
tr -d '\r' < "$HERE/remote_release.sh" \
  | remote "bash -s -- $(printf '%q ' "$DEPLOY_PATH" "$SHA" "$REMOTE_BUNDLE" "$INVALIDATE")"
status=$?
set -e
[ "$status" = 0 ] || die "release failed (exit $status) after $(( $(date +%s) - started ))s - see the output above"

if [ -n "${CLOUDFLARE_ZONE_ID:-}" ] && [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
  say "Purging the landing page from Cloudflare"
  curl -fsS -X POST "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/purge_cache" \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
    --data "{\"files\":[\"$SITE_URL/\"]}" >/dev/null \
    || warn "Cloudflare purge failed; the landing page may be stale for up to ten minutes"
fi

say "Checking $SITE_URL/healthz from outside"
if curl -fsS --max-time 20 "$SITE_URL/healthz" >/dev/null; then
  echo "OK"
else
  warn "$SITE_URL/healthz did not answer from here (the server's own check passed: look at Cloudflare/nginx)"
fi

say "Done: $SHA is live ($(( $(date +%s) - started ))s). Run docs/ops/smoke-test.md next."
