# Database backups

The `backup` service (`docker/backup/`) keeps the only copy of Akilent's data somewhere other
than the VPS:

- **Every night at 02:00 UTC** it streams `pg_dump -Fc` to `s3://<bucket>/db/YYYY-MM-DD.dump`
  with server-side encryption.
- **Every Sunday** it also restores the newest dump into a scratch database, counts the key
  tables, drops the scratch database and writes the result to `db/restore-check-latest.json`.

The Pilot Command Center (`/manage/pilot/`) shows the newest dump and the last restore test.

## One-time setup (AWS console)

1. **Create a bucket**, for example `akilent-backups`, in the same region as the app.
   - Leave *Block all public access* **on**.
   - Default encryption: SSE-S3.
   - Never put this bucket behind CloudFront: the media bucket is public, this one must not be.
2. **Lifecycle rule:** prefix `db/`, expire current versions after **30 days**.
3. **IAM user** `akilent-backup` with only this policy, then create an access key:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::akilent-backups"},
       {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
        "Resource": "arn:aws:s3:::akilent-backups/db/*"}
     ]
   }
   ```
4. **GitHub secrets:** `BACKUP_S3_BUCKET`, `BACKUP_AWS_ACCESS_KEY_ID` and
   `BACKUP_AWS_SECRET_ACCESS_KEY`. The next deploy writes them to `.env` and starts the service.
   Until `BACKUP_S3_BUCKET` is set, the service only logs that nothing is being backed up.

For the Pilot Command Center to read the backup status, the web app uses the same bucket name
with its own AWS keys. Give that user `s3:ListBucket` and `s3:GetObject` on the bucket as well.

## Check it now (on the VPS)

```sh
docker compose --profile prod run --rm backup dump           # one dump now
docker compose --profile prod run --rm backup restore-test   # restore the newest and report
docker compose --profile prod logs --tail 50 backup
```

## Restore for real

Stop anything that writes, restore, then start again:

```sh
docker compose --profile prod stop web celery_worker celery_ai celery_beat
aws s3 cp s3://akilent-backups/db/2026-09-26.dump /tmp/restore.dump
docker compose cp /tmp/restore.dump db:/tmp/restore.dump
docker compose exec db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" \
  && createdb -U "$POSTGRES_USER" "$POSTGRES_DB" \
  && pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner /tmp/restore.dump'
docker compose --profile prod up -d
```

A dump is taken once a day, so up to 24 hours of messages can be lost. WhatsApp won't resend
those: Meta retries webhooks for a limited time only.
