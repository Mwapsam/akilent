"""Concrete Stalwart Mail Server provider implementation.

All Stalwart-specific API knowledge is confined to this file and
apps.email.providers.stalwart.adapters. The EmailProvider interface
(apps.email.providers.base) defines what every provider must support;
this class maps those contracts onto Stalwart's proprietary JMAP objects
(capability ``urn:stalwart:jmap``):

  - ``x:Domain``       — sending domains (confirmed live: get/set/query)
  - ``x:Account``       — mailboxes, ``@type: "User"`` (confirmed live:
                          get/set/query — ``credentials`` is a map keyed
                          by numeric string ids, e.g. ``{"0": {...}}``,
                          not an array)
  - ``x:MailingList``    — forwarding aliases (confirmed live: get/set —
                          ``recipients`` is a set encoded as
                          ``{"email@x.com": true, ...}``, not an array)

Credentials are resolved from Django settings by StalwartApiClient.from_settings().
Never hard-code credentials here.
"""

from __future__ import annotations

import logging
from typing import Any

from apps.email.exceptions import EmailProviderError, ResourceNotFoundError
from apps.email.providers.base import (
    EmailProvider,
    OperationResult,
)
from apps.email.providers.stalwart.adapters import (
    adapt_account_object,
    adapt_dkim_from_signature,
    adapt_domain_object,
    adapt_mailing_list_object,
    adapt_quota_account,
    choose_dkim_signature,
    ok,
)
from apps.email.providers.stalwart.client import StalwartApiClient
from apps.email.types import (
    AliasInfo,
    DkimRecord,
    DomainInfo,
    MailboxInfo,
    QuotaInfo,
)

logger = logging.getLogger(__name__)


class StalwartProvider(EmailProvider):
    """EmailProvider backed by a remote Stalwart Mail Server instance."""

    def __init__(self) -> None:
        self._client = StalwartApiClient.from_settings()

    # ── Generic x:* object primitives ───────────────────────────────────────
    # Every domain/mailbox/alias operation is a JMAP <Type>/get, <Type>/set
    # or <Type>/query call under the hood, where <Type> is e.g. "x:Domain".

    def _create_object(
        self, type_name: str, key: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        result = self._client.call(f"{type_name}/set", {"create": {key: properties}})
        not_created = result.get("notCreated") or {}
        if key in not_created:
            err = not_created[key]
            raise EmailProviderError(
                f"Stalwart rejected {type_name} create: {err}",
                code=f"{type_name}_not_created",
                details=err if isinstance(err, dict) else {},
            )
        created = result.get("created") or {}
        return created.get(key, {})

    def _update_object(
        self, type_name: str, object_id: str, patch: dict[str, Any]
    ) -> None:
        result = self._client.call(f"{type_name}/set", {"update": {object_id: patch}})
        not_updated = result.get("notUpdated") or {}
        if object_id in not_updated:
            err = not_updated[object_id]
            raise EmailProviderError(
                f"Stalwart rejected {type_name} update: {err}",
                code=f"{type_name}_not_updated",
                details=err if isinstance(err, dict) else {},
            )

    def _destroy_object(self, type_name: str, object_id: str) -> None:
        result = self._client.call(f"{type_name}/set", {"destroy": [object_id]})
        not_destroyed = result.get("notDestroyed") or {}
        if object_id in not_destroyed:
            err = not_destroyed[object_id]
            raise EmailProviderError(
                f"Stalwart rejected {type_name} destroy: {err}",
                code=f"{type_name}_not_destroyed",
                details=err if isinstance(err, dict) else {},
            )

    def _query_object_ids(
        self, type_name: str, filter_: dict[str, Any] | None = None
    ) -> list[str]:
        result = self._client.call(
            f"{type_name}/query", {"filter": filter_} if filter_ else {}
        )
        return list(result.get("ids") or [])

    def _get_objects(self, type_name: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        result = self._client.call(f"{type_name}/get", {"ids": ids})
        return list(result.get("list") or [])

    def _find_object(
        self, type_name: str, predicate, *, filter_: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        ids = self._query_object_ids(type_name, filter_)
        for obj in self._get_objects(type_name, ids):
            if predicate(obj):
                return obj
        return None

    # ── Domain lifecycle (x:Domain — confirmed live) ────────────────────────

    def _find_domain(self, domain: str) -> dict[str, Any] | None:
        return self._find_object("x:Domain", lambda d: d.get("name") == domain)

    def _require_domain(self, domain: str) -> dict[str, Any]:
        obj = self._find_domain(domain)
        if obj is None:
            raise ResourceNotFoundError("domain", domain)
        return obj

    def create_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str = "",
    ) -> DomainInfo:
        # Stalwart's x:Domain object has no max-accounts/disk-quota fields —
        # those limits are enforced in Django (apps.billing) before calling
        # this method, not pushed down to the mail server.
        properties: dict[str, Any] = {"name": domain}
        if description:
            properties["description"] = description

        self._create_object("x:Domain", "d1", properties)
        logger.info("stalwart: domain created - %s", domain)
        return self.get_domain(domain)

    def get_domain(self, domain: str) -> DomainInfo:
        obj = self._require_domain(domain)
        sigs = self._dkim_signatures_for_domain(obj["id"])
        chosen = choose_dkim_signature(sigs, "dkim")
        dkim = adapt_dkim_from_signature(chosen, domain) if chosen else None
        return adapt_domain_object(obj, domain, dkim=dkim)

    def _dkim_signatures_for_domain(self, domain_id: str) -> list[dict[str, Any]]:
        # x:DkimSignature/query doesn't support filtering on domainId
        # (confirmed live: "unsupportedFilter") — filter client-side.
        ids = self._query_object_ids("x:DkimSignature")
        return [
            s
            for s in self._get_objects("x:DkimSignature", ids)
            if s.get("domainId") == domain_id
        ]

    def update_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str | None = None,
    ) -> DomainInfo:
        obj = self._require_domain(domain)
        patch: dict[str, Any] = {}
        if description is not None:
            patch["description"] = description
        if patch:
            self._update_object("x:Domain", obj["id"], patch)
        return self.get_domain(domain)

    def delete_domain(self, domain: str) -> OperationResult:
        obj = self._require_domain(domain)
        domain_id = obj["id"]
        self._destroy_linked_dkim_signatures(domain_id)
        self._destroy_object("x:Domain", domain_id)
        logger.info("stalwart: domain deleted - %s", domain)
        return ok(f"Domain {domain} deleted.")

    def _destroy_linked_dkim_signatures(self, domain_id: str) -> None:
        # Domains with dkimManagement @type "Automatic" (the default for
        # newly created domains) own x:DkimSignature objects that block
        # domain destroy with "objectIsLinked" until removed first.
        linked_ids = [s["id"] for s in self._dkim_signatures_for_domain(domain_id)]
        if not linked_ids:
            return
        result = self._client.call("x:DkimSignature/set", {"destroy": linked_ids})
        not_destroyed = result.get("notDestroyed") or {}
        if not_destroyed:
            logger.warning(
                "stalwart: could not destroy linked DKIM signatures for domain %s: %s",
                domain_id,
                not_destroyed,
            )

    def list_domains(self) -> list[DomainInfo]:
        ids = self._query_object_ids("x:Domain")
        domain_objs = self._get_objects("x:Domain", ids)

        all_sig_ids = self._query_object_ids("x:DkimSignature")
        all_sigs = self._get_objects("x:DkimSignature", all_sig_ids)
        sigs_by_domain_id: dict[str, list[dict[str, Any]]] = {}
        for sig in all_sigs:
            sigs_by_domain_id.setdefault(sig.get("domainId") or "", []).append(sig)

        results = []
        for d in domain_objs:
            name = d.get("name", "")
            chosen = choose_dkim_signature(sigs_by_domain_id.get(d["id"], []), "dkim")
            dkim = adapt_dkim_from_signature(chosen, name) if chosen else None
            results.append(adapt_domain_object(d, name, dkim=dkim))
        return results

    def set_domain_active(self, domain: str, *, active: bool) -> OperationResult:
        obj = self._require_domain(domain)
        self._update_object("x:Domain", obj["id"], {"isEnabled": active})
        state = "enabled" if active else "disabled"
        logger.info("stalwart: domain %s - %s", domain, state)
        return ok(f"Domain {domain} {state}.")

    # ── DKIM management ───────────────────────────────────────────────────
    # Stalwart manages DKIM keys/rotation automatically per-domain
    # (x:Domain.dkimManagement, @type "Automatic") — keys are generated as
    # x:DkimSignature objects at domain-creation time (confirmed live:
    # available immediately, no propagation delay). There is no separate
    # provision/rotate endpoint to call. Note: x:Domain.dnsManagement.dnsZoneFile
    # is NEVER populated via the JMAP API (confirmed empty even on
    # progstack.org after a day in production) — do not read DKIM from it.

    def provision_dkim(
        self,
        domain: str,
        *,
        selector: str = "dkim",
        algorithm: str = "rsa-sha256",
    ) -> DkimRecord:
        return self.get_dkim(domain, selector=selector)

    def get_dkim(self, domain: str, *, selector: str = "dkim") -> DkimRecord:
        obj = self._require_domain(domain)
        sigs = self._dkim_signatures_for_domain(obj["id"])
        chosen = choose_dkim_signature(sigs, selector)
        if chosen is None:
            raise EmailProviderError(
                f"No DKIM signatures found for {domain}.",
                code="dkim_not_available",
            )
        return adapt_dkim_from_signature(chosen, domain)

    def rotate_dkim(
        self,
        domain: str,
        *,
        new_selector: str,
        algorithm: str = "rsa-sha256",
    ) -> DkimRecord:
        logger.warning(
            "stalwart: rotate_dkim no-op for %s — DKIM rotation is automatic "
            "on this server (x:Domain.dkimManagement); returning current key.",
            domain,
        )
        return self.get_dkim(domain, selector=new_selector)

    # ── Mailbox lifecycle (x:Account, @type "User") ─────────────────────────
    # Confirmed live: create/get round-tripped against the production server.

    def _find_account(self, email: str) -> dict[str, Any] | None:
        local, _, _domain = email.partition("@")
        return self._find_object(
            "x:Account",
            lambda a: a.get("emailAddress") == email or a.get("name") == local,
        )

    def _require_account(self, email: str) -> dict[str, Any]:
        obj = self._find_account(email)
        if obj is None:
            raise ResourceNotFoundError("mailbox", email)
        return obj

    def create_mailbox(
        self,
        email: str,
        password: str,
        *,
        name: str = "",
        quota_mb: int | None = None,
        description: str = "",
    ) -> MailboxInfo:
        local, _, domain = email.partition("@")
        domain_obj = self._require_domain(domain)
        properties: dict[str, Any] = {
            "@type": "User",
            "name": local,
            "domainId": domain_obj["id"],
            "credentials": {"0": {"@type": "Password", "secret": password}},
            "roles": {"@type": "User"},
            "permissions": {"@type": "Inherit"},
            "encryptionAtRest": {"@type": "Disabled"},
            "memberGroupIds": {},
            "aliases": {},
            "quotas": {},
        }
        if quota_mb:
            properties["quotas"] = {"maxDiskQuota": quota_mb * 1024 * 1024}
        if description:
            properties["description"] = description

        self._create_object("x:Account", "m1", properties)
        logger.info("stalwart: mailbox created - %s", email)
        return self.get_mailbox(email)

    def get_mailbox(self, email: str) -> MailboxInfo:
        obj = self._require_account(email)
        return adapt_account_object(obj, email)

    def update_mailbox(
        self,
        email: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> MailboxInfo:
        obj = self._require_account(email)
        patch: dict[str, Any] = {}
        if name is not None:
            patch["name"] = name
        if description is not None:
            patch["description"] = description
        if patch:
            self._update_object("x:Account", obj["id"], patch)
        return self.get_mailbox(email)

    def delete_mailbox(self, email: str) -> OperationResult:
        obj = self._require_account(email)
        self._destroy_object("x:Account", obj["id"])
        logger.info("stalwart: mailbox deleted - %s", email)
        return ok(f"Mailbox {email} deleted.")

    def list_mailboxes(self, domain: str) -> list[MailboxInfo]:
        domain_obj = self._require_domain(domain)
        ids = self._query_object_ids("x:Account", {"domainId": domain_obj["id"]})
        results = []
        for obj in self._get_objects("x:Account", ids):
            email = obj.get("emailAddress") or f"{obj.get('name', '')}@{domain}"
            results.append(adapt_account_object(obj, email))
        return results

    def set_mailbox_active(self, email: str, *, active: bool) -> OperationResult:
        # x:Account has no confirmed enable/disable field; suspension is
        # enforced via the Disable credential-permissions variant instead.
        obj = self._require_account(email)
        permissions = (
            {"@type": "Inherit"}
            if active
            else {
                "@type": "Replace",
                "enabledPermissions": [],
                "disabledPermissions": [],
            }
        )
        self._update_object("x:Account", obj["id"], {"permissions": permissions})
        state = "activated" if active else "suspended"
        logger.info("stalwart: mailbox %s - %s", email, state)
        return ok(f"Mailbox {email} {state}.")

    # ── Password management ───────────────────────────────────────────────

    def change_password(self, email: str, new_password: str) -> OperationResult:
        obj = self._require_account(email)
        self._update_object(
            "x:Account",
            obj["id"],
            {"credentials": {"0": {"@type": "Password", "secret": new_password}}},
        )
        logger.info("stalwart: password changed - %s", email)
        return ok(f"Password updated for {email}.")

    # ── Quota management ──────────────────────────────────────────────────

    def get_quota(self, email: str) -> QuotaInfo:
        obj = self._require_account(email)
        return adapt_quota_account(obj)

    def set_quota(self, email: str, quota_mb: int) -> OperationResult:
        obj = self._require_account(email)
        self._update_object(
            "x:Account", obj["id"], {"quotas": {"maxDiskQuota": quota_mb * 1024 * 1024}}
        )
        logger.info("stalwart: quota set - %s -> %d MB", email, quota_mb)
        return ok(f"Quota for {email} set to {quota_mb} MB.")

    # ── Alias lifecycle (x:MailingList) ──────────────────────────────────────
    # Modeled as a mailing list: a single address fanning out to one or more
    # recipient addresses. Confirmed live: create/get round-tripped.

    def _find_mailing_list(self, address: str) -> dict[str, Any] | None:
        return self._find_object(
            "x:MailingList", lambda obj: obj.get("emailAddress") == address
        )

    def _require_mailing_list(self, address: str) -> dict[str, Any]:
        obj = self._find_mailing_list(address)
        if obj is None:
            raise ResourceNotFoundError("alias", address)
        return obj

    def create_alias(
        self,
        address: str,
        targets: list[str],
        *,
        description: str = "",
    ) -> AliasInfo:
        local, _, domain = address.partition("@")
        domain_obj = self._require_domain(domain)
        properties: dict[str, Any] = {
            "name": local,
            "domainId": domain_obj["id"],
            "recipients": dict.fromkeys(targets, True),
        }
        if description:
            properties["description"] = description

        self._create_object("x:MailingList", "a1", properties)
        logger.info("stalwart: alias created - %s -> %s", address, targets)
        return self.get_alias(address)

    def get_alias(self, address: str) -> AliasInfo:
        obj = self._require_mailing_list(address)
        return adapt_mailing_list_object(obj, address)

    def update_alias(
        self,
        address: str,
        targets: list[str],
        *,
        description: str | None = None,
    ) -> AliasInfo:
        obj = self._require_mailing_list(address)
        patch: dict[str, Any] = {"recipients": dict.fromkeys(targets, True)}
        if description is not None:
            patch["description"] = description
        self._update_object("x:MailingList", obj["id"], patch)
        return self.get_alias(address)

    def delete_alias(self, address: str) -> OperationResult:
        obj = self._require_mailing_list(address)
        self._destroy_object("x:MailingList", obj["id"])
        logger.info("stalwart: alias deleted - %s", address)
        return ok(f"Alias {address} deleted.")

    def list_aliases(self, domain: str) -> list[AliasInfo]:
        # x:MailingList/query doesn't support filtering on domainId
        # (confirmed live: "unsupportedFilter") — filter client-side instead.
        domain_obj = self._require_domain(domain)
        ids = self._query_object_ids("x:MailingList")
        results = []
        for obj in self._get_objects("x:MailingList", ids):
            if obj.get("domainId") != domain_obj["id"]:
                continue
            address = obj.get("emailAddress") or f"{obj.get('name', '')}@{domain}"
            results.append(adapt_mailing_list_object(obj, address))
        return results
