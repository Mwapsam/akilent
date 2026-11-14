"""In-app knowledge base registry.

Articles are curated, server-rendered guides (bodies live in
``templates/help/articles/<slug>.html``). This module holds their metadata,
category ordering, and a tiny keyword search — no database, no admin churn.
"""

from dataclasses import dataclass


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
    # Channel key this article belongs to, or None for channel-agnostic articles.
    channel: str | None = None
    # Release key (apps.core.releases) the article waits for, or None.
    release: str | None = None

    @property
    def template(self) -> str:
        return f"help/articles/{self.slug}.html"

    def haystack(self) -> str:
        return " ".join(
            (self.title, self.summary, self.category, *self.keywords)
        ).lower()


# Display order for the index; a category with no visible articles is skipped.
CATEGORIES = [
    "Getting started",
    "Email setup",
    "WhatsApp",
    "Instagram",
    "Facebook",
    "Website chat",
    "Content Studio",
    "Automate",
    "Account & team",
    "Troubleshooting",
]

ARTICLES = [
    Article(
        "getting-started",
        "Getting started",
        "Set up your workspace: connect your channels, then follow one customer from a post to a paid order.",
        "Getting started",
        icon="sparkles",
        keywords=("intro", "setup", "quickstart", "begin", "first", "overview"),
        related=("whatsapp-setup", "content-studio", "domains"),
    ),
    Article(
        "domains",
        "Add & configure a sending domain",
        "Provision a domain, understand each status, and enable or disable sending.",
        "Email setup",
        icon="globe",
        keywords=(
            "domain",
            "sending",
            "provision",
            "verify",
            "verified",
            "pending",
            "disable",
        ),
        related=("dns-setup", "troubleshooting"),
        channel="email",
    ),
    Article(
        "dns-setup",
        "Set up your DNS records",
        "Add the DKIM, SPF, DMARC and verification records at your DNS provider, step by step.",
        "Email setup",
        icon="globe",
        keywords=(
            "dns",
            "txt",
            "dkim",
            "spf",
            "dmarc",
            "cloudflare",
            "godaddy",
            "namecheap",
            "google domains",
            "records",
            "nameserver",
            "cname",
            "host",
        ),
        related=("domains", "troubleshooting"),
        channel="email",
    ),
    Article(
        "whatsapp-setup",
        "Connect & use WhatsApp",
        "Connect a number through Meta, then send templates, campaigns and one-time codes, and fix common errors.",
        "WhatsApp",
        icon="chat",
        keywords=(
            "whatsapp",
            "meta",
            "waba",
            "cloud api",
            "register",
            "registration",
            "133010",
            "token",
            "webhook",
            "test message",
            "business verification",
            "phone number",
            "template",
            "campaign",
            "verification code",
            "otp",
            "inbox",
            "ai",
            "auto reply",
            "automatic reply",
            "knowledge base",
            "faq",
        ),
        related=("instagram-setup", "troubleshooting"),
        channel="whatsapp",
    ),
    Article(
        "whatsapp-campaign-lists",
        "Build a list and send a WhatsApp campaign",
        "Add customers to a list four ways, understand who can get offers, and see why someone didn't get a message.",
        "WhatsApp",
        icon="send",
        keywords=(
            "campaign",
            "broadcast",
            "bulk",
            "list",
            "customers",
            "import",
            "csv",
            "paste",
            "opt in",
            "opt-in",
            "consent",
            "permission",
            "stop",
            "promotion",
            "marketing",
            "not sent",
            "skipped",
        ),
        related=("whatsapp-setup", "troubleshooting"),
        channel="whatsapp",
    ),
    Article(
        "post-results",
        "Track which posts bring WhatsApp chats",
        "Add a chat link to a Facebook or Instagram post, and see the chats, leads and orders it brings in.",
        "Content Studio",
        icon="chart",
        keywords=(
            "post",
            "content studio",
            "link",
            "chat link",
            "wa.me",
            "tracking",
            "attribution",
            "results",
            "leads",
            "orders",
            "facebook",
            "instagram",
            "reach",
            "performance",
        ),
        related=("content-studio", "whatsapp-setup"),
        channel="whatsapp",
    ),
    Article(
        "content-studio",
        "Plan and publish posts in Content Studio",
        "Write a post once for Instagram and your Facebook Page, schedule each channel, and review before it goes out.",
        "Content Studio",
        icon="image",
        keywords=(
            "content studio",
            "publishing",
            "post",
            "schedule",
            "calendar",
            "planning",
            "pillar",
            "campaign",
            "caption",
            "hashtags",
            "approval",
            "review",
            "instagram",
            "facebook",
        ),
        related=("facebook-page-setup", "instagram-setup", "post-results"),
    ),
    Article(
        "facebook-page-setup",
        "Connect your Facebook Page",
        "Connect your Page to publish posts, and answer its messages and comments in Akilent.",
        "Facebook",
        icon="image",
        keywords=(
            "facebook",
            "page",
            "connect",
            "publish",
            "reconnect",
            "expired",
            "meta",
            "messenger",
            "messages",
            "comments",
            "inbox",
        ),
        related=("content-studio", "post-results"),
        channel="facebook",
    ),
    Article(
        "instagram-setup",
        "Connect & use Instagram",
        "Connect your Instagram account, answer DMs in the inbox or with AI, and set up comment rules.",
        "Instagram",
        icon="instagram",
        keywords=(
            "instagram",
            "ig",
            "dm",
            "direct message",
            "comment",
            "private reply",
            "moderation",
            "spam",
            "business login",
            "facebook page",
            "meta",
            "inbox",
            "ai",
            "auto reply",
            "automatic reply",
            "knowledge base",
            "faq",
        ),
        related=("whatsapp-setup",),
        channel="instagram",
    ),
    Article(
        "website-chat",
        "Website chatbot: set up, install and hand over",
        "Put a chatbot on your website that answers from your Knowledge, captures leads and hands "
        "chats to your team in the inbox.",
        "Website chat",
        icon="message-circle",
        keywords=(
            "chatbot",
            "website",
            "widget",
            "live chat",
            "chat",
            "install",
            "embed",
            "snippet",
            "script",
            "domain",
            "handoff",
            "hand over",
            "talk to a person",
            "lead",
            "ai",
        ),
        related=("automations", "getting-started"),
        release="chatbot",
    ),
    Article(
        "team",
        "Invite & manage your team",
        "Invite teammates by email, assign roles, and remove members.",
        "Account & team",
        icon="user",
        keywords=(
            "team",
            "user",
            "invite",
            "invitation",
            "role",
            "member",
            "admin",
            "owner",
            "permission",
            "colleague",
            "seat",
        ),
        related=("security", "getting-started"),
    ),
    Article(
        "security",
        "Security & sign-in",
        "Change your password, understand roles and permissions, and keep your account safe.",
        "Account & team",
        icon="key",
        keywords=(
            "security",
            "password",
            "change password",
            "reset",
            "2fa",
            "login",
            "sign in",
            "auth",
            "authentication",
            "permission",
            "role",
        ),
        related=("team",),
    ),
    Article(
        "automations",
        "Automations: build, test and fix",
        "Answer common questions, ask with buttons, send forms and hand customers to your team, without writing code.",
        "Automate",
        icon="workflow",
        keywords=(
            "automation",
            "workflow",
            "auto reply",
            "keyword",
            "starter",
            "test",
            "simulator",
            "run history",
            "retry",
            "pause",
            "publish",
        ),
        related=("follow-ups", "forms", "troubleshooting"),
    ),
    Article(
        "follow-ups",
        "Follow-ups for your team",
        "Set a follow-up from a chat, a customer or a lead, see it when it's due, move it to later "
        "or give it to a teammate. Only your team sees it.",
        "Automate",
        icon="bell",
        keywords=(
            "follow up",
            "follow-up",
            "reminder",
            "snooze",
            "due",
            "missed",
            "remind me",
        ),
        related=("automations", "getting-started"),
    ),
    Article(
        "forms",
        "Forms & WhatsApp Flows",
        "Collect details in a conversation: a native form on WhatsApp, or questions one at a time elsewhere.",
        "Automate",
        icon="list",
        keywords=(
            "form",
            "flow",
            "whatsapp flow",
            "questions",
            "survey",
            "intake",
            "answers",
            "csv",
        ),
        related=("automations", "whatsapp-setup"),
    ),
    Article(
        "troubleshooting",
        "Troubleshooting & FAQ",
        "Fix the most common problems: email not sending, a domain that won't verify, DNS issues.",
        "Troubleshooting",
        icon="alert",
        keywords=(
            "error",
            "fail",
            "failed",
            "not working",
            "bounce",
            "spam",
            "verify",
            "troubleshoot",
            "faq",
            "problem",
            "issue",
            "stuck",
            "pending",
        ),
        related=("dns-setup", "domains"),
        channel="email",
    ),
]

_BY_SLUG = {a.slug: a for a in ARTICLES}


def _is_visible(article: "Article") -> bool:
    from apps.core.channels import is_enabled
    from apps.core.releases import is_released

    if article.release is not None and not is_released(article.release):
        return False
    return article.channel is None or is_enabled(article.channel)


def visible_articles() -> list:
    return [a for a in ARTICLES if _is_visible(a)]


def get_article(slug: str):
    article = _BY_SLUG.get(slug)
    return article if article is not None and _is_visible(article) else None


def grouped():
    """Return ``[(category, [articles]), ...]`` in display order."""
    visible = visible_articles()
    out = []
    for cat in CATEGORIES:
        items = [a for a in visible if a.category == cat]
        if items:
            out.append((cat, items))
    return out


def search(query: str):
    """Return articles matching every whitespace-separated term in ``query``."""
    terms = query.lower().split()
    if not terms:
        return []
    return [a for a in visible_articles() if all(t in a.haystack() for t in terms)]


def related_to(article: "Article"):
    return [_BY_SLUG[s] for s in article.related if get_article(s) is not None]
