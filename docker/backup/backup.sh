#!/bin/sh
# Database backups for Akilent (see docs/ops/backups.md).
#
#   backup.sh loop          nightly dump at BACKUP_HOUR (UTC, default 02), restore test on Sundays
#   backup.sh dump          one dump now
#   backup.sh restore-test  restore the newest dump into a scratch database and report
#
# Needs PGHOST/PGUSER/PGPASSWORD/PGDATABASE, BACKUP_S3_BUCKET and AWS credentials that can only
# reach that bucket. Dumps go to s3://$BACKUP_S3_BUCKET/db/YYYY-MM-DD.dump (server-side encrypted);
# the bucket's lifecycle rule expires them after 30 days.
set -eu
set -o pipefail  # a failed pg_dump must fail the upload, not leave a truncated dump

BUCKET="${BACKUP_S3_BUCKET:-}"
HOUR="${BACKUP_HOUR:-02}"
SCRATCH_DB="restore_check"

log() { echo "[backup $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

need_bucket() {
    if [ -z "$BUCKET" ]; then
        log "BACKUP_S3_BUCKET is not set; nothing is being backed up."
        return 1
    fi
}

dump() {
    need_bucket || return 1
    key="db/$(date -u +%Y-%m-%d).dump"
    log "dumping $PGDATABASE to s3://$BUCKET/$key"
    # Stream straight to S3: the dump never sits on the VPS disk. --expected-size lets the CLI
    # pick part sizes for large streams.
    pg_dump -Fc --no-owner "$PGDATABASE" \
        | aws s3 cp - "s3://$BUCKET/$key" --sse AES256 --expected-size 10737418240
    log "dump done"
}

restore_test() {
    need_bucket || return 1
    latest=$(aws s3 ls "s3://$BUCKET/db/" | awk '/\.dump$/ {print $4}' | sort | tail -n 1)
    if [ -z "$latest" ]; then
        log "no dump found to test"
        return 1
    fi
    log "restore test of $latest"
    tmp="/tmp/$latest"
    aws s3 cp "s3://$BUCKET/db/$latest" "$tmp"
    psql -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH_DB"
    psql -d postgres -qc "CREATE DATABASE $SCRATCH_DB"
    result="passed"
    if ! pg_restore --no-owner -d "$SCRATCH_DB" "$tmp"; then
        result="failed"
    fi
    count() { psql -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM $1" 2>/dev/null || echo "missing"; }
    accounts=$(count accounts_account)
    conversations=$(count conversations_conversation)
    messages=$(count conversations_message)
    runs=$(count automation_workflowrun)
    if [ "$accounts" = "missing" ] || [ "$accounts" = "0" ]; then
        result="failed"
    fi
    psql -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH_DB"
    rm -f "$tmp"
    printf '{"checked_at": "%s", "dump": "%s", "result": "%s", "counts": {"accounts": "%s", "conversations": "%s", "messages": "%s", "workflow_runs": "%s"}}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$latest" "$result" "$accounts" "$conversations" "$messages" "$runs" \
        | aws s3 cp - "s3://$BUCKET/db/restore-check-latest.json" --sse AES256 --content-type application/json
    log "restore test $result (accounts=$accounts conversations=$conversations messages=$messages runs=$runs)"
    [ "$result" = "passed" ]
}

loop() {
    last=""
    while true; do
        today=$(date -u +%Y-%m-%d)
        if [ "$(date -u +%H)" = "$HOUR" ] && [ "$last" != "$today" ]; then
            last="$today"
            dump || log "dump FAILED"
            if [ "$(date -u +%u)" = "7" ]; then
                restore_test || log "restore test FAILED"
            fi
        fi
        sleep 300
    done
}

case "${1:-loop}" in
    loop) loop ;;
    dump) dump ;;
    restore-test) restore_test ;;
    *) echo "usage: backup.sh [loop|dump|restore-test]" >&2; exit 2 ;;
esac
