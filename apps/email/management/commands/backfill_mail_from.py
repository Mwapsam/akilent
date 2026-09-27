"""Management command: configure MAIL FROM and sync DNS records for SES domains.

Usage::

    # See what would change for all SES domains — no writes
    python manage.py backfill_mail_from --dry-run

    # Process a specific domain
    python manage.py backfill_mail_from --domain akilent.com

    # Process a specific domain with a different bounce subdomain
    python manage.py backfill_mail_from --domain akilent.com --subdomain mail

    # Real run for all SES domains
    python manage.py backfill_mail_from

After each real run the command re-reads the identity from SES and exits
non-zero if the provider state doesn't match what was requested.
"""
from __future__ import annotations

import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.email.models import EmailDomain
from apps.email.providers import get_mail_provider
from apps.email.services.domain import DomainService
from apps.email import dnscheck, verification


class Command(BaseCommand):
    help = "Configure MAIL FROM on SES and sync DNS record spec for sending domains."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--domain",
            action="append",
            dest="domains",
            metavar="DOMAIN",
            help=(
                "Sending domain to process (repeatable). "
                "Defaults to all SES-backed verified or pending domains."
            ),
        )
        parser.add_argument(
            "--subdomain",
            default="bounce",
            metavar="SUB",
            help="Subdomain prefix for the MAIL FROM domain (default: bounce).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change; make no SES writes and no DB writes.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        subdomain: str = options["subdomain"]
        dry_run: bool = options["dry_run"]
        domain_filter: list[str] | None = options["domains"]

        qs = EmailDomain.objects.select_related("account").filter(
            account__isnull=False,
        )
        if domain_filter:
            qs = qs.filter(domain__in=domain_filter)
            found = set(qs.values_list("domain", flat=True))
            missing = set(domain_filter) - found
            if missing:
                raise CommandError(
                    "Domains not found in the database: " + ", ".join(sorted(missing))
                )

        # Keep only SES-backed domains (the non-SES ones have no MAIL FROM concept).
        domains = [d for d in qs if d.is_ses_backed()]
        if not domains:
            self.stdout.write("No SES-backed domains found.")
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN — no SES writes, no DB writes.\n")
            )

        provider = get_mail_provider()
        get_mail_from = getattr(provider, "get_mail_from", None)
        configure_mail_from = getattr(provider, "configure_mail_from", None)

        if configure_mail_from is None:
            raise CommandError(
                "The current mail provider does not support MAIL FROM configuration."
            )

        errors: list[str] = []

        for record in domains:
            self._process_domain(
                record=record,
                provider=provider,
                subdomain=subdomain,
                dry_run=dry_run,
                get_mail_from=get_mail_from,
                configure_mail_from=configure_mail_from,
                errors=errors,
            )

        if errors:
            for msg in errors:
                self.stderr.write(self.style.ERROR(msg))
            sys.exit(1)

    # ------------------------------------------------------------------
    # Per-domain helpers
    # ------------------------------------------------------------------

    def _process_domain(
        self,
        *,
        record: EmailDomain,
        provider,
        subdomain: str,
        dry_run: bool,
        get_mail_from,
        configure_mail_from,
        errors: list[str],
    ) -> None:
        domain = record.domain
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {domain} ==="))

        # 1. Current SES state.
        current = None
        if get_mail_from is not None:
            try:
                current = get_mail_from(domain)
            except Exception as exc:
                self.stdout.write(f"  SES read failed: {exc}")

        self._print_ses_state("Current SES MAIL FROM", current)

        # 2. Desired DNS records diff.
        svc = DomainService(record.account)
        svc._provider = provider
        try:
            diff = svc.planned_dns_records(record)
        except Exception as exc:
            self.stdout.write(f"  Could not compute DNS diff: {exc}")
            diff = None

        if diff is not None:
            self._print_diff(diff)

        if dry_run:
            return

        # 3. Real run: configure MAIL FROM and sync.
        requested_domain = f"{subdomain}.{domain}"

        try:
            configure_mail_from(domain, subdomain=subdomain)
            self.stdout.write(f"  ✓ MAIL FROM configured → {requested_domain}")
        except Exception as exc:
            msg = f"  configure_mail_from failed for {domain}: {exc}"
            self.stdout.write(self.style.ERROR(msg))
            errors.append(f"{domain}: {exc}")
            return

        try:
            svc.sync_dns_records(record)
            self.stdout.write("  ✓ DNS records synced")
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  sync_dns_records failed: {exc}"))
            errors.append(f"{domain} dns-sync: {exc}")
            # Carry on to refresh and read-back even if sync had issues.

        try:
            result = verification.refresh_domain(record, provider=provider)
            self.stdout.write("  ✓ Domain refreshed")
            self._print_record_table(record)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"  refresh_domain failed: {exc}"))
            errors.append(f"{domain} refresh: {exc}")
            return

        # 4. Post-run SES read-back.
        if get_mail_from is None:
            return

        try:
            actual = get_mail_from(domain)
        except Exception as exc:
            msg = f"{domain}: post-run SES read failed — {exc}"
            self.stderr.write(self.style.ERROR(msg))
            errors.append(msg)
            return

        self._print_ses_state("Post-run SES MAIL FROM", actual)

        # Verify the provider accepted what we requested.
        ok = True
        if actual is None:
            self.stdout.write(
                self.style.ERROR(
                    "  ✗ SES returned no MAIL FROM attributes after configuration."
                )
            )
            ok = False
        else:
            if actual.mail_from_domain != requested_domain:
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ MAIL FROM domain mismatch: "
                        f"SES has {actual.mail_from_domain!r}, "
                        f"expected {requested_domain!r}"
                    )
                )
                ok = False
            if actual.behavior_on_mx_failure != "USE_DEFAULT_VALUE":
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ BehaviorOnMxFailure mismatch: "
                        f"SES has {actual.behavior_on_mx_failure!r}, "
                        f"expected 'USE_DEFAULT_VALUE'"
                    )
                )
                ok = False

        if ok:
            self.stdout.write(self.style.SUCCESS("  ✓ SES state verified"))
        else:
            errors.append(f"{domain}: SES state did not match what was requested")

    def _print_ses_state(self, label: str, info) -> None:
        if info is None:
            self.stdout.write(f"  {label}: (none)")
            return
        self.stdout.write(f"  {label}:")
        self.stdout.write(f"    Domain   : {info.mail_from_domain}")
        self.stdout.write(f"    Status   : {info.status}")
        self.stdout.write(f"    Behavior : {info.behavior_on_mx_failure}")
        if info.mx_value:
            self.stdout.write(
                f"    MX target: {info.mx_priority} {info.mx_value}"
            )

    def _print_diff(self, diff: dict) -> None:
        # diff values are DesiredRecord dataclass objects; use attribute access.
        for action, rows in (("ADD", diff.get("add", [])),
                              ("UPDATE", diff.get("update", [])),
                              ("REMOVE", diff.get("remove", []))):
            for row in rows:
                key = row.key
                rtype = row.type
                name = row.name
                value = row.value
                priority = row.priority
                p_str = f" pri={priority}" if priority is not None else ""
                self.stdout.write(f"  {action:6s}  {rtype:5s}  {key:6s}  {name}  {value[:60]}{p_str}")

    def _print_record_table(self, record: EmailDomain) -> None:
        rows = record.dns_records()
        if not rows:
            return
        self.stdout.write("")
        self.stdout.write(
            f"  {'Type':<5}  {'Host':<30}  {'Prio':>4}  Value"
        )
        self.stdout.write("  " + "-" * 80)
        for row in rows:
            host = (row.get("host") or "@")[:30]
            rtype = (row.get("record_type") or "")[:5]
            prio = str(row.get("priority") or "")
            value = (row.get("value") or "")[:60]
            ok_marker = "✓" if row.get("ok") else "✗"
            diag = row.get("diag") or {}
            diag_msg = diag.get("message", "") if not row.get("ok") else ""
            line = f"  {rtype:<5}  {host:<30}  {prio:>4}  {value}  {ok_marker}"
            if diag_msg:
                line += f"\n         ↳ {diag_msg}"
            self.stdout.write(line)
