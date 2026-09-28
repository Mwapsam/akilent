"""Provider-agnostic normalized response types.

All EmailProvider methods return instances of these dataclasses — never raw
provider-specific dicts. This is the contract between the provider layer and
Django business logic: swapping the mail server requires only a new provider
class; services, views, and tasks remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class MailboxStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING = "pending"


class DomainStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    PENDING = "pending"


@dataclass(frozen=True)
class DkimRecord:
    """DKIM public-key information for a domain.

    The private key never leaves the mail server. Django stores only
    public_key_txt in EmailDomain.dkim_public_key for display purposes.
    """

    selector: str
    algorithm: str  # e.g. "rsa-sha256"
    public_key_txt: str  # full DNS TXT value: "v=DKIM1; k=rsa; p=..."
    record_name: str  # e.g. "dkim._domainkey.example.com"


@dataclass(frozen=True)
class QuotaInfo:
    """Mailbox storage quota details."""

    used_mb: float
    limit_mb: int  # 0 = unlimited

    @property
    def used_percent(self) -> float:
        if not self.limit_mb:
            return 0.0
        return round((self.used_mb / self.limit_mb) * 100, 2)

    @property
    def is_unlimited(self) -> bool:
        return self.limit_mb == 0


@dataclass(frozen=True)
class DomainInfo:
    """Normalized domain representation returned by provider domain methods."""

    domain: str
    status: DomainStatus
    dkim: DkimRecord | None = None
    max_accounts: int | None = None
    disk_quota_mb: int | None = None
    description: str = ""
    created_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == DomainStatus.ACTIVE


@dataclass(frozen=True)
class MailboxInfo:
    """Normalized mailbox/account representation."""

    email: str
    name: str
    status: MailboxStatus
    quota: QuotaInfo
    is_admin: bool = False
    description: str = ""
    created_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == MailboxStatus.ACTIVE

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1]


@dataclass(frozen=True)
class AliasInfo:
    """Normalized alias/forwarding-group representation."""

    address: str
    targets: list[str] = field(default_factory=list)
    is_active: bool = True
    description: str = ""
    created_at: datetime | None = None

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[-1]


@dataclass(frozen=True)
class OperationResult:
    """Generic result for mutating operations that don't return a resource."""

    success: bool
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# SPF value published at the custom MAIL FROM subdomain for SES.
SES_MAIL_FROM_SPF = "v=spf1 include:amazonses.com ~all"


@dataclass(frozen=True)
class MailFromInfo:
    """Provider-side custom MAIL FROM (return-path) configuration for a domain.

    ``status`` mirrors SES MailFromDomainStatus: PENDING / SUCCESS / FAILED /
    TEMPORARY_FAILURE. ``mx_value``/``spf_value`` are the records the tenant
    must publish at ``mail_from_domain``.
    """

    mail_from_domain: str
    status: str = ""
    behavior_on_mx_failure: str = ""
    mx_value: str = ""
    mx_priority: int = 10
    spf_value: str = SES_MAIL_FROM_SPF


@dataclass(frozen=True)
class Attachment:
    """One file attached to an outbound message. ``content`` is the raw bytes."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class OutboundEmail:
    """A single fully-rendered message ready to hand to a send provider.

    Rendering (merge tags, tracking pixel/link rewriting) happens before this
    is constructed — send providers only ever see final content.
    """

    from_email: str
    to_email: str
    subject: str
    text_body: str = ""
    html_body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class SendResult:
    """Normalized result of handing an OutboundEmail to a send provider."""

    success: bool
    provider_message_id: str = ""
    error: str = ""
