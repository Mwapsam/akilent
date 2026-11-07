"""Landing page Phases 3 and 4 (docs/plans/landing-premium-global.md): pricing for a global buyer,
then the finish (closing call to action, developers, FAQ, footer, share and search).

- Plan cards: price, who it's for, the three limits that differ between plans, the button.
- Monthly / annual works without JavaScript; the annual price is what checkout charges.
- "All plans include", a comparison table that scrolls in its own box, Meta's fees.
- Enterprise lists only what can be set up on request today.
- Developers lead with WhatsApp and show only real endpoints and payloads.
"""

import json
import re
import struct
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.models import User

from apps.billing import api as billing_api
from apps.billing.models import ComingSoonFeature, Plan, PlanFeature, PlanLimit
from apps.billing.pricing import price_for
from apps.core.models import SiteSettings

POSITIONING = "The customer conversation platform for teams that sell through chat"


@pytest.fixture
def plans(db, settings):
    settings.WHATSAPP_ENABLED = True
    settings.EMAIL_ENABLED = True
    rows = [
        ("trial", "Trial", "0", 14, 3, 100),
        ("starter", "Starter", "19", 0, 14, 1000),
        ("professional", "Professional", "49", 0, 30, 5000),
        ("business", "Business", "99", 0, 90, -1),
    ]
    out = []
    for slug, name, price, trial, history, conversations in rows:
        plan = Plan.objects.create(
            slug=slug,
            name=name,
            price_monthly=Decimal(price),
            trial_days=trial,
            log_retention_days=history,
            tagline=f"For {name.lower()} teams.",
        )
        billing_api.set_plan_channels(plan, ["whatsapp", "email_sending"])
        PlanLimit.objects.update_or_create(
            plan=plan, key="conversations_month", defaults={"value": conversations}
        )
        PlanLimit.objects.update_or_create(
            plan=plan, key="whatsapp_numbers", defaults={"value": 1}
        )
        PlanFeature.objects.get_or_create(plan=plan, key="whatsapp_campaigns")
        out.append(plan)
    # Automations only on the paid plans: in the table, not in "All plans include".
    PlanFeature.objects.filter(plan=out[0], key="automations").delete()
    for plan in out[1:]:
        PlanFeature.objects.get_or_create(plan=plan, key="automations")
    return out


def _section(html, sid):
    return html.split(f'<section id="{sid}"', 1)[1].split("</section>", 1)[0]


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _pricing(client):
    return _section(client.get("/").content.decode(), "pricing")


# ---- Phase 3: pricing -------------------------------------------------------------------------


@pytest.mark.django_db
def test_cards_share_one_structure_and_list_what_each_plan_adds(client, plans):
    # Automations from Professional up, so Professional adds them to Starter.
    PlanFeature.objects.filter(plan=plans[1], key="automations").delete()
    pricing = _pricing(client)
    cards = re.split(r'<div class="lp-plan[ "]', pricing)[1:]
    names = [
        re.search(r'<h3 class="lp-h3">([^<]+)</h3>', c).group(1)
        for c in cards
        if "<h3" in c
    ]
    # The free plan is a line above the cards, not a card: three paid plans and Enterprise.
    assert names == ["Starter", "Professional", "Business", "Enterprise"]
    assert "Try it free for 14 days · No card required" in pricing
    assert 'href="/signup/?plan=trial"' in pricing
    starter = next(c for c in cards if ">Starter<" in c)
    pro = next(c for c in cards if ">Professional<" in c)
    assert "For starter teams." in starter
    # The button sits straight under the price, before the list, on every card.
    for card in (starter, pro):
        assert (
            card.index("lp-price") < card.index("lp-btn") < card.index("lp-plan-plus")
        )

    def points(card):
        return re.findall(r"</svg>([^<]+)</li>", card.split('class="lp-points"', 1)[1])

    assert "Includes:" in starter and "1,000 conversations/mo" in points(starter)
    # Each card after the first lists only what it adds.
    assert "Everything in Starter, plus:" in pro
    assert "5,000 conversations/mo" in points(pro) and "Automations" in points(pro)
    assert "1 WhatsApp number" not in points(pro)
    # Message history isn't advertised: nothing enforces it.
    assert "message history" not in pricing
    # Professional is the one amber button.
    assert pricing.count("lp-btn-primary") == pricing.count(
        'data-cta-plan="professional"'
    )


@pytest.mark.django_db
def test_the_annual_price_is_what_checkout_charges(client, plans):
    pricing = _pricing(client)
    starter = next(p for p in plans if p.slug == "starter")
    yearly = price_for(starter, "annually")
    assert yearly == yearly.to_integral_value()  # a whole amount, shown as it's charged
    per_month = (yearly / 12).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    assert f"<b>${per_month}</b>" in pricing
    assert f"Billed ${yearly:,.0f} a year" in pricing
    assert 'href="/signup/?plan=starter&amp;period=annually"' in pricing
    assert 'href="/signup/?plan=starter"' in pricing
    # The switch is two radios before the cards, so it works without JavaScript.
    assert pricing.index('id="bill-monthly"') < pricing.index('class="plans')
    assert (
        'id="bill-monthly" value="monthly" class="sr-only lp-bill-input" checked'
        in pricing
    )
    assert "Save 15%" in pricing


@pytest.mark.django_db
def test_without_an_annual_discount_there_is_no_switch(client, plans, settings):
    settings.BILLING_PERIOD_MULTIPLIERS = {"annually": "1.00"}
    pricing = _pricing(client)
    assert "bill-annual" not in pricing and "lp-when-a" not in pricing
    assert "Annual billing" not in pricing


@pytest.mark.django_db
def test_all_plans_include_only_what_every_plan_has(client, plans):
    pricing = _pricing(client)
    block = pricing.split('class="lp-included', 1)[1].split("</div>", 1)[0]
    grid = re.findall(r"</svg>([^<]+)</li>", block)
    assert "All plans include" in block and "WhatsApp campaigns" in grid
    assert "Automations" not in grid
    # No duplicates, and names keep their capitals ("WhatsApp", "AI").
    assert len(grid) == len(set(grid))
    assert "whatsApp" not in block and "aI " not in block
    assert "Unlimited team members" in block


@pytest.mark.django_db
def test_the_comparison_table_lists_only_what_differs(client, plans):
    trial = plans[0]
    ComingSoonFeature.objects.create(name="Website chat", is_active=True).plans.add(
        trial
    )
    pricing = _pricing(client)
    table = pricing.split('<details class="lp-compare', 1)[1].split("</details>", 1)[0]
    # It scrolls sideways inside its own box, never the page.
    assert 'class="lp-compare-scroll table-scroll" role="region"' in table
    for label in (
        "Customer conversations per month",
        "Unlimited",
        "Automations",
        "Website chat",
        "Coming soon",
    ):
        assert label in table, label
    # The same on every plan: in "All plans include", not a row of identical cells.
    for same in ("WhatsApp campaigns", "WhatsApp numbers", "Message history"):
        assert same not in table, same
    assert "per day per day" not in pricing and "per month per month" not in pricing
    # Collapsed until opened.
    assert '<details class="lp-compare lp-catalog lp-reveal">' in pricing
    for plan in plans:
        assert f'<th scope="col">{plan.name}</th>' in table


@pytest.mark.django_db
def test_enterprise_offers_only_what_exists_today(client, plans):
    pricing = _pricing(client)
    ent = pricing.split('class="lp-plan lp-plan-ent', 1)[1].split("</ul>", 1)[0]
    assert "Custom" in ent
    assert 'href="/demo/?topic=sales"' in ent and "Contact sales" in ent
    for claim in ("custom-limits", "feature-overrides", "annual-billing", "demo"):
        assert f'data-claim="{claim}"' in ent, claim
    text = _text(ent).lower()
    for absent in (
        "single sign-on",
        "sso",
        "sla",
        "purchase order",
        "invoice",
        "multiple whatsapp numbers",
        "audit log export",
        "onboarding",
    ):
        assert absent not in text, absent


@pytest.mark.django_db
def test_meta_fees_are_explained_only_with_whatsapp(client, plans, settings):
    pricing = _pricing(client)
    assert "billed by Meta directly" in pricing and "never adds a markup" in pricing
    assert 'href="https://developers.facebook.com/docs/whatsapp/pricing"' in pricing
    settings.WHATSAPP_ENABLED = False
    assert "billed by Meta" not in _section(client.get("/").content.decode(), "pricing")


@pytest.mark.django_db
def test_contact_sales_opens_the_demo_form_under_its_own_name(client):
    html = client.get("/demo/?topic=sales").content.decode()
    assert '<h1 id="demo-title">Talk to sales</h1>' in html
    assert 'action="/demo/?topic=sales"' in html
    assert "Book a demo</h1>" in client.get("/demo/").content.decode()


@pytest.mark.django_db
def test_an_operator_sets_the_plans_tagline(client, plans):
    operator = User.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(operator)
    plan = plans[2]
    client.post(
        f"/manage/plans/{plan.pk}/edit/",
        {
            "name": plan.name,
            "price_monthly": "49",
            "tagline": "For teams that answer every chat.",
            "is_active": "on",
            "channels": ["whatsapp", "email"],
            "accept_loss": "on",
        },
    )
    plan.refresh_from_db()
    assert plan.tagline == "For teams that answer every chat."


# ---- Phase 4: finish --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_closing_band_repeats_the_positioning_line(client, plans):
    html = client.get("/").content.decode()
    final = html.split('aria-labelledby="final-title"', 1)[1].split("</section>", 1)[0]
    assert f"{POSITIONING}." in final
    assert "14-day free trial · No card required" in final
    assert 'data-cta="start_trial"' in final and 'data-cta="book_demo"' in final


@pytest.mark.django_db
def test_developers_lead_with_whatsapp_and_show_real_calls(client, plans, settings):
    dev = _section(client.get("/").content.decode(), "developers")
    tabs = re.findall(r'aria-controls="(code-[a-z]+)"', dev)
    assert tabs == ["code-whatsapp", "code-event", "code-webhook", "code-email"]
    assert "POST</span> /api/v1/whatsapp/campaigns" in dev
    assert "X-Akilent-Signature:" in dev and "event.received" in dev
    # The API has neither of these today, so neither is shown.
    assert "message.received" not in dev
    assert "/api/v1/whatsapp/messages" not in dev
    # The tabs row is hidden until the script runs: without it every sample shows.
    assert 'class="lp-ctabs" role="tablist" aria-label="Code samples" hidden' in dev
    settings.WHATSAPP_ENABLED = False
    dev = _section(client.get("/").content.decode(), "developers")
    assert "code-whatsapp" not in dev and "code-email" in dev


def test_the_code_samples_name_real_webhook_events():
    """The sample's event names come from the webhook catalog and the events API."""
    from apps.email.models import WebhookEndpoint
    from apps.email.webhooks import EVENT_HEADER, SIGNATURE_HEADER

    assert "event.received" in {c[0] for c in WebhookEndpoint.EVENT_CHOICES}
    assert (SIGNATURE_HEADER, EVENT_HEADER) == (
        "X-Akilent-Signature",
        "X-Akilent-Event",
    )


@pytest.mark.django_db
def test_the_faq_answers_a_global_buyer(client, plans):
    faq = _section(client.get("/").content.decode(), "faq")
    for question in (
        "Which countries can I use it in?",
        "Who pays WhatsApp's message fees?",
        "Where is my data stored?",
        "Can I bring my existing WhatsApp number?",
        "Do you offer annual billing or invoices?",
    ):
        assert question in faq, question
    text = _text(faq)
    assert "save 15%" in text
    assert "Invoices against a purchase order aren't available yet" in text
    for region in ("Ireland", "Frankfurt", "Virginia", "eu-west", "us-east", "GDPR"):
        assert region not in text, region


@pytest.mark.django_db
def test_footer_columns_and_social_links(client, plans):
    footer = client.get("/").content.decode().split('<footer class="lp-footer', 1)[1]
    for label in ("Product", "Company", "Trust"):
        assert f'<nav aria-label="{label}">' in footer, label
    assert 'href="/demo/?topic=sales"' in footer and 'href="/security/"' in footer
    assert "lp-social" not in footer
    # There's no About or Careers page yet, so neither is linked.
    assert "About" not in footer and "Careers" not in footer
    site = SiteSettings.load()
    site.linkedin_url = "https://www.linkedin.com/company/example"
    site.save()
    footer = client.get("/").content.decode().split('<footer class="lp-footer', 1)[1]
    assert (
        'href="https://www.linkedin.com/company/example" rel="noopener me">LinkedIn'
        in footer
    )


@pytest.mark.django_db
def test_console_keeps_only_real_social_links(client):
    operator = User.objects.create_superuser("root", "root@example.com", "pw")
    client.force_login(operator)
    client.post(
        "/manage/settings/",
        {
            "app_name": "Akilent",
            "x_url": "https://x.com/example",
            "instagram_url": "javascript:alert(1)",
        },
    )
    site = SiteSettings.load()
    assert site.x_url == "https://x.com/example"
    assert site.instagram_url == ""


def test_the_share_image_is_1200_by_630():
    png = (
        Path(django_settings.BASE_DIR) / "static" / "img" / "seo" / "og-default.png"
    ).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (1200, 630)


@pytest.mark.django_db
def test_share_and_search_tags(client, plans, settings):
    settings.SEO_ALLOW_INDEXING = True
    settings.SITE_URL = "https://akilent.com"
    html = client.get("/").content.decode()
    assert (
        'og:image" content="https://akilent.com/static/img/seo/og-default.png' in html
    )
    assert '<meta property="og:image:width" content="1200">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert '<link rel="canonical" href="https://akilent.com/">' in html
    data = [
        json.loads(m)
        for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html)
    ]
    kinds = {d["@type"]: d for d in data}
    assert {"WebSite", "Organization", "SoftwareApplication"} <= set(kinds)
    offer = kinds["SoftwareApplication"]["offers"]
    assert (offer["lowPrice"], offer["highPrice"]) == ("19.00", "99.00")
    # The product markup is for the landing page only.
    help_html = client.get("/help/").content.decode()
    assert "SoftwareApplication" not in help_html


@pytest.mark.django_db
def test_the_inbox_tab_has_its_own_picture(client, plans):
    """Platform -> Inbox no longer repeats the journey's Engage chat."""
    html = client.get("/").content.decode()
    assert html.count('data-art="thread"') == 1
    assert 'data-art="assign"' in _section(html, "platform")


@pytest.mark.django_db
def test_the_page_stays_cheap_for_signed_in_visitors(
    client, plans, django_user_model, django_assert_max_num_queries
):
    """Features, limits and coming-soon items load in three queries for every plan, and the
    dashboard's nav and onboarding context processors skip the landing page."""
    from apps.accounts.models import Account, Membership

    user = django_user_model.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Shop")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    client.get("/")
    with django_assert_max_num_queries(20):
        resp = client.get("/")
    assert "onboarding" not in resp.context
    assert "locked_features" not in resp.context
    assert "Open dashboard" in resp.content.decode()
