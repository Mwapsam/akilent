#!/usr/bin/env bash
# The server half of a manual deploy (tools/deploy/manual_deploy.sh pipes this file to the VPS
# over SSH: `ssh … bash -s -- <path> <sha> [<bundle>] [<invalidate>]`). It does what the
# "Deploy to VPS" step of .github/workflows/deploy.yml does, so a release is the same whichever
# way it goes out: snapshot the database, check out the commit, build, migrate, collectstatic,
# restart, reload nginx, wait for /healthz, and roll the code back if it never gets healthy.
#
# The one difference: GitHub rebuilds .env from its secrets on every deploy, and those secrets
# aren't on your laptop. Here the .env already on the server (written by the last GitHub
# deploy) is kept as it is; only DEPLOY_SHA is updated. A key the new commit's deploy.yml
# writes but this .env lacks is reported - add it by hand, or wait for GitHub.
#
# Keep release() and healthy() in step with deploy.yml: apps/core/tests/test_manual_deploy.py
# fails when deploy.yml's release commands are missing here.
set -euo pipefail

# Everything runs inside main(), called on the last line. This file arrives on bash's stdin
# (`bash -s`), and docker compose exec/run read stdin too: without the function, the first one
# swallows the rest of the script and bash stops early with success. bash parses the whole
# function before it runs any of it.
main() {

DEPLOY_PATH=${1:?usage: remote_release.sh <deploy-path> <sha> [<bundle-file>] [<invalidate 1|0>]}
SHA=${2:?commit sha required}
BUNDLE=${3:-}
INVALIDATE=${4:-1}

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

cd "$DEPLOY_PATH" || die "deploy path $DEPLOY_PATH not found"
[ -f .env ] || die "no .env in $DEPLOY_PATH: run one GitHub deploy first, or write it by hand"

# One deploy at a time: a GitHub run that comes back mid-way must not race this one.
if command -v flock >/dev/null 2>&1; then
  exec 9>.deploy.lock
  flock -n 9 || die "another deploy is running (lock: $DEPLOY_PATH/.deploy.lock)"
fi

# A value from .env, read as text (never sourced: values can hold $, quotes and spaces).
envget() { grep -m1 "^$1=" .env | cut -d= -f2- || true; }

# The same required values deploy.yml checks before touching anything.
missing=""
for name in DJANGO_SECRET_KEY ALLOWED_HOSTS POSTGRES_PASSWORD FIELD_ENCRYPTION_KEY INSTAGRAM_VERIFY_TOKEN; do
  [ -n "$(envget "$name")" ] || missing="$missing $name"
done
[ -n "$(envget INSTAGRAM_APP_SECRET)$(envget WHATSAPP_APP_SECRET)" ] || missing="$missing META_APP_SECRET"
[ -z "$missing" ] || die "required values are empty in .env:$missing"

POSTGRES_USER=$(envget POSTGRES_USER)
POSTGRES_DB=$(envget POSTGRES_DB)
ALLOWED_HOSTS=$(envget ALLOWED_HOSTS)
export POSTGRES_USER POSTGRES_DB ALLOWED_HOSTS

PREV=$(git rev-parse HEAD)
say "Live now: $PREV"

say "Fetching $SHA"
if [ -n "$BUNDLE" ]; then
  # GitHub itself is unreachable: the commit came over SSH as a git bundle.
  git fetch --quiet "$BUNDLE" "refs/manual-deploy/release"
  rm -f "$BUNDLE"
else
  git fetch --quiet origin main
fi
git cat-file -e "$SHA^{commit}" 2>/dev/null || die "commit $SHA is not available on the server"

set_deploy_sha() {
  if grep -q '^DEPLOY_SHA=' .env; then
    sed -i "s/^DEPLOY_SHA=.*/DEPLOY_SHA=$1/" .env
  else
    printf 'DEPLOY_SHA=%s\n' "$1" >> .env
  fi
  chmod 600 .env
}

# Builds every service the checked-out docker-compose.yml defines, not a fixed list, so a
# rollback to a commit with different services still builds. (Same as deploy.yml.)
release() {
  docker compose --profile prod build
  docker compose --profile prod run --rm web python manage.py migrate --noinput
  docker compose --profile prod run --rm web python manage.py collectstatic --noinput
  docker compose --profile prod up -d --remove-orphans
  # nginx reads nginx/conf.d only when it starts: check the new config, then reload it.
  docker compose --profile prod exec -T nginx nginx -t
  docker compose --profile prod exec -T nginx nginx -s reload
}

# Healthy once /healthz answers through gunicorn (up to 90s). (Same as deploy.yml.)
healthy() {
  HOST=$(echo "$ALLOWED_HOSTS" | cut -d, -f1)
  for i in $(seq 1 18); do
    if curl -fsS -H "Host: $HOST" http://127.0.0.1:8000/healthz >/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 1
}

# Snapshot the database before migrations run (keeps the last 5), as deploy.yml does.
if docker compose ps --status running db </dev/null | grep -q db; then
  say "Snapshotting the database"
  mkdir -p backups/pre-deploy
  docker compose exec -T db pg_dump -Fc --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB" \
    </dev/null >"backups/pre-deploy/$(date -u +%Y%m%dT%H%M%SZ)-$PREV.dump"
  ls -1t backups/pre-deploy/*.dump | tail -n +6 | xargs -r rm -f
fi

say "Checking out $SHA"
git reset --hard "$SHA"

# Keys the new commit's deploy.yml writes into .env that this .env doesn't have. Not fatal:
# the app treats a missing key like an empty one, which may switch a feature off.
if [ -f .github/workflows/deploy.yml ]; then
  new_keys=$(awk '/cat > \.env <<ENVEOF/{on=1;next} /^[[:space:]]*ENVEOF/{on=0} on' .github/workflows/deploy.yml \
    | sed -n 's/^[[:space:]]*\([A-Z0-9_]*\)=.*/\1/p' | sort -u)
  absent=""
  for key in $new_keys; do grep -q "^$key=" .env || absent="$absent $key"; done
  if [ -n "$absent" ]; then
    printf '\nWARNING: this release expects keys that .env does not have:%s\n' "$absent"
    printf 'They come from GitHub secrets. Add them to %s/.env by hand if the release needs them.\n' "$DEPLOY_PATH"
  fi
fi

set_deploy_sha "$SHA"

say "Releasing"
release
say "Waiting for /healthz"
if ! healthy; then
  echo "Health check failed; rolling back to $PREV"
  git reset --hard "$PREV"
  set_deploy_sha "$PREV"
  if release && healthy; then
    echo "Rolled back to $PREV (migrations are not reversed; the pre-deploy snapshot is in backups/pre-deploy/)"
  else
    echo "Rollback failed or is unhealthy too - check 'docker compose logs web'"
  fi
  exit 1
fi
docker image prune -f >/dev/null

if [ "$INVALIDATE" = "1" ]; then
  say "Invalidating CloudFront"
  # Through the app container, which has boto3 and the AWS keys from .env: no AWS CLI needed
  # on the server or on your machine. Not fatal: stale static files for a while, not an outage.
  docker compose --profile prod run --rm -T web python - <<'PY' || echo "WARNING: CloudFront invalidation failed; run it from the AWS console"
import os, time, boto3
dist = os.environ.get("CLOUDFRONT_ID") or os.environ.get("AWS_CLOUDFRONT_ID")
if not dist:
    raise SystemExit("CLOUDFRONT_ID is not set in .env")
cf = boto3.client("cloudfront")
resp = cf.create_invalidation(
    DistributionId=dist,
    InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": f"manual-{int(time.time())}"},
)
print("Invalidation", resp["Invalidation"]["Id"], resp["Invalidation"]["Status"])
PY
fi

say "Deployed $SHA (was $PREV)"
}

main "$@"
