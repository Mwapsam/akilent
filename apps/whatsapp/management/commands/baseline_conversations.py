"""Step 0a: read-only baseline of how WhatsApp enquiries are answered today.

    poetry run python manage.py baseline_conversations \\
        (--account <id|slug> | --all-accounts) [--all-accounts-total] \\
        [--since YYYY-MM-DD] [--until YYYY-MM-DD] \\
        [--gap-hours 24,72] [--grace-hours 1] \\
        [--out snapshot.json --operator "Name"]

Measurement only: it never writes to the database. With ``--out`` the snapshot
is written exactly once (an existing file is never overwritten; a rerun is a
new artifact) next to a ``.measurement.json`` record holding baseline_id,
generated_at, git_commit, command_arguments, output_sha256 and operator.
Only aggregate counts are output - no message content or phone numbers.
"""
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.whatsapp.baseline import build_snapshot, render_report
from apps.whatsapp.baseline_schema import SnapshotSchemaError, validate_snapshot


def _git_commit() -> str:
    root = Path(settings.BASE_DIR)
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}+dirty" if dirty else sha


def _parse_date(value, flag):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise CommandError(f"{flag} must be YYYY-MM-DD, got {value!r}")


def _parse_gaps(value):
    try:
        gaps = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise CommandError("--gap-hours must be comma-separated integers, e.g. 24,72")
    if not gaps or any(g <= 0 for g in gaps) or len(set(gaps)) != len(gaps):
        raise CommandError("--gap-hours needs one or more distinct positive integers")
    return gaps


class Command(BaseCommand):
    help = "Read-only baseline of WhatsApp enquiry response times (Step 0a)."

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument("--account", help="Account id or slug.")
        who.add_argument("--all-accounts", action="store_true",
                         help="Measure every account, each independently.")
        parser.add_argument("--all-accounts-total", action="store_true",
                            help="Add a total that is the arithmetic sum of the per-account results.")
        parser.add_argument("--since", help="Enquiries starting on/after this date (UTC).")
        parser.add_argument("--until", help="Enquiries starting on/before this date (UTC, inclusive).")
        parser.add_argument("--gap-hours", default="24,72",
                            help="Enquiry-gap values to compare (default 24,72).")
        parser.add_argument("--grace-hours", type=float, default=1,
                            help="Applied uniformly to every gap (default 1).")
        parser.add_argument("--out", help="Write the JSON snapshot here (never overwrites).")
        parser.add_argument("--operator", help="Who ran this; required with --out.")

    def handle(self, *args, **options):
        if options["all_accounts_total"] and not options["all_accounts"]:
            raise CommandError("--all-accounts-total requires --all-accounts")
        if options["out"] and not options["operator"]:
            raise CommandError("--operator is required with --out")
        if options["grace_hours"] < 0:
            raise CommandError("--grace-hours must be >= 0")
        out_path = Path(options["out"]) if options["out"] else None
        record_path = out_path.with_name(out_path.name + ".measurement.json") if out_path else None
        for path in (out_path, record_path):
            if path is not None and path.exists():
                raise CommandError(
                    f"{path} already exists. Baselines are immutable; choose a new path for a new measurement."
                )

        gaps = _parse_gaps(options["gap_hours"])
        since_date = _parse_date(options["since"], "--since") if options["since"] else None
        until_date = _parse_date(options["until"], "--until") if options["until"] else None
        since = datetime.combine(since_date, time.min, tzinfo=dt_timezone.utc) if since_date else None
        until = (datetime.combine(until_date, time.min, tzinfo=dt_timezone.utc) + timedelta(days=1)
                 if until_date else None)
        if since and until and since >= until:
            raise CommandError("--since must be before --until")

        Account = apps.get_model("accounts", "Account")  # no cross-app model import
        if options["all_accounts"]:
            accounts = list(Account.objects.order_by("id"))
        else:
            ref = options["account"]
            accounts = list(Account.objects.filter(id=int(ref) if ref.isdigit() else None)
                            or Account.objects.filter(slug=ref))
            if not accounts:
                raise CommandError(f"No account matches {ref!r}")

        as_of = timezone.now()
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            snapshot = build_snapshot(
                accounts, gaps, options["grace_hours"], since, until,
                options["since"], options["until"], as_of, _git_commit(),
                include_total=options["all_accounts_total"],
            )

        try:
            validate_snapshot(snapshot)
        except SnapshotSchemaError as exc:
            raise CommandError(f"Snapshot violates the schema contract: {exc}")

        self.stdout.write(render_report(snapshot))

        if out_path:
            body = json.dumps(snapshot, indent=2, sort_keys=True).encode()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "xb") as fh:  # "x": fail rather than overwrite
                fh.write(body)
            record = {
                "baseline_id": str(uuid.uuid4()),
                "generated_at": snapshot["metadata"]["generated_at"],
                "git_commit": snapshot["metadata"]["git_commit"],
                "command_arguments": sys.argv[1:],
                "output_sha256": hashlib.sha256(body).hexdigest(),
                "operator": options["operator"],
            }
            with open(record_path, "x") as fh:
                json.dump(record, fh, indent=2, sort_keys=True)
            self.stdout.write(self.style.SUCCESS(
                f"\nSnapshot: {out_path}\nsha256: {record['output_sha256']}\n"
                f"Record: {record_path} (baseline_id {record['baseline_id']})"
            ))
