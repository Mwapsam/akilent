"""Domain provisioning business logic.

DomainService is the authoritative place for:
  - Creating, updating, and deleting sending domains
  - Keeping EmailDomain (DB) in sync with the mail provider
  - Writing AuditLog entries for every mutation
  - Firing async Celery jobs for heavy provisioning work

Views and tasks import this service — never the provider directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.email.audit import record as audit
from apps.email.exceptions import EmailProviderError
from apps.email.models import EmailDomain
from apps.email.providers import get_mail_provider
from apps.email.types import (
    SES_MAIL_FROM_SPF,
    DkimRecord,
    DomainInfo,
    MailFromInfo,
    OperationResult,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)

MAIL_FROM_SUBDOMAIN = "bounce"
MAIL_FROM_MX_PRIORITY = 10


@dataclass(frozen=True)
class DesiredRecord:
    """One DNS record we want the tenant to publish."""

    key: str
    type: str
    name: str
    value: str
    priority: int | None = None

    @property
    def ident(self) -> tuple[str, str]:
        return (self.key, self.name)


def desired_dns_records(
    record: EmailDomain,
    dkim_records: list[DkimRecord],
    mail_from: MailFromInfo | None,
) -> list[DesiredRecord]:
    """The full DNS spec for an SES-backed domain. Pure: no I/O.

    There is deliberately no root-domain SPF record. SES evaluates SPF against
    the MAIL FROM (return-path) domain, never the root, so a root record does
    nothing for SES mail -- and on a domain that already publishes SPF (e.g. for
    its inbox provider) it would be a *second* SPF record, which receivers
    treat as a permanent error for everything sent from that domain.
    """
    from apps.email.models import EmailDnsRecord as R

    rows = [
        DesiredRecord(
            R.Key.VERIFY,
            R.RecordType.TXT,
            record.verify_record_name or record.domain,
            record.verify_record_value,
        ),
    ]
    rows += [
        DesiredRecord(R.Key.DKIM, R.RecordType.CNAME, d.record_name, d.public_key_txt)
        for d in dkim_records
    ]
    if mail_from and mail_from.mail_from_domain:
        rows += [
            DesiredRecord(
                R.Key.MAIL_FROM_MX,
                R.RecordType.MX,
                mail_from.mail_from_domain,
                mail_from.mx_value,
                mail_from.mx_priority,
            ),
            DesiredRecord(
                R.Key.MAIL_FROM_SPF,
                R.RecordType.TXT,
                mail_from.mail_from_domain,
                mail_from.spf_value,
            ),
        ]
    rows.append(
        DesiredRecord(
            R.Key.DMARC,
            R.RecordType.TXT,
            record.dmarc_record_name,
            record.dmarc_value,
        )
    )
    return rows


def diff_dns_records(existing: dict, desired: list[DesiredRecord]) -> dict:
    """Compare stored rows (keyed by (key, name)) against the desired spec.

    Returns ``{"add": [...], "update": [...], "remove": [...]}``. Pure, so the
    dry-run command can show exactly what a real run would change.
    """
    wanted = {d.ident: d for d in desired}
    add, update = [], []
    for ident, d in wanted.items():
        row = existing.get(ident)
        if row is None:
            add.append(d)
        elif (row.record_type, row.value, row.priority) != (
            d.type,
            d.value,
            d.priority,
        ):
            update.append(d)
    remove = [row for ident, row in existing.items() if ident not in wanted]
    return {"add": add, "update": update, "remove": remove}


class DomainService:
    """Orchestrates domain provisioning between Django and the mail provider."""

    def __init__(
        self, account, *, actor: AbstractBaseUser | None = None, provider=None
    ) -> None:
        self.account = account
        self.actor = actor
        self._provider = provider if provider is not None else get_mail_provider()

    # ── Public API ────────────────────────────────────────────────────────

    def provision(
        self,
        domain_record: EmailDomain,
        *,
        selector: str | None = None,
    ) -> DomainInfo:
        """Create the domain on the mail server and populate DKIM fields.

        Updates domain_record in place (dkim_public_key, status) and saves it.
        Raises EmailProviderError on failure — caller decides how to surface it.
        """
        selector = selector or domain_record.dkim_selector or "dkim"
        try:
            info = self._provider.create_domain(
                domain_record.domain,
                description=f"Automator account {self.account.pk}",
            )
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action="domain.provision",
                resource_type="domain",
                resource_id=domain_record.domain,
                success=False,
                error="Provider error during create_domain",
            )
            raise

        dkim = info.dkim
        if dkim:
            domain_record.dkim_public_key = dkim.public_key_txt
            domain_record.dkim_selector = dkim.selector
            domain_record.save(update_fields=["dkim_public_key", "dkim_selector"])
        elif hasattr(self._provider, "get_dkim_records"):
            # Multi-record DKIM (AWS SES Easy DKIM = 3 CNAMEs): the tokens aren't
            # available at create time, so fetch them now and materialise the
            # full DNS spec as EmailDnsRecord rows for the customer to publish.
            # MAIL FROM first so its records are part of that spec; it never
            # fails provisioning.
            self.ensure_mail_from(domain_record)
            self.sync_dns_records(domain_record)

        audit(
            account=self.account,
            actor=self.actor,
            action="domain.provision",
            resource_type="domain",
            resource_id=domain_record.domain,
            metadata={"selector": selector},
        )
        logger.info(
            "DomainService.provision: %s provisioned (account=%s)",
            domain_record.domain,
            self.account.pk,
        )
        return info

    def enable(self, domain_record: EmailDomain) -> OperationResult:
        """Enable a previously disabled domain."""
        return self._toggle(domain_record, active=True)

    def disable(self, domain_record: EmailDomain) -> OperationResult:
        """Disable a domain without deleting it."""
        return self._toggle(domain_record, active=False)

    def deprovision(self, domain_record: EmailDomain) -> OperationResult:
        """Remove the domain from the mail server.

        Does not delete the Django EmailDomain row — caller decides that.
        """
        try:
            result = self._provider.delete_domain(domain_record.domain)
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action="domain.deprovision",
                resource_type="domain",
                resource_id=domain_record.domain,
                success=False,
                error="Provider error during delete_domain",
            )
            raise

        audit(
            account=self.account,
            actor=self.actor,
            action="domain.deprovision",
            resource_type="domain",
            resource_id=domain_record.domain,
        )
        return result

    def rotate_dkim(
        self,
        domain_record: EmailDomain,
        *,
        new_selector: str,
    ) -> DkimRecord:
        """Generate a new DKIM keypair under a new selector.

        Updates dkim_public_key and dkim_selector in the DB after success.
        The old selector continues to work until DNS TTL expires.
        """
        try:
            record = self._provider.rotate_dkim(
                domain_record.domain,
                new_selector=new_selector,
            )
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action="domain.rotate_dkim",
                resource_type="domain",
                resource_id=domain_record.domain,
                success=False,
                error=f"Provider error rotating DKIM to selector {new_selector!r}",
            )
            raise

        domain_record.dkim_public_key = record.public_key_txt
        domain_record.dkim_selector = record.selector
        domain_record.save(update_fields=["dkim_public_key", "dkim_selector"])

        audit(
            account=self.account,
            actor=self.actor,
            action="domain.rotate_dkim",
            resource_type="domain",
            resource_id=domain_record.domain,
            metadata={
                "old_selector": domain_record.dkim_selector,
                "new_selector": new_selector,
            },
        )
        return record

    # ── MAIL FROM + DNS spec ──────────────────────────────────────────────

    def ensure_mail_from(
        self, domain_record: EmailDomain, *, subdomain: str = MAIL_FROM_SUBDOMAIN
    ) -> MailFromInfo | None:
        """Configure the custom MAIL FROM on the provider. Never raises.

        A MAIL FROM failure must never fail provisioning: the domain is usable
        without it (SES falls back to its own return path). Failures are logged
        and audited, and the refresh loop retries later.
        """
        configure = getattr(self._provider, "configure_mail_from", None)
        if configure is None:
            return None
        domain_record.mail_from_attempted_at = timezone.now()
        try:
            info = configure(domain_record.domain, subdomain=subdomain)
        except Exception as exc:
            logger.exception("ensure_mail_from failed for %s", domain_record.domain)
            domain_record.save(update_fields=["mail_from_attempted_at"])
            audit(
                account=self.account,
                actor=self.actor,
                action="domain.mail_from",
                resource_type="domain",
                resource_id=domain_record.domain,
                success=False,
                error=str(exc)[:500],
            )
            return None
        if info is None:  # the provider doesn't support it
            domain_record.save(update_fields=["mail_from_attempted_at"])
            return None
        domain_record.mail_from_domain = info.mail_from_domain
        domain_record.mail_from_status = info.status or domain_record.mail_from_status
        domain_record.save(
            update_fields=[
                "mail_from_domain",
                "mail_from_status",
                "mail_from_attempted_at",
            ]
        )
        audit(
            account=self.account,
            actor=self.actor,
            action="domain.mail_from",
            resource_type="domain",
            resource_id=domain_record.domain,
            metadata={"mail_from_domain": info.mail_from_domain},
        )
        return info

    def _mail_from_spec(
        self, domain_record: EmailDomain, mail_from_domain: str | None = None
    ) -> MailFromInfo | None:
        """The MAIL FROM records to publish for this domain, if it has one."""
        name = mail_from_domain or domain_record.mail_from_domain
        if not name:
            return None
        mx_for = getattr(self._provider, "_mail_from_mx", None)
        if mx_for is not None:
            mx_value = mx_for()
        else:
            from apps.core.models import MailProviderSettings

            region = MailProviderSettings.load().aws_region or "us-east-1"
            mx_value = f"feedback-smtp.{region}.amazonses.com"
        return MailFromInfo(
            mail_from_domain=name,
            mx_value=mx_value,
            mx_priority=MAIL_FROM_MX_PRIORITY,
            spf_value=SES_MAIL_FROM_SPF,
        )

    def planned_dns_records(
        self, domain_record: EmailDomain, *, mail_from_domain: str | None = None
    ) -> tuple[list[DesiredRecord], dict]:
        """(desired spec, diff against stored rows). Reads only; writes nothing.

        ``mail_from_domain`` lets the dry run show the spec as it will be once
        MAIL FROM is configured. If the provider returns no DKIM tokens, the
        stored DKIM rows are carried over rather than planned for removal: an
        empty answer from the provider is never a reason to delete them.
        """
        from apps.email.models import EmailDnsRecord

        dkim_records = self._provider.get_dkim_records(domain_record.domain)  # type: ignore[union-attr]
        desired = desired_dns_records(
            domain_record,
            dkim_records,
            self._mail_from_spec(domain_record, mail_from_domain),
        )
        existing = {(r.key, r.name): r for r in domain_record.dns_record_rows.all()}
        if not dkim_records:
            carried = [
                DesiredRecord(r.key, r.record_type, r.name, r.value, r.priority)
                for (key, _name), r in existing.items()
                if key == EmailDnsRecord.Key.DKIM
            ]
            # Keep DKIM straight after verify, as desired_dns_records orders it.
            desired = desired[:1] + carried + desired[1:]
        return desired, diff_dns_records(existing, desired)

    def sync_dns_records(self, domain_record: EmailDomain) -> dict:
        """Make the stored EmailDnsRecord rows match the desired spec.

        Adds missing rows, updates changed ones (resetting their found state),
        and removes rows that are no longer wanted -- e.g. a root SPF row from
        before MAIL FROM existed, or a rotated DKIM token. Returns the diff.
        """
        from apps.email.models import EmailDnsRecord

        if domain_record.ensure_verification_token():
            domain_record.save(
                update_fields=["verify_record_name", "verify_record_value"]
            )

        _desired, diff = self.planned_dns_records(domain_record)
        with transaction.atomic():
            for d in diff["add"]:
                EmailDnsRecord.objects.create(
                    domain=domain_record,
                    key=d.key,
                    name=d.name,
                    record_type=d.type,
                    value=d.value,
                    priority=d.priority,
                )
            for d in diff["update"]:
                EmailDnsRecord.objects.filter(
                    domain=domain_record, key=d.key, name=d.name
                ).update(
                    record_type=d.type,
                    value=d.value,
                    priority=d.priority,
                    is_ok=False,
                    checked_at=None,
                )
            if diff["remove"]:
                EmailDnsRecord.objects.filter(
                    pk__in=[r.pk for r in diff["remove"]]
                ).delete()
        return diff

    # Back-compat for any caller still using the old private name.
    _sync_dns_records = sync_dns_records

    def ensure_dns_spec(self, domain_record: EmailDomain) -> bool:
        """Bring an existing domain up to the current spec, if it needs it.

        Called on every refresh; cheap when nothing is wrong, and rate-limited
        so the card's 20-second poll doesn't hit the provider each time.
        Returns True when a resync ran. Never raises.
        """
        from apps.email.models import EmailDnsRecord as R

        if not hasattr(self._provider, "get_dkim_records"):
            return False
        keys = set(domain_record.dns_record_rows.values_list("key", flat=True))
        needs = (
            R.Key.DKIM not in keys
            or not domain_record.mail_from_domain
            or R.Key.MAIL_FROM_MX not in keys
            or R.Key.MAIL_FROM_SPF not in keys
            or R.Key.SPF in keys  # stale root SPF row from before MAIL FROM
        )
        if not needs:
            return False
        if not cache.add(f"dnsspec:{domain_record.pk}", 1, 300):
            return False
        try:
            if not domain_record.mail_from_domain:
                self.ensure_mail_from(domain_record)
            self.sync_dns_records(domain_record)
            return True
        except Exception:
            logger.exception("ensure_dns_spec failed for %s", domain_record.domain)
            return False

    # ── Private helpers ───────────────────────────────────────────────────

    def _toggle(self, domain_record: EmailDomain, *, active: bool) -> OperationResult:
        action = "domain.enable" if active else "domain.disable"
        try:
            result = self._provider.set_domain_active(
                domain_record.domain, active=active
            )
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action=action,
                resource_type="domain",
                resource_id=domain_record.domain,
                success=False,
                error="Provider error during set_domain_active",
            )
            raise

        domain_record.is_active = active
        domain_record.save(update_fields=["is_active"])

        audit(
            account=self.account,
            actor=self.actor,
            action=action,
            resource_type="domain",
            resource_id=domain_record.domain,
        )
        return result
