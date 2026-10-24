"""Onboarding checklist state.

Single source of truth for "where is this workspace in its setup?", shared by
the onboarding page, the dashboard, and the always-available floating widget
(via the context processor).

The checklist is a registry of steps (``STEPS``). Each step says which section
it belongs to, when it applies to an account (its channel is on, the plan
includes it, the owner hasn't switched it off) and the cheap query that proves
it's done. Only the "Get set up" steps are required; everything else is a
recommendation that shows the business what else Akilent does for them.

When a channel the business chose is switched off by the admin (or its state
can't be read yet), its setup is shown as *paused* rather than dropped: the
checklist never claims "all set" and the stored state never moves on until the
channel is back (see ``_channel_plan``).
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

from django.conf import settings
from django.urls import reverse

from apps.core.channels import CHANNEL_REGISTRY, channel_state, enabled_channels

WHATSAPP_SETUP_URL = "/whatsapp/numbers/"
DOMAIN_SETUP_URL = "/email/domains/"

SECTIONS: tuple[tuple[str, str], ...] = (
    ("setup", "Get set up"),
    ("reach", "Reach customers"),
    ("know", "Know your customers"),
    ("automate", "Automate & AI"),
    ("sell", "Sell & measure"),
    ("team", "Your team"),
)


def _channel_plan(account) -> tuple[frozenset, frozenset, tuple[str, ...]]:
    """The one place that decides what happens to each channel the business chose.

    Returns ``(enabled channels, chosen channels to set up, chosen channels paused)``.
    A chosen channel that is on is set up. One the admin switched off, or whose state
    can't be read yet, is *paused*: it's coming back, so its setup waits rather than
    being skipped. One this server isn't set up for at all (its env flag is off) is
    dropped, since waiting on it would hold onboarding open forever.
    """
    from apps.accounts.forms import SERVICE_CHANNELS
    from apps.core.channels import available_channels

    enabled = enabled_channels()
    available = available_channels()
    chosen = SERVICE_CHANNELS.get(account.selected_services, set())
    # WhatsApp first: for "Email & WhatsApp" Meta onboarding comes before domain setup.
    ordered = [ch for ch in ("whatsapp", "email") if ch in chosen and ch in available]
    setup = frozenset(ch for ch in ordered if ch in enabled)
    paused = tuple(ch for ch in ordered if channel_state(ch) != "enabled")
    return enabled, setup, paused


def _has_ready_whatsapp_number(account) -> bool:
    """A number registered on the Cloud API with working credentials.

    Merely having a number row doesn't mean WhatsApp works, so this gates the
    resumable onboarding state (``WhatsAppBusinessNumber.is_ready``).
    """
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    return any(
        n.is_ready for n in WhatsAppBusinessNumber.objects.filter(account=account)
    )


def _has_sending_domain(account) -> bool:
    from apps.email.models import EmailDomain

    return EmailDomain.objects.filter(account=account).exists()


def advance_onboarding(account) -> str:
    """Recompute the resumable onboarding state from live data, persist it, and
    return the URL of the next service-specific step ('' once complete).

    The pre-account steps (service selection → plan → account information) live
    entirely in the signup wizard; the first persisted state is ACCOUNT_CREATED.
    From there the sequence is, in order, whichever of these still apply:

        WhatsApp connected (Meta Embedded Signup)  →  a sending domain added

    For "Email & WhatsApp" that means Meta onboarding first, then domain setup,
    matching the documented flow. DNS verification stays a (still-required)
    checklist item in ``get_state`` rather than blocking the state machine, so
    slow DNS never strands a user mid-onboarding.

    While a chosen channel is paused nothing is written: moving on (and above
    all to COMPLETED, which is never recomputed) would skip that channel's setup
    for good once it comes back.
    """
    from apps.accounts.models import Account

    if account.onboarding_state == Account.Onboarding.COMPLETED:
        return ""
    _, setup, paused = _channel_plan(account)
    if paused:
        return ""

    if "whatsapp" in setup and not _has_ready_whatsapp_number(account):
        target, url = Account.Onboarding.WHATSAPP_SETUP, WHATSAPP_SETUP_URL
    elif "email" in setup and not _has_sending_domain(account):
        target, url = Account.Onboarding.DOMAIN_SETUP, DOMAIN_SETUP_URL
    else:
        target, url = Account.Onboarding.COMPLETED, ""

    if account.onboarding_state != target:
        account.onboarding_state = target
        account.save(update_fields=["onboarding_state"])
    return url


def first_setup_url(account) -> str:
    """Where to send a freshly created account for its first setup step."""
    return advance_onboarding(account) or "/onboarding/"


# --- What each step needs to know ------------------------------------------


class _Ctx:
    """Everything the steps read, computed once per ``get_state`` call."""

    def __init__(self, account):
        self.account = account
        self.channels, setup, self.paused = _channel_plan(account)
        self.wants_whatsapp = "whatsapp" in setup
        self.wants_email = "email" in setup

    @cached_property
    def usable(self) -> frozenset:
        """Billing feature keys this account can use (plan + owner switches), one read."""
        from apps.billing import api as billing_api

        return billing_api.nav_state(self.account)["usable"]

    @cached_property
    def whatsapp(self) -> dict:
        return _whatsapp_step(self.account)

    @cached_property
    def domains(self) -> tuple[bool, bool, int | None]:
        """(has a domain, has a verified domain, pk of the first unverified one)."""
        from apps.email.models import EmailDomain

        rows = list(
            EmailDomain.objects.filter(account=self.account)
            .order_by("pk")
            .values_list("pk", "status")
        )
        verified = EmailDomain.Status.VERIFIED
        pending = next((pk for pk, status in rows if status != verified), None)
        return bool(rows), any(s == verified for _, s in rows), pending


def _exists(model_path: str, **filters) -> Callable:
    """A ``done`` check: a row of ``app_label.Model`` for this account exists."""

    def check(ctx) -> bool:
        from django.apps import apps

        model = apps.get_model(model_path)
        return model.objects.filter(account=ctx.account, **filters).exists()

    return check


def _always(ctx) -> bool:
    return True


def _usable(key: str, *, also: Callable = _always) -> Callable:
    return lambda ctx: key in ctx.usable and also(ctx)


@dataclass(frozen=True)
class StepDef:
    key: str
    section: str
    title: str
    desc: str
    icon: str
    cta: str
    url: str | Callable  # a URL name, or fn(ctx) -> path
    done: Callable  # fn(ctx) -> bool
    available: Callable = _always  # fn(ctx) -> bool
    required: bool = False
    needs: str = ""  # key of a step that must be done first
    blocked: Callable | None = None  # fn(ctx) -> what to do first, "" when nothing
    once: bool = False  # nothing to manage afterwards, so no link once done


# --- Done checks that need more than one row -------------------------------


def _done_whatsapp(ctx) -> bool:
    return ctx.whatsapp["done"]


def _done_domain(ctx) -> bool:
    return ctx.domains[0]


def _done_verified(ctx) -> bool:
    return ctx.domains[1]


def _verify_url(ctx) -> str:
    # Deep-link straight to the unverified domain's DNS-check panel instead of
    # just the bare list, so the "verify" step doesn't dump the user back at
    # square one.
    pending = ctx.domains[2]
    return f"/email/domains/#domain-card-{pending}" if pending else DOMAIN_SETUP_URL


def _done_instagram(ctx) -> bool:
    from apps.instagram.models.account import InstagramBusinessAccount

    return any(
        a.is_ready
        for a in InstagramBusinessAccount.objects.filter(
            account=ctx.account, is_active=True
        )
    )


def _instagram_usable(ctx) -> bool:
    return "instagram" in ctx.channels and "instagram" in ctx.usable


def _can_publish(ctx) -> bool:
    """Content Studio needs somewhere this business may publish: Instagram (channel on
    and in the plan), or a Facebook Page (which connects through the Meta app)."""
    return _instagram_usable(ctx) or bool(settings.WHATSAPP_APP_ID)


def _publish_blocked(ctx) -> str:
    """Until a page or account is connected there is nothing to publish to."""
    from apps.publishing.models import FacebookPageConnection

    if _instagram_usable(ctx) and _done_instagram(ctx):
        return ""
    if FacebookPageConnection.objects.filter(
        account=ctx.account, status=FacebookPageConnection.Status.ACTIVE
    ).exists():
        return ""
    return "Connect Instagram" if _instagram_usable(ctx) else "Connect a Facebook Page"


def _done_profile(ctx) -> bool:
    from apps.accounts import profile

    return bool(getattr(profile.get_profile(ctx.account), "completed_at", None))


def _done_hours(ctx) -> bool:
    from apps.accounts.models import BusinessHours

    return (
        BusinessHours.objects.filter(account=ctx.account).exclude(schedule={}).exists()
    )


def _done_contacts(ctx) -> bool:
    from apps.contacts.models import Contact, ContactImport

    return (
        Contact.objects.filter(account=ctx.account).exists()
        or ContactImport.objects.filter(account=ctx.account).exists()
    )


def _done_email_template(ctx) -> bool:
    """A template of their own, or a starter they edited. Every new account is given the
    starters (apps.email.signals), so their mere presence proves nothing."""
    from apps.email.models import EmailTemplate
    from apps.email.starter_templates import STARTER_TEMPLATES

    fields = ("subject", "text_body", "html_body")
    starters = {t["slug"]: t for t in STARTER_TEMPLATES}
    templates = EmailTemplate.objects.filter(account=ctx.account)
    if templates.exclude(slug__in=starters).exists():
        return True
    # Edited = its content no longer matches the starter it was seeded from. Compared by
    # content, not timestamps, so refreshing the starters themselves never counts.
    for row in templates.filter(slug__in=starters).values("slug", *fields):
        starter = starters[row["slug"]]
        if any(row[f] != starter.get(f, row[f]) for f in fields):
            return True
    return False


def _done_automation(ctx) -> bool:
    from apps.automation import api as automation_api

    return bool(automation_api.published_slugs(ctx.account))


def _ai_configured(ctx) -> bool:
    from apps.ai.providers import is_configured

    return is_configured()


def _done_team(ctx) -> bool:
    from apps.accounts.models import Invitation, Membership

    return (
        Membership.objects.filter(account=ctx.account).count() > 1
        or Invitation.objects.filter(
            account=ctx.account, accepted_at__isnull=True
        ).exists()
    )


def _wants_whatsapp_ctx(ctx) -> bool:
    return ctx.wants_whatsapp


def _wants_email_ctx(ctx) -> bool:
    return ctx.wants_email


def _email_feature(key: str) -> Callable:
    return lambda ctx: ctx.wants_email and key in ctx.usable


# Order within a section is the order shown. Required steps come first.
STEPS: tuple[StepDef, ...] = (
    # Get set up ------------------------------------------------------------
    StepDef(
        key="account",
        section="setup",
        title="Create your account",
        desc="Your workspace is ready to go.",
        icon="check-circle",
        cta="",
        url="",
        done=_always,
        required=True,
    ),
    StepDef(
        key="verify_email",
        section="setup",
        title="Verify your email address",
        desc="Confirm your email so we can send you delivery reports and alerts.",
        icon="mail",
        cta="Resend link",
        url="resend-verification",
        done=lambda ctx: ctx.account.email_verified,
        required=True,
        once=True,
    ),
    StepDef(
        key="whatsapp",
        section="setup",
        title="Set up WhatsApp",
        desc="",  # follows the setup console, see _whatsapp_step
        icon="chat",
        cta="",
        url=lambda ctx: WHATSAPP_SETUP_URL,
        done=_done_whatsapp,
        available=_wants_whatsapp_ctx,
        required=True,
    ),
    StepDef(
        key="domain",
        section="setup",
        title="Add a sending domain",
        desc="Add the domain you'll send email from, e.g. mail.yourcompany.com.",
        icon="globe",
        cta="Add domain",
        url="email-domains",
        done=_done_domain,
        available=_wants_email_ctx,
        required=True,
    ),
    StepDef(
        key="verify",
        section="setup",
        title="Verify your domain",
        desc="Add the DNS records and run the DNS check to switch sending on.",
        icon="check-circle",
        cta="Verify DNS",
        url=_verify_url,
        done=_done_verified,
        available=_wants_email_ctx,
        required=True,
    ),
    # Reach customers -------------------------------------------------------
    StepDef(
        key="instagram",
        section="reach",
        title="Connect Instagram",
        desc="Connect your Instagram business account to Akilent.",
        icon="instagram",
        cta="Connect",
        url="instagram-accounts",
        done=_done_instagram,
        available=_instagram_usable,
    ),
    StepDef(
        key="post",
        section="reach",
        title="Publish your first post",
        desc="Write a post once in Content Studio and publish it to your connected pages.",
        icon="image",
        cta="Create a post",
        url="publishing:post-new",
        done=_exists("publishing.ContentPost", status="published"),
        available=_can_publish,
        blocked=_publish_blocked,
    ),
    StepDef(
        key="wa_template",
        section="reach",
        title="Get a WhatsApp template approved",
        desc="Meta must approve a template before you can message customers who haven't written to you in 24 hours.",
        icon="layout-template",
        cta="Create template",
        url="whatsapp-template-create",
        done=_exists("whatsapp.MessageTemplate", approval_status="approved"),
        available=_wants_whatsapp_ctx,
        needs="whatsapp",
    ),
    StepDef(
        key="wa_campaign",
        section="reach",
        title="Send your first WhatsApp campaign",
        desc="Send an approved template to a list or segment of your customers.",
        icon="send",
        cta="New campaign",
        url="whatsapp-campaign-new",
        done=_exists("whatsapp.WhatsAppCampaign"),
        available=lambda ctx: ctx.wants_whatsapp and "whatsapp_campaigns" in ctx.usable,
        needs="wa_template",
    ),
    StepDef(
        key="email_template",
        section="reach",
        title="Make an email template your own",
        desc="Edit one of the ready-made templates, or design your own, for campaigns and API sends.",
        icon="layout-template",
        cta="Open templates",
        url="email-templates",
        done=_done_email_template,
        available=_email_feature("email_templates"),
    ),
    StepDef(
        key="email_campaign",
        section="reach",
        title="Send your first email campaign",
        desc="Email a list or segment of your customers from your own domain.",
        icon="send",
        cta="New campaign",
        url="email-campaign-compose",
        done=_exists("email_service.BulkEmailCampaign"),
        available=_email_feature("email_campaigns"),
        needs="verify",
    ),
    StepDef(
        key="use",
        section="reach",
        title="Create an API key",
        desc="Generate an API key to send email from your own app or website.",
        icon="code-bracket",
        cta="Get API key",
        url="settings-api-keys",
        done=_exists("email_service.EmailApiKey", is_active=True),
        available=_email_feature("email_sending"),
        needs="verify",
    ),
    # Know your customers ---------------------------------------------------
    StepDef(
        key="profile",
        section="know",
        title="Tell Akilent about your business",
        desc="Answer a few questions and see three things worth automating.",
        icon="building",
        cta="Start",
        # The questionnaire is step 1 of the Build area (build:profile only takes the POST).
        url=lambda ctx: reverse("build:home") + "?step=1",
        done=_done_profile,
        available=_usable("automations"),
    ),
    StepDef(
        key="hours",
        section="know",
        title="Set your business hours",
        desc="Akilent uses them for out-of-hours replies and reminders.",
        icon="clock",
        cta="Set hours",
        url="settings-hours",
        done=_done_hours,
    ),
    StepDef(
        key="contacts",
        section="know",
        title="Import your contacts",
        desc="Bring in your customer list from a spreadsheet so their history lives in one place.",
        icon="upload",
        cta="Import",
        # Opens "Add customers" on the CSV tab (contacts:import only takes the upload).
        url=lambda ctx: reverse("contacts:list") + "?add=csv",
        done=_done_contacts,
    ),
    # Automate & AI ---------------------------------------------------------
    StepDef(
        key="build",
        section="automate",
        title="Turn on your first automation",
        desc="Pick a ready-made automation, like a greeting or a closed-hours reply, and switch it on.",
        icon="workflow",
        cta="Choose one",
        url="build:home",
        done=_done_automation,
        # The starter automations run on WhatsApp conversations.
        available=lambda ctx: ctx.wants_whatsapp and "automations" in ctx.usable,
        needs="whatsapp",
    ),
    StepDef(
        key="ai",
        section="automate",
        title="Turn on AI reply suggestions",
        desc="AI suggests replies for you to check and send. It's off until you turn it on.",
        icon="sparkles",
        cta="Turn on",
        url="settings-ai",
        done=_exists("ai.AISettings", enabled=True),
        available=_usable("ai_assistant", also=_ai_configured),
    ),
    StepDef(
        key="knowledge",
        section="automate",
        title="Teach AI about your business",
        desc="Add answers about your products, prices and policies so suggestions are accurate.",
        icon="book-open",
        cta="Add knowledge",
        url="settings-ai-knowledge",
        done=_exists("ai.KnowledgeBaseEntry", is_active=True),
        available=_usable("ai_assistant", also=_ai_configured),
        needs="ai",
    ),
    # Sell & measure --------------------------------------------------------
    StepDef(
        key="orders",
        section="sell",
        title="Create your first order",
        desc="Record an order and send the customer a payment link.",
        icon="credit-card",
        cta="New order",
        url="commerce:orders",
        done=_exists("commerce.Order"),
        available=_usable("orders"),
    ),
    StepDef(
        key="sales",
        section="sell",
        title="Track interested customers",
        desc="Mark customers who are interested so none of them is forgotten.",
        icon="trending-up",
        cta="Open",
        url="crm:sales",
        done=_exists("crm.Lead"),
        available=_usable("sales"),
    ),
    StepDef(
        key="insights",
        section="sell",
        title="Set a monthly goal",
        desc="Pick a target on the Insights page and see each week whether you're on track.",
        icon="bar-chart",
        cta="Set a goal",
        url="insights",
        done=_exists("conversations.InsightGoal"),
    ),
    # Your team -------------------------------------------------------------
    StepDef(
        key="team",
        section="team",
        title="Invite your team",
        desc="Bring colleagues into your workspace so they can help with customers and setup.",
        icon="user-plus",
        cta="Invite teammate",
        url="settings-team",
        done=_done_team,
    ),
    StepDef(
        key="security",
        section="team",
        title="Review your security settings",
        desc="Check your password and active sessions, and confirm your account details are correct.",
        icon="key",
        cta="Review",
        url="settings-security",
        done=lambda ctx: bool(ctx.account.security_reviewed_at),
    ),
)


def _whatsapp_step(account) -> dict:
    """Desc, CTA and done for the WhatsApp step, following the setup console.

    Done once a number is registered *and* a test message was accepted by Meta
    (outbound proven). A reply/webhook is recommended but never blocks this.
    """
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
    from apps.whatsapp.setup import build_setup_console

    numbers = list(
        WhatsAppBusinessNumber.objects.filter(account=account).order_by(
            "phone_number_id"
        )
    )
    console = build_setup_console(
        numbers,
        embedded_enabled=bool(settings.WHATSAPP_APP_ID and settings.WHATSAPP_CONFIG_ID),
        inbound_seen=False,
    )
    current = console.current
    if not numbers:
        cta = "Connect WhatsApp"
        # The console's own hint points at a button on the numbers page ("below").
        hint = "Connect your WhatsApp Business number through Meta's guided setup, or add one manually."
    elif current:
        cta = (current.action or {}).get("label") or "Continue setup"
    else:
        cta = None
    return {
        "done": bool(numbers) and console.required_complete,
        "cta": cta,
        "desc": (
            hint
            if not numbers
            else current.hint
            if current
            else "Your number is registered and sending. Reply to the test message to confirm inbound."
        ),
    }


def _paused_step(channel: str) -> dict:
    label = CHANNEL_REGISTRY[channel].label
    return {
        "key": f"{channel}_paused",
        "section": "setup",
        "title": f"{label} setup is paused",
        "desc": f"{label} is temporarily unavailable on Akilent. Your setup will continue here as soon as it's back.",
        "done": False,
        "url": None,
        "cta": None,
        "icon": "pause",
        "optional": False,
        "locked": "",
    }


def _resolve_url(defn: StepDef, ctx) -> str | None:
    if callable(defn.url):
        return defn.url(ctx)
    return reverse(defn.url) if defn.url else None


def _render_step(defn: StepDef, ctx) -> dict:
    step = {
        "key": defn.key,
        "section": defn.section,
        "title": defn.title,
        "desc": defn.desc,
        "done": bool(defn.done(ctx)),
        "url": _resolve_url(defn, ctx),
        "cta": defn.cta or None,
        "icon": defn.icon,
        "optional": not defn.required,
        "locked": "",
    }
    if defn.key == "whatsapp":
        step.update(desc=ctx.whatsapp["desc"], cta=ctx.whatsapp["cta"])
    if defn.once and step["done"]:
        step.update(url=None, cta=None)
    return step


def get_state(account, *, essentials_only: bool = False) -> dict:
    """The checklist for ``account``.

    ``essentials_only`` computes just the required "Get set up" steps — what the
    widget and the dashboard card need on every page — and skips the dozen or so
    queries behind the recommendations.
    """
    ctx = _Ctx(account)
    steps: list[dict] = []
    for defn in STEPS:
        if essentials_only and not defn.required:
            continue
        if not defn.available(ctx):
            continue
        steps.append(_render_step(defn, ctx))
        if defn.key == "verify_email":
            # Paused channels sit where their own setup would have been.
            steps += [_paused_step(ch) for ch in ctx.paused]

    # A step whose prerequisite isn't done yet can't be started; one whose prerequisite
    # isn't on the checklist at all (another channel, or paused) doesn't apply.
    by_key = {s["key"]: s for s in steps}
    titles = {d.key: d.title for d in STEPS}
    for defn in STEPS:
        step = by_key.get(defn.key)
        if step is None or step["done"]:
            continue
        if defn.needs:
            needed = by_key.get(defn.needs)
            if needed is None:
                steps.remove(step)
                del by_key[defn.key]  # so steps that need this one go too
                continue
            if not needed["done"]:
                step.update(url=None, cta=None, locked=titles[defn.needs])
                continue
        reason = defn.blocked(ctx) if defn.blocked else ""
        if reason:
            step.update(url=None, cta=None, locked=reason)

    required = [s for s in steps if not s["optional"]]
    recommended = [s for s in steps if s["optional"]]
    required_done = sum(1 for s in required if s["done"])
    # Only essentials drive "next up", so finishing them surfaces the
    # completion state even if a recommendation remains.
    next_step = next((s for s in required if not s["done"]), None)
    next_recommended = next(
        (s for s in recommended if not s["done"] and not s["locked"]), None
    )
    # One word for the templates: done, current, paused, locked or todo.
    for s in steps:
        if s["done"]:
            s["state"] = "done"
        elif s["key"].endswith("_paused"):
            s["state"] = "paused"
        elif s["locked"]:
            s["state"] = "locked"
        elif s is next_step:
            s["state"] = "current"
        else:
            s["state"] = "todo"

    sections = []
    for key, label in SECTIONS:
        items = [s for s in steps if s["section"] == key]
        if items:
            sections.append(
                {
                    "key": key,
                    "label": label,
                    "steps": items,
                    "done": sum(1 for s in items if s["done"]),
                    "total": len(items),
                }
            )

    return {
        "steps": steps,
        "sections": sections,
        "complete": required_done == len(required),
        "required_done": required_done,
        "required_total": len(required),
        "recommended_done": sum(1 for s in recommended if s["done"]),
        "recommended_total": len(recommended),
        "done_count": sum(1 for s in steps if s["done"]),
        "total": len(steps),
        "next_step": next_step,
        "next_recommended": next_recommended,
        "paused": list(ctx.paused),
        "wants_whatsapp": ctx.wants_whatsapp,
        "wants_email": ctx.wants_email,
        "pct": round(required_done / len(required) * 100) if required else 100,
    }
