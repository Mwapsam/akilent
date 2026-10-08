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
    # Channel key this page documents, or None for channel-agnostic pages.
    channel: str | None = None

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
        channel="email",
    ),
    DocPage(
        "templates",
        "Templates",
        "Create and manage reusable email templates for /api/v1/templates.",
        "API reference",
        channel="email",
    ),
    DocPage(
        "campaigns",
        "Campaigns",
        "Send to many recipients at once with POST /api/v1/campaigns.",
        "API reference",
        channel="email",
    ),
    DocPage(
        "whatsapp-codes",
        "WhatsApp one-time codes",
        "Deliver login and confirmation codes on WhatsApp with POST /api/v1/whatsapp/verification-codes.",
        "API reference",
        channel="whatsapp",
    ),
    DocPage(
        "smtp",
        "SMTP relay",
        "Send directly over SMTP using a per-domain relay credential.",
        "API reference",
        channel="email",
    ),
    DocPage(
        "webhooks",
        "Webhooks",
        "Subscribe to delivery, open, and click events with signed callbacks.",
        "API reference",
        channel="email",
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


def visible_pages() -> list:
    from apps.core.channels import enabled_channels

    channels = enabled_channels()
    return [p for p in PAGES if p.channel is None or p.channel in channels]


def get_page(slug: str):
    page = _BY_SLUG.get(slug)
    return page if page is not None and page in visible_pages() else None


def neighbors(page: DocPage):
    """Previous/next pages in reading order, for the pager at the foot of each page."""
    pages = visible_pages()
    i = pages.index(page)
    prev_p = pages[i - 1] if i > 0 else None
    next_p = pages[i + 1] if i < len(pages) - 1 else None
    return prev_p, next_p
