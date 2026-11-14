"""The public product and integration pages: one registry for the views, the sitemap, the
navigation and the related-page links.

A page is open only while what it sells is switched on (``gate`` reads ``marketing_channels()``),
the same rule the landing page and the use-case pages follow: a page for a channel that is off
404s and drops out of the sitemap. Copy lives in ``templates/public/<kind>/<slug>.html`` and
follows docs/marketing/messaging.md (every product statement carries a data-claim id).

``updated`` is the sitemap's lastmod: change it when the page's copy changes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

PRODUCT = "product"
INTEGRATION = "integration"

# URL prefix per kind. /channels/ is the in-app Channels page, hence /integrations/.
PREFIX = {PRODUCT: "/product/", INTEGRATION: "/integrations/"}
HUB = {
    PRODUCT: {
        "title": "Product",
        "h1": "Everything that turns a chat into a sale",
        "description": "Akilent's shared inbox, automations, AI answers, Content Studio, post results and sales tools: what each one does and how they work together.",
    },
    INTEGRATION: {
        "title": "Integrations",
        "h1": "Connect the channels your customers use",
        "description": "Connect WhatsApp, Instagram and your Facebook Page to Akilent, and answer every customer from one shared inbox.",
    },
}


def _chat(ch: dict) -> bool:
    return any(ch.get(c) == "live" for c in ("whatsapp", "instagram", "facebook"))


def _live(channel: str) -> Callable[[dict], bool]:
    return lambda ch: ch.get(channel) == "live"


@dataclass(frozen=True)
class MarketingPage:
    kind: str
    slug: str
    name: str  # the short label in navigation and cards
    title: str  # <title>, before the site suffix
    description: str  # meta description, at most ~160 characters
    blurb: str  # one line on hub and related cards
    claims: str  # the blurb's data-claim ids (docs/marketing/messaging.md §10)
    gate: Callable[[dict], bool]
    updated: date
    related: tuple[str, ...] = field(default=())  # "product:slug" / "integration:slug"

    @property
    def url(self) -> str:
        return f"{PREFIX[self.kind]}{self.slug}/"

    @property
    def template(self) -> str:
        return f"public/{self.kind}/{self.slug}.html"

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.slug}"


_UPDATED = date(2026, 11, 13)

PAGES: tuple[MarketingPage, ...] = (
    MarketingPage(
        PRODUCT,
        "shared-inbox",
        "Shared inbox",
        "Shared inbox for WhatsApp, Instagram and Facebook",
        "One shared inbox for your team's WhatsApp, Instagram and Facebook messages. Assign each chat, see the customer's history and never answer twice.",
        "Every chat in one inbox the whole team can see, each one assigned.",
        "inbox assign",
        _chat,
        _UPDATED,
        ("product:automations", "product:ai-answers", "product:sales"),
    ),
    MarketingPage(
        PRODUCT,
        "automations",
        "Automations",
        "Auto-replies, follow-ups and workflows",
        "Greet customers, reply when you're closed, answer by keyword and remind your team to follow up. Test every automation before customers see it.",
        "Auto-replies, follow-up reminders and workflows you can test first.",
        "auto-replies follow-ups workflows workflow-testing",
        _chat,
        _UPDATED,
        ("product:shared-inbox", "product:ai-answers", "product:sales"),
    ),
    MarketingPage(
        PRODUCT,
        "ai-answers",
        "AI answers",
        "AI replies from your own business information",
        "Teach AI your answers, import them or read them from your website. It suggests replies for your team, and answers on its own only if you allow it.",
        "AI suggests replies from your own answers, off until you switch it on.",
        "ai ai-knowledge",
        _chat,
        _UPDATED,
        ("product:shared-inbox", "product:automations"),
    ),
    MarketingPage(
        PRODUCT,
        "content-studio",
        "Content Studio",
        "Plan and schedule Instagram and Facebook posts",
        "Write a post once, with its own version for Instagram and your Facebook Page. Plan on a calendar, draft captions with AI and schedule each channel.",
        "Plan, write and schedule posts for Instagram and Facebook.",
        "content-planning post-scheduling",
        lambda ch: bool(ch.get("content")),
        _UPDATED,
        ("product:post-results", "integration:instagram", "integration:facebook"),
    ),
    MarketingPage(
        PRODUCT,
        "post-results",
        "Post results",
        "See which social media posts bring customers",
        "Put a WhatsApp chat link on each post and see the chats, leads, orders and paid revenue it brought, counted from real evidence only.",
        "The chats, leads and paid orders each post brought in.",
        "post-results",
        lambda ch: bool(ch.get("results")),
        _UPDATED,
        ("product:content-studio", "product:sales", "integration:whatsapp"),
    ),
    MarketingPage(
        PRODUCT,
        "sales",
        "Sales and orders",
        "Track leads and orders from chat",
        "Mark interested customers, record each order and mark it paid when the customer pays you. Send campaigns and see what your conversations turned into.",
        "Interested customers, orders and campaigns, from the same chats.",
        "interested orders campaigns",
        _chat,
        _UPDATED,
        ("product:shared-inbox", "product:automations"),
    ),
    MarketingPage(
        INTEGRATION,
        "whatsapp",
        "WhatsApp",
        "WhatsApp Business inbox for teams",
        "Connect your own WhatsApp business number through Meta's guided setup. Share it with your team, automate replies and send campaigns from approved templates.",
        "Your own business number, shared by the team.",
        "own-number inbox assign",
        _live("whatsapp"),
        _UPDATED,
        ("product:shared-inbox", "product:automations", "product:sales"),
    ),
    MarketingPage(
        INTEGRATION,
        "instagram",
        "Instagram",
        "Instagram DM inbox and comment automation",
        "Answer Instagram DMs from your team's shared inbox, reply to comments by keyword, privately or publicly, and publish posts to Instagram.",
        "DMs in the shared inbox, comments answered by rule.",
        "instagram-dms instagram-comments",
        _live("instagram"),
        _UPDATED,
        ("product:shared-inbox", "product:content-studio"),
    ),
    MarketingPage(
        INTEGRATION,
        "facebook",
        "Facebook",
        "Facebook Page messages and comments inbox",
        "Messages to your Facebook Page arrive in your team's shared inbox. Answer, hide or reply privately to comments, and publish posts to your Page.",
        "Page messages and comments, answered from one place.",
        "facebook-inbox facebook-comments",
        _live("facebook"),
        _UPDATED,
        ("product:shared-inbox", "product:content-studio"),
    ),
    MarketingPage(
        INTEGRATION,
        "website-chat",
        "Website chat",
        "Website chatbot that hands chats to your team",
        "Put a chatbot on your website: it answers from your own information, captures leads and hands chats to your team's shared inbox when a person is needed.",
        "A chatbot on your site that answers, captures leads and hands over.",
        "website-chat website-chat-handoff",
        _live("website_chat"),
        date(2026, 11, 14),
        ("product:shared-inbox", "product:automations", "product:ai-answers"),
    ),
)

_BY_KEY = {p.key: p for p in PAGES}


_CHAT_NAMES = {"whatsapp": "WhatsApp", "instagram": "Instagram", "facebook": "Facebook"}
_INBOX_CLAIM = {
    "whatsapp": "inbox",
    "instagram": "instagram-dms",
    "facebook": "facebook-inbox",
}


def chat_context(ch: dict) -> dict:
    """The chat channels that are on, for copy: "WhatsApp, Instagram and Facebook" and the
    claim ids that say their messages reach the shared inbox."""
    live = [c for c in ("whatsapp", "instagram", "facebook") if ch.get(c) == "live"]
    names = [_CHAT_NAMES[c] for c in live]
    if len(names) > 1:
        label = ", ".join(names[:-1]) + " and " + names[-1]
    else:
        label = "".join(names)
    return {
        "chat_names": label,
        "inbox_claims": " ".join(_INBOX_CLAIM[c] for c in live),
    }


def visible(kind: str | None = None, ch: dict | None = None) -> list[MarketingPage]:
    """The pages open right now, in registry order."""
    if ch is None:
        from apps.accounts.views import marketing_channels

        ch = marketing_channels()
    return [p for p in PAGES if (kind is None or p.kind == kind) and p.gate(ch)]


def get(kind: str, slug: str, ch: dict) -> MarketingPage | None:
    page = _BY_KEY.get(f"{kind}:{slug}")
    return page if page is not None and page.gate(ch) else None


def related(page: MarketingPage, ch: dict) -> list[MarketingPage]:
    out = []
    for key in page.related:
        other = _BY_KEY.get(key)
        if other is not None and other.gate(ch):
            out.append(other)
    return out
