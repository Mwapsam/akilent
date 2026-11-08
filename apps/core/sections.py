"""The app's sections and their tabs: one source for the sidebar, the tab bar and Ctrl+K.

The sidebar shows six main items (Home, Inbox, Customers, Campaigns, Content Studio,
Automations), then Connections and Settings. Every page keeps its URL; it is shown as a
tab of the section it belongs to. So old links, bookmarks and the App Review instructions
keep working, and nothing redirects.

* A tab is **hidden** when it doesn't apply (a channel that isn't on, an optional tool the
  owner switched off, a release flag) and **locked** when the plan doesn't include it (it
  opens the locked page). A main item whose tabs are all hidden or locked is hidden.
* A main item is active on any of its tabs' pages, and on ``also`` pages (Inbox settings)
  that belong to the section without being a tab.
* Matching is the sidebar's prefix rule (``templatetags.nav._matches``), first section wins,
  with ``exclude`` carving out sub-paths another tab owns.

Connections stays its own sidebar item while Meta App Review is open: the reviewer
instructions say Settings → Channels (``docs/ops/instagram-app-review.md``).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    href: str
    match: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    feature: str = ""  # catalog key: locked when not in the plan
    tool: str = ""  # optional tool: hidden when switched off in Settings
    when: str = ""  # a gate in _GATES: hidden when false
    exact: bool = False
    skeleton: str = "table"  # data-skeleton while a slow page loads
    aliases: tuple[str, ...] = ()  # old names, for Ctrl+K


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    icon: str
    tabs: tuple[Tab, ...]
    when: str = ""
    badge_url: str = ""
    # Pages of the section that aren't tabs (they have their own sub-navigation).
    also: tuple[str, ...] = ()
    # False: the section's pages bring their own tabs (Content Studio).
    tab_bar: bool = True
    aliases: tuple[str, ...] = field(default=())


INBOX_SETTINGS = (
    "inbox/saved-replies",
    "inbox/teams",
    "inbox/routing-rules",
    "inbox/settings",
)

SECTIONS: tuple[Section, ...] = (
    Section(
        "home",
        "Home",
        "home",
        (
            Tab(
                "overview",
                "Overview",
                "/dashboard/",
                ("dashboard",),
                aliases=("Dashboard",),
            ),
            Tab(
                "insights",
                "Insights",
                "/insights/",
                ("insights", "email/insights"),
                skeleton="grid",
            ),
        ),
    ),
    Section(
        "inbox",
        "Inbox",
        "message-circle",
        (
            Tab(
                "conversations",
                "Conversations",
                "/inbox/",
                ("inbox",),
                exclude=(
                    "inbox/followups",
                    "inbox/forms",
                    "inbox/comments",
                    *INBOX_SETTINGS,
                ),
            ),
            Tab(
                "comments",
                "Comments",
                "/inbox/comments/",
                ("inbox/comments", "instagram/rules"),
                when="comments",
                aliases=("Comment rules",),
            ),
            Tab("followups", "Follow-ups", "/inbox/followups/", ("inbox/followups",)),
        ),
        badge_url="/inbox/badge/",
        also=INBOX_SETTINGS,
        aliases=("Saved replies", "Teams", "Routing rules", "Inbox settings"),
    ),
    Section(
        "customers",
        "Customers",
        "users",
        (
            Tab(
                "all",
                "All customers",
                "/contacts/",
                ("contacts",),
                aliases=("Contacts",),
            ),
            Tab(
                "interested",
                "Interested",
                "/sales/",
                ("sales",),
                feature="sales",
                tool="sales",
                aliases=("Sales", "Leads"),
            ),
            Tab(
                "orders",
                "Orders",
                "/orders/",
                ("orders",),
                feature="orders",
                tool="orders",
            ),
        ),
    ),
    Section(
        "campaigns",
        "Campaigns",
        "send",
        (
            Tab(
                "campaigns",
                "Campaigns",
                "/campaigns/",
                ("campaigns", "email/campaigns", "scheduled"),
            ),
            Tab(
                "templates",
                "Message templates",
                "/email/templates/",
                ("email/templates", "whatsapp/templates"),
                aliases=("Templates", "WhatsApp templates", "One-time codes"),
            ),
        ),
        when="campaigns",
    ),
    Section(
        "studio",
        "Content Studio",
        "image",
        (
            Tab(
                "studio",
                "Content Studio",
                "/publishing/",
                ("publishing",),
                feature="content_studio",
            ),
        ),
        tab_bar=False,
    ),
    Section(
        "automations",
        "Automations",
        "workflow",
        (
            Tab(
                "automations",
                "Automations",
                "/automations/",
                ("automations",),
                exclude=("automations/why-not",),
                feature="automations",
            ),
            Tab(
                "forms",
                "Forms",
                "/inbox/forms/",
                ("inbox/forms",),
                feature="automations",
            ),
            Tab(
                "build",
                "Build with AI",
                "/build/",
                ("build",),
                feature="automations",
                aliases=("Build",),
            ),
            Tab(
                "why_not",
                "Why didn't it reply?",
                "/automations/why-not/",
                ("automations/why-not",),
                feature="automations",
                skeleton="form",
            ),
        ),
    ),
)

# When the sidebar became six sections: people who joined before see the one-time
# "Navigation got simpler" notice; people who joined after never knew the old one.
NAV_CHANGED_AT = datetime.datetime(2026, 11, 8, tzinfo=datetime.UTC)

# Pages that left the sidebar, with where they live now: Ctrl+K finds them by their old
# name, and the one-time "Navigation got simpler" notice lists them.
MOVED = (
    ("Dashboard", "Home"),
    ("Interested", "Customers \u203a Interested"),
    ("Orders", "Customers \u203a Orders"),
    ("Comments", "Inbox \u203a Comments"),
    ("Follow-ups", "Inbox \u203a Follow-ups"),
    ("Templates", "Campaigns \u203a Message templates"),
    ("Forms", "Automations \u203a Forms"),
    ("Build", "Automations \u203a Build with AI"),
    ("Insights", "Home \u203a Insights"),
    ("Message history", "Settings \u203a Activity log"),
    ("Billing", "your account menu"),
)


def _gates(ctx) -> dict[str, bool]:
    channels = ctx.get("enabled_channels") or set()
    released = ctx.get("released") or {}
    return {
        "comments": "instagram" in channels or "facebook" in channels,
        "campaigns": bool(ctx.get("CAMPAIGNS_ENABLED")),
        "support": bool(
            released.get("support")
            if hasattr(released, "get")
            else getattr(released, "support", False)
        ),
    }


def _matches(
    path: str, targets: tuple[str, ...], exclude: tuple[str, ...] = (), exact=False
) -> bool:
    from apps.core.templatetags.nav import _matches as match
    from apps.core.templatetags.nav import _resolve

    if any(match(path, _resolve(t), exact=False) for t in exclude):
        return False
    return any(match(path, _resolve(t), exact=exact) for t in targets)


def section_paths(section: Section) -> list[str]:
    """Every path the main item counts as its own (for data-nav-match in the browser)."""
    paths = [t for tab in section.tabs for t in tab.match] + list(section.also)
    return list(dict.fromkeys(paths))


def section_excludes(section: Section) -> list[str]:
    """Other sections' pages under this section's prefixes (Automations' /inbox/forms/
    under Inbox's /inbox/), so the browser's prefix rule marks only one main item."""
    own = section_paths(section)
    others = [
        p for other in SECTIONS if other is not section for p in section_paths(other)
    ]
    return [p for p in others if any(p != o and p.startswith(o + "/") for o in own)]


def build(ctx, path: str) -> dict:
    """The visible sections for this request, with tab states and the current section."""
    from django.urls import reverse

    gates = _gates(ctx)
    locked_features = ctx.get("locked_features") or set()
    tools_off = ctx.get("tools_off") or set()

    sections, current = [], None
    for section in SECTIONS:
        if section.when and not gates.get(section.when, False):
            continue
        tabs = []
        for tab in section.tabs:
            if (tab.when and not gates.get(tab.when, False)) or (
                tab.tool and tab.tool in tools_off
            ):
                continue
            locked = bool(tab.feature and tab.feature in locked_features)
            tabs.append(
                {
                    "key": tab.key,
                    "label": tab.label,
                    "href": reverse("billing:locked", args=[tab.feature])
                    if locked
                    else tab.href,
                    "locked": locked,
                    "active": _matches(path, tab.match, tab.exclude, tab.exact),
                    "skeleton": tab.skeleton,
                    "aliases": tab.aliases,
                }
            )
        usable = [t for t in tabs if not t["locked"]]
        if not usable:
            continue  # every tab hidden or locked: no main item at all
        in_also = _matches(path, section.also)
        active = any(t["active"] for t in tabs) or in_also
        entry = {
            "key": section.key,
            "label": section.label,
            "icon": section.icon,
            "href": usable[0]["href"],
            "tabs": tabs,
            "active": active and current is None,
            "badge_url": section.badge_url,
            "match": " ".join(section_paths(section)),
            "exclude": " ".join(section_excludes(section)),
            "show_tab_bar": section.tab_bar and len(tabs) > 1 and not in_also,
            "aliases": section.aliases,
        }
        if entry["active"]:
            current = entry
        sections.append(entry)
    return {"sections": sections, "current": current}


def palette_items(ctx, nav: dict) -> list[dict]:
    """Ctrl+K's "Go to" list: every page in the sidebar and its tabs, the account pages,
    and the old names of pages that moved (searchable as "Interested", "Templates"...)."""
    from django.urls import reverse

    items: list[dict] = []

    def add(label: str, href: str, hint: str = "") -> None:
        items.append({"group": "Go to", "label": label, "href": href, "hint": hint})

    for section in nav["sections"]:
        tabs = [t for t in section["tabs"] if not t["locked"]]
        for tab in tabs:
            label = (
                section["label"]
                if len(section["tabs"]) == 1
                else f"{section['label']} \u203a {tab['label']}"
            )
            add(label, tab["href"])
            for alias in tab["aliases"]:
                add(alias, tab["href"], f"now {label}")
        if section["key"] == "inbox":
            add("Inbox \u203a Saved replies", reverse("conversations:saved_replies"))
            add(
                "Inbox \u203a Teams & routing",
                reverse("conversations:inbox_settings_teams"),
            )
            for alias in section["aliases"]:
                add(
                    alias, reverse("conversations:inbox_settings"), "now Inbox settings"
                )

    channels = ctx.get("enabled_channels") or set()
    if channels:
        add("Connections", "/channels/", "")
        add("Channels", "/channels/", "now Connections")
    if "email" in channels:
        add("Email domains", "/email/domains/")
    if ctx.get("WHATSAPP_ENABLED"):
        add("WhatsApp numbers", "/whatsapp/numbers/")
    add("Settings", reverse("settings-profile"))
    add("Settings \u203a Activity log", reverse("logs:messages"))
    add("Message history", reverse("logs:messages"), "now Settings \u203a Activity log")
    add("Billing & plans", "/billing/plans/", "in your account menu")
    add("Help & guides", "/help/")
    add("API docs", "/docs/")
    if _gates(ctx)["support"]:
        add("Contact support", "/support/")
    return items
