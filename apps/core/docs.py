"""Static developer documentation registry.

Pages are curated, server-rendered guides (bodies live in
``templates/docs/<slug>.html``) covering the public Developer Platform: the
versioned REST API, SMTP relay, and webhooks — plus the auth/error/rate-limit
model shared across all of them. No database, no admin churn, same pattern
as ``apps.core.help``.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DocPage:
    slug: str
    title: str
    summary: str
    section: str

    @property
    def template(self) -> str:
        return f"docs/{self.slug}.html"


# Sections must stay contiguous within PAGES — the sidebar groups them with
# {% regroup %}, which only groups consecutive items.
PAGES = [
    DocPage(
        "index",
        "Developer Platform",
        "A versioned REST API, per-domain SMTP relay, and signed webhooks for transactional email.",
        "Getting started",
    ),
    DocPage(
        "authentication",
        "Authentication",
        "API keys and SMTP relay credentials — creating, rotating, and revoking them.",
        "Getting started",
    ),
    DocPage(
        "messages",
        "Send a message",
        "POST /api/v1/messages — request, response, and status codes.",
        "API reference",
    ),
    DocPage(
        "templates",
        "Templates",
        "Create and manage reusable email templates for /api/v1/templates.",
        "API reference",
    ),
    DocPage(
        "campaigns",
        "Campaigns",
        "Send to many recipients at once with POST /api/v1/campaigns.",
        "API reference",
    ),
    DocPage(
        "whatsapp-codes",
        "WhatsApp one-time codes",
        "Deliver login and confirmation codes on WhatsApp with POST /api/v1/whatsapp/verification-codes.",
        "API reference",
    ),
    DocPage(
        "smtp",
        "SMTP relay",
        "Send directly over SMTP using a per-domain relay credential.",
        "API reference",
    ),
    DocPage(
        "webhooks",
        "Webhooks",
        "Subscribe to delivery, open, and click events with signed callbacks.",
        "API reference",
    ),
    DocPage(
        "errors",
        "Error codes",
        'The {"error": {"code", "message"}} envelope and what each code means.',
        "Platform",
    ),
    DocPage(
        "rate-limits",
        "Rate limits",
        "Per-minute request throttling vs. the monthly volume cap — two different layers.",
        "Platform",
    ),
    DocPage(
        "changelog",
        "Versioning & changelog",
        "How API versions work, and what's changed since launch.",
        "Platform",
    ),
]

_BY_SLUG = {p.slug: p for p in PAGES}


def get_page(slug: str):
    return _BY_SLUG.get(slug)


def neighbors(page: DocPage):
    """Previous/next pages in reading order, for the pager at the foot of each page."""
    i = PAGES.index(page)
    prev_p = PAGES[i - 1] if i > 0 else None
    next_p = PAGES[i + 1] if i < len(PAGES) - 1 else None
    return prev_p, next_p
