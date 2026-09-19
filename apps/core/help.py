"""In-app knowledge base registry.

Articles are curated, server-rendered guides (bodies live in
``templates/help/articles/<slug>.html``). This module holds their metadata,
category ordering, and a tiny keyword search — no database, no admin churn.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Article:
    slug: str
    title: str
    summary: str
    category: str
    icon: str = "book-open"
    keywords: tuple = ()
    # Slugs of related guides shown at the foot of the article.
    related: tuple = ()

    @property
    def template(self) -> str:
        return f"help/articles/{self.slug}.html"

    def haystack(self) -> str:
        return " ".join((self.title, self.summary, self.category, *self.keywords)).lower()


# Display order for the index.
CATEGORIES = [
    "Getting started", "Email setup", "WhatsApp", "Account & team", "Troubleshooting",
]

ARTICLES = [
    Article(
        "getting-started",
        "Getting started",
        "Set up your workspace and send your first email in a few quick steps.",
        "Getting started", icon="sparkles",
        keywords=("intro", "setup", "quickstart", "begin", "first", "overview"),
        related=("domains", "dns-setup", "mailboxes"),
    ),
    Article(
        "domains",
        "Add & configure a sending domain",
        "Provision a domain, understand each status, and enable or disable sending.",
        "Email setup", icon="globe",
        keywords=("domain", "sending", "provision", "verify", "verified", "pending", "disable"),
        related=("dns-setup", "mailboxes", "troubleshooting"),
    ),
    Article(
        "dns-setup",
        "Set up your DNS records",
        "Add the DKIM, SPF, DMARC and verification records at your DNS provider, step by step.",
        "Email setup", icon="globe",
        keywords=("dns", "txt", "dkim", "spf", "dmarc", "cloudflare", "godaddy",
                  "namecheap", "google domains", "records", "nameserver", "cname", "host"),
        related=("domains", "troubleshooting"),
    ),
    Article(
        "mailboxes",
        "Create mailboxes & aliases",
        "Add real mailboxes with quotas and passwords, and forward mail with aliases.",
        "Email setup", icon="inbox",
        keywords=("mailbox", "alias", "quota", "storage", "password", "forward", "inbox", "user@"),
        related=("domains", "security"),
    ),
    Article(
        "whatsapp-setup",
        "Connect & troubleshoot WhatsApp",
        "Connect a number through Meta, register it, send a test message, and fix common errors.",
        "WhatsApp", icon="chat",
        keywords=("whatsapp", "meta", "waba", "cloud api", "register", "registration", "133010",
                  "token", "webhook", "test message", "business verification", "phone number"),
        related=("troubleshooting", "getting-started"),
    ),
    Article(
        "team",
        "Invite & manage your team",
        "Invite teammates by email, assign roles, and remove members.",
        "Account & team", icon="user",
        keywords=("team", "user", "invite", "invitation", "role", "member", "admin",
                  "owner", "permission", "colleague", "seat"),
        related=("security", "getting-started"),
    ),
    Article(
        "security",
        "Security & sign-in",
        "Change your password, understand roles and permissions, and keep your account safe.",
        "Account & team", icon="key",
        keywords=("security", "password", "change password", "reset", "2fa", "login",
                  "sign in", "auth", "authentication", "permission", "role"),
        related=("team",),
    ),
    Article(
        "troubleshooting",
        "Troubleshooting & FAQ",
        "Fix the most common problems: email not sending, a domain that won't verify, DNS issues.",
        "Troubleshooting", icon="alert",
        keywords=("error", "fail", "failed", "not working", "bounce", "spam", "verify",
                  "troubleshoot", "faq", "problem", "issue", "stuck", "pending"),
        related=("dns-setup", "domains"),
    ),
]

_BY_SLUG = {a.slug: a for a in ARTICLES}


def get_article(slug: str):
    return _BY_SLUG.get(slug)


def grouped():
    """Return ``[(category, [articles]), ...]`` in display order."""
    out = []
    for cat in CATEGORIES:
        items = [a for a in ARTICLES if a.category == cat]
        if items:
            out.append((cat, items))
    return out


def search(query: str):
    """Return articles matching every whitespace-separated term in ``query``."""
    terms = query.lower().split()
    if not terms:
        return []
    return [a for a in ARTICLES if all(t in a.haystack() for t in terms)]


def related_to(article: "Article"):
    return [_BY_SLUG[s] for s in article.related if s in _BY_SLUG]
