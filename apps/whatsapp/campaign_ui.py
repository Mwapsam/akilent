"""What the WhatsApp campaign screens show before and after a send.

Kept apart from ``apps.whatsapp.campaigns`` (which sends) so the owner's view —
"will this reach people, and who won't get it, and why" — is computed with the
same rules the send applies (``reachability`` + ``variables.resolve``), without
the screens reaching into the fan-out code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign

# Resolving every blank for every recipient is exact but costs a row read each;
# past this many we check a sample and say so.
_RESOLVE_CHECK_LIMIT = 2000

SKIP_REASON_COPY = {
    "not_opted_in": (
        "Haven't opted in",
        "This is a promotional (marketing) message, so it only goes to people who agreed to "
        "hear from you.",
    ),
    "opted_out": ("Opted out", "They replied STOP, so they're never sent campaigns."),
    "no_whatsapp_identity": (
        "No WhatsApp number",
        "They don't have a phone number saved.",
    ),
    "missing_value": (
        "Missing details",
        "The message needs something we don't have for them (like their name) and no "
        "fallback was set.",
    ),
    "cancelled": (
        "Cancelled",
        "The campaign was cancelled before their message went out.",
    ),
    "plan_limit": (
        "Plan limit",
        "Your plan's monthly WhatsApp limit stayed full for days, so their message "
        "wasn't sent late.",
    ),
    "not_on_whatsapp": (
        "Not on WhatsApp",
        "WhatsApp told us this number isn't on WhatsApp.",
    ),
    "template_stopped": (
        "Template stopped",
        "Meta paused or rejected the message template while the campaign was sending.",
    ),
}

STATUS_STATE = {
    WhatsAppCampaign.Status.DRAFT: "draft",
    WhatsAppCampaign.Status.SCHEDULED: "scheduled",
    WhatsAppCampaign.Status.QUEUED: "sending",
    WhatsAppCampaign.Status.SENDING: "sending",
    WhatsAppCampaign.Status.PAUSED: "paused",
    WhatsAppCampaign.Status.COMPLETED: "sent",
    WhatsAppCampaign.Status.FAILED: "failed",
    WhatsAppCampaign.Status.CANCELLED: "cancelled",
}

EVENT_COPY = {
    "drafted": "Saved as a draft",
    "edited": "Draft updated",
    "scheduled": "Scheduled",
    "sent": "Sent",
    "started": "Started sending",
    "paused": "Paused",
    "resumed": "Resumed",
    "cancelled": "Cancelled",
    "completed": "Every message handed to WhatsApp",
    "failed": "Stopped with a problem",
}


def timeline(campaign) -> list[dict]:
    """The history entries as the detail page shows them, oldest first."""
    from django.utils.dateparse import parse_datetime

    out = []
    for entry in campaign.history or []:
        at = parse_datetime(entry.get("at") or "")
        when = parse_datetime(entry.get("when") or "") if entry.get("when") else None
        label = EVENT_COPY.get(entry.get("event", ""), entry.get("event", ""))
        if when is not None:
            label = f"{label} for {local_label(when, campaign.timezone)}"
        out.append(
            {
                "at": at,
                "label": label,
                "by": entry.get("by", ""),
                "when": when,
                "recipients": entry.get("recipients"),
                "not_sent": entry.get("not_sent"),
            }
        )
    return out


def local_label(dt, tz: str) -> str:
    """``dt`` as the owner reads it in ``tz``: "Fri 3 Oct 2026, 09:00"."""
    from zoneinfo import ZoneInfo

    if dt is None:
        return ""
    try:
        local = dt.astimezone(ZoneInfo(tz or "UTC"))
    except (ValueError, KeyError):
        local = dt
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M}"


def lifecycle_state(campaign) -> str:
    """The design-spec status word (§2) for a campaign: draft/sending/sent/failed."""
    return STATUS_STATE.get(campaign.status, "draft")


@dataclass
class Readiness:
    number_connected: bool
    has_template: bool
    has_text_template: bool
    has_ready_list: bool

    @property
    def ready(self) -> bool:
        return self.number_connected and self.has_text_template and self.has_ready_list

    @property
    def steps(self) -> list[dict]:
        return [
            {
                "done": self.number_connected,
                "title": "Connect your WhatsApp number",
                "url": "/whatsapp/numbers/",
                "action": "Connect",
            },
            {
                "done": self.has_text_template,
                "title": "Get a message template approved by Meta",
                "hint": (
                    "Your approved templates all have a picture or file at the top — "
                    "campaigns need one with text only."
                    if self.has_template and not self.has_text_template
                    else "WhatsApp only lets businesses start a conversation with an approved "
                    "template. Approval usually takes minutes."
                ),
                "url": "/whatsapp/templates/new/"
                if not self.has_template
                else "/email/templates/",
                "action": "Create a template"
                if not self.has_template
                else "See templates",
            },
            {
                "done": self.has_ready_list,
                "title": "Make a list of people to send to",
                "hint": "Paste numbers, upload a spreadsheet or pick from your customers.",
                "url": "/contacts/lists/",
                "action": "Make a list",
            },
        ]


def readiness(account) -> Readiness:
    from apps.contacts.models import ContactList
    from apps.whatsapp.models import WhatsAppBusinessNumber

    approved = MessageTemplate.objects.filter(
        account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED
    )
    return Readiness(
        number_connected=WhatsAppBusinessNumber.objects.filter(
            account=account, is_active=True
        ).exists(),
        has_template=approved.exists(),
        has_text_template=approved.filter(
            header_format=MessageTemplate.HeaderFormat.TEXT
        ).exists(),
        has_ready_list=ContactList.objects.filter(
            account=account, contacts__phone__isnull=False
        ).exists(),
    )


def segment_options(account) -> list:
    """Every saved segment with who it reaches right now, for the audience step."""
    from apps.contacts import reachability, segments
    from apps.contacts.models import Segment

    out = list(Segment.objects.filter(account=account).order_by("name"))
    for seg in out:
        try:
            qs = segments.contacts_for(seg.definition or {}, account)
            seg.reach = reachability.counts(  # type: ignore[attr-defined]
                qs.model.objects.filter(pk__in=qs.values("pk"))
            )
            seg.broken = False  # type: ignore[attr-defined]
        except segments.SegmentError:
            seg.reach = {"total": 0}  # type: ignore[attr-defined]
            seg.broken = True  # type: ignore[attr-defined]
    return out


def list_options(account) -> list:
    """Every list with its WhatsApp reachability counts, for the audience step."""
    from apps.contacts import reachability
    from apps.contacts.models import ContactList

    lists = list(ContactList.objects.filter(account=account).order_by("name"))
    for cl in lists:
        cl.reach = reachability.counts(cl.contacts.all())  # type: ignore[attr-defined]
    return lists


def template_options(account) -> list:
    templates = list(
        MessageTemplate.objects.filter(
            account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED
        ).order_by("name")
    )
    for t in templates:
        t.sendable = t.header_format == MessageTemplate.HeaderFormat.TEXT  # type: ignore[attr-defined]
        t.is_marketing = t.category == MessageTemplate.Category.MARKETING  # type: ignore[attr-defined]
    return templates


def render_body(template, params: dict) -> str:
    """The template body with this customer's values in its blanks."""
    text = template.content or ""
    for i, var in enumerate(template.variables or [], start=1):
        value = str(params.get(var, "") or "")
        text = text.replace(f"{{{{{i}}}}}", value).replace(f"{{{{{var}}}}}", value)
    return text


def preview_parts(template, params: dict) -> dict:
    header = template.header or ""
    if header and template.variables:
        header = re.sub(
            r"\{\{\s*1\s*\}\}", str(params.get(template.variables[0], "")), header
        )
    return {
        "header": header,
        "body": render_body(template, params),
        "footer": template.footer or "",
        "buttons": [b.get("text") for b in (template.buttons or []) if b.get("text")],
    }


@dataclass
class Estimate:
    total: int = 0
    will_receive: int = 0
    not_opted_in: int = 0
    opted_out: int = 0
    no_whatsapp: int = 0
    missing_value: int = 0
    missing_sampled: bool = False
    preview: dict = field(default_factory=dict)
    preview_for: str = ""

    @property
    def excluded(self) -> list[tuple[str, int, str, str]]:
        rows = [
            ("not_opted_in", self.not_opted_in),
            ("opted_out", self.opted_out),
            ("no_whatsapp_identity", self.no_whatsapp),
            ("missing_value", self.missing_value),
        ]
        return [(key, n, *SKIP_REASON_COPY[key]) for key, n in rows if n]


def estimate(account, audience, template, mapping: dict, fallbacks: dict) -> Estimate:
    """Who this audience + template + blanks would reach — the same rules as the send.

    ``audience`` is the Contact queryset of a list or a segment
    (``apps.whatsapp.campaigns.audience_contacts``).
    """
    from apps.automation import variables
    from apps.contacts import reachability

    # Re-rooted on pk so a segment's distinct()/joins can't skew the counts.
    base = audience.model.objects.filter(account=account, pk__in=audience.values("pk"))
    members = reachability.annotate(base)
    counts = reachability.counts(base)
    est = Estimate(total=counts["total"])
    est.opted_out = counts[reachability.OPTED_OUT]
    est.no_whatsapp = counts[reachability.NONE]
    marketing = template.category == MessageTemplate.Category.MARKETING
    if marketing:
        est.not_opted_in = counts[reachability.UPDATES]
        eligible = members.filter(wa_state=reachability.OPTED_IN)
    else:
        eligible = members.filter(
            wa_state__in=[reachability.OPTED_IN, reachability.UPDATES]
        )
    eligible_count = counts[reachability.OPTED_IN] + (
        0 if marketing else counts[reachability.UPDATES]
    )

    sample = list(eligible.order_by("-last_engaged_at", "pk")[:_RESOLVE_CHECK_LIMIT])
    est.missing_sampled = eligible_count > len(sample)
    first_ok = None
    for contact in sample:
        try:
            params = variables.resolve(
                template.variables,
                mapping,
                contact=contact,
                account=account,
                context={},
                fallbacks=fallbacks,
            )
        except variables.MissingValue:
            est.missing_value += 1
            continue
        if first_ok is None:
            first_ok = (contact, params)
    if est.missing_sampled and sample:
        est.missing_value = round(est.missing_value * eligible_count / len(sample))
    est.will_receive = max(eligible_count - est.missing_value, 0)

    if first_ok is not None:
        contact, params = first_ok
        est.preview = preview_parts(template, params)
        est.preview_for = contact.full_name or contact.phone or ""
    else:
        est.preview = preview_parts(
            template,
            {v: (fallbacks.get(v) or f"[{v}]") for v in (template.variables or [])},
        )
    return est
