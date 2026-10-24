import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts import onboarding as ob
from apps.accounts.models import Account, Invitation, Membership
from apps.email.models import EmailApiKey, EmailDomain


def _make_account(services=Account.Services.EMAIL, *, verified_email=True):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    acc = Account.objects.create(
        company_name="Acme",
        selected_services=services,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=verified_email,
    )
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture
def account(db):
    return _make_account()


# --- Checklist state --------------------------------------------------------


def _keys(acc, **kw):
    return [s["key"] for s in ob.get_state(acc, **kw)["steps"]]


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    from apps.core import channels as ch

    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    ch._cache.update(reset)
    yield
    ch._cache.update(reset)


@pytest.mark.django_db
def test_fresh_email_account_steps(account):
    state = ob.get_state(account)
    assert state["complete"] is False
    assert _keys(account, essentials_only=True) == [
        "account",
        "verify_email",
        "domain",
        "verify",
    ]
    assert state["next_step"]["key"] == "domain"  # email already verified in fixture


@pytest.mark.django_db
def test_unverified_email_is_the_first_gap(db):
    acc = _make_account(verified_email=False)
    assert ob.get_state(acc)["next_step"]["key"] == "verify_email"


@pytest.mark.django_db
def test_whatsapp_only_account_skips_email_steps(db, settings):
    settings.WHATSAPP_ENABLED = True
    acc = _make_account(Account.Services.WHATSAPP)
    keys = _keys(acc)
    assert _keys(acc, essentials_only=True) == ["account", "verify_email", "whatsapp"]
    assert not {"domain", "verify", "use", "email_template"} & set(keys)


@pytest.mark.django_db
def test_both_account_has_whatsapp_and_email_steps(db, settings):
    settings.WHATSAPP_ENABLED = True
    acc = _make_account(Account.Services.BOTH)
    assert _keys(acc, essentials_only=True) == [
        "account",
        "verify_email",
        "whatsapp",
        "domain",
        "verify",
    ]


@pytest.mark.django_db
def test_next_step_advances_as_setup_progresses(account):
    EmailDomain.objects.create(account=account, domain="mail.acme.com")
    assert ob.get_state(account)["next_step"]["key"] == "verify"

    EmailDomain.objects.filter(account=account).update(
        status=EmailDomain.Status.VERIFIED
    )
    state = ob.get_state(account)
    assert state["complete"] is True and state["next_step"] is None


@pytest.mark.django_db
def test_recommendations_do_not_block_completion(account):
    EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED
    )
    state = ob.get_state(account)
    assert state["complete"] is True
    assert state["next_step"] is None
    team = next(s for s in state["steps"] if s["key"] == "team")
    assert team["optional"] is True and team["done"] is False
    security = next(s for s in state["steps"] if s["key"] == "security")
    assert security["optional"] is True and security["done"] is False
    assert state["next_recommended"] is not None


@pytest.mark.django_db
def test_team_step_done_with_pending_invite(account):
    Invitation.objects.create(account=account, email="x@example.com", role="member")
    team = next(s for s in ob.get_state(account)["steps"] if s["key"] == "team")
    assert team["done"] is True


@pytest.mark.django_db
def test_steps_are_grouped_into_sections(account):
    state = ob.get_state(account)
    assert state["sections"][0]["key"] == "setup"
    assert sum(s["total"] for s in state["sections"]) == state["total"]
    assert all(not s["optional"] for s in state["sections"][0]["steps"])


# --- Feature steps follow channels and entitlements ------------------------


def _usable(monkeypatch, keys):
    from apps.billing import api as billing_api

    monkeypatch.setattr(
        billing_api,
        "nav_state",
        lambda account: {
            "usable": frozenset(keys),
            "locked": frozenset(),
            "tools_off": frozenset(),
        },
    )


@pytest.mark.django_db
def test_feature_steps_hidden_without_the_feature(account, monkeypatch):
    _usable(monkeypatch, set())
    keys = set(_keys(account))
    hidden = {"orders", "sales", "ai", "knowledge", "build", "profile", "instagram"}
    hidden |= {"email_template", "email_campaign", "use"}
    assert not hidden & keys
    # Core things every business can do are always suggested.
    assert {"hours", "contacts", "insights", "team", "security"} <= keys


@pytest.mark.django_db
def test_feature_steps_shown_when_usable(account, monkeypatch, settings):
    settings.INSTAGRAM_ENABLED = True
    monkeypatch.setattr("apps.ai.providers.is_configured", lambda: True)
    _usable(
        monkeypatch,
        {
            "orders",
            "sales",
            "ai_assistant",
            "automations",
            "instagram",
            "email_templates",
            "email_campaigns",
            "email_sending",
        },
    )
    assert {
        "orders",
        "sales",
        "ai",
        "knowledge",
        "profile",
        "instagram",
        "post",
        "email_template",
        "email_campaign",
        "use",
    } <= set(_keys(account))


@pytest.mark.django_db
def test_ai_steps_hidden_when_ai_is_not_set_up_on_the_site(account, monkeypatch):
    monkeypatch.setattr("apps.ai.providers.is_configured", lambda: False)
    _usable(monkeypatch, {"ai_assistant"})
    assert "ai" not in _keys(account)


@pytest.mark.django_db
def test_instagram_step_hidden_when_channel_off(account, monkeypatch, settings):
    settings.INSTAGRAM_ENABLED = False
    _usable(monkeypatch, {"instagram"})
    assert "instagram" not in _keys(account)


@pytest.mark.django_db
def test_step_locked_until_its_prerequisite_is_done(account, monkeypatch):
    _usable(monkeypatch, {"email_campaigns"})
    EmailDomain.objects.create(account=account, domain="mail.acme.com")

    def campaign():
        return next(
            s for s in ob.get_state(account)["steps"] if s["key"] == "email_campaign"
        )

    assert campaign()["url"] is None and campaign()["locked"] == "Verify your domain"
    EmailDomain.objects.update(status=EmailDomain.Status.VERIFIED)
    assert campaign()["url"] == "/email/campaigns/new/" and not campaign()["locked"]


@pytest.mark.django_db
def test_done_flips_when_the_feature_is_set_up(account):
    from apps.accounts.models import BusinessHours

    def hours():
        return next(s for s in ob.get_state(account)["steps"] if s["key"] == "hours")

    assert hours()["done"] is False
    BusinessHours.objects.create(
        account=account, schedule={"mon": {"open": "09:00", "close": "17:00"}}
    )
    assert hours()["done"] is True


def test_every_step_icon_exists():
    import re
    from pathlib import Path

    from django.conf import settings as dj

    src = (Path(dj.BASE_DIR) / "templates/components/icon.html").read_text(
        encoding="utf-8"
    )
    names = set(re.findall(r"name\s*==\s*['\"]([\w-]+)['\"]", src))
    assert names, "icon names not found"
    for icon in {d.icon for d in ob.STEPS} | {"pause"}:
        assert icon in names, icon


@pytest.mark.django_db
def test_every_step_link_opens_for_a_fully_entitled_business(
    client, settings, monkeypatch
):

    from apps.billing.features import FEATURES
    from apps.billing.models import Plan, PlanFeature, Subscription

    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    monkeypatch.setattr("apps.ai.providers.is_configured", lambda: True)
    acc = _make_account(Account.Services.BOTH)
    plan = Plan.objects.create(slug="all", name="All", service_type=Plan.SERVICE_BOTH)
    PlanFeature.objects.bulk_create(
        [PlanFeature(plan=plan, key=f.key) for f in FEATURES], ignore_conflicts=True
    )
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    client.force_login(acc.owner)

    def check_links():
        steps = [s for s in ob.get_state(acc)["steps"] if s["url"]]
        for step in steps:
            resp = client.get(step["url"].split("#")[0])
            assert resp.status_code == 200, (step["key"], step["url"], resp.status_code)
        return {s["key"] for s in steps}

    first = check_links()
    assert len(first) > 10

    # Unlock the steps that wait for WhatsApp, an approved template and AI.
    from apps.ai.models import AISettings
    from apps.whatsapp.models.templates import MessageTemplate

    n = _wa_number(acc, registration_status="registered")
    n.connection_tests.create(recipient="+260971234567", status="sent")
    MessageTemplate.objects.create(account=acc, name="hi", approval_status="approved")
    AISettings.objects.update_or_create(account=acc, defaults={"enabled": True})
    unlocked = check_links() - first
    assert {"wa_template", "wa_campaign", "build", "knowledge"} <= unlocked | first


# --- Paused channels ---------------------------------------------------------


def _switch_off(*channels):
    """The admin's on/off switch (a pause), as opposed to the server not being set up."""
    from types import SimpleNamespace

    from apps.core import channels as ch

    operator, _ = User.objects.get_or_create(
        username="ops", defaults={"email": "ops@example.com", "is_superuser": True}
    )
    for channel in channels:
        ch.set_channel_enabled(channel, False, request=SimpleNamespace(user=operator))


def _switch_on(channel):
    from types import SimpleNamespace

    from apps.core import channels as ch

    ch.set_channel_enabled(
        channel, True, request=SimpleNamespace(user=User.objects.get(username="ops"))
    )


@pytest.fixture
def all_wired(settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True


@pytest.mark.django_db
def test_email_account_with_email_off_is_paused_not_done(account, all_wired):
    _switch_off("email")
    state = ob.get_state(account)
    keys = [s["key"] for s in state["steps"]]
    assert "email_paused" in keys and "domain" not in keys
    assert state["complete"] is False
    assert state["next_step"]["key"] == "email_paused"
    assert state["next_step"]["url"] is None

    # Nothing is written while paused, so setup resumes when email is back.
    assert ob.advance_onboarding(account) == ""
    account.refresh_from_db()
    assert account.onboarding_state == Account.Onboarding.ACCOUNT_CREATED

    _switch_on("email")
    assert ob.advance_onboarding(account) == "/email/domains/"
    account.refresh_from_db()
    assert account.onboarding_state == Account.Onboarding.DOMAIN_SETUP


@pytest.mark.django_db
def test_channel_the_server_is_not_set_up_for_is_dropped_not_paused(db, settings):
    """An env flag that's off isn't coming back, so it must not hold setup open."""
    settings.WHATSAPP_ENABLED = True
    settings.EMAIL_ENABLED = False
    acc = _make_account(Account.Services.BOTH)
    assert _keys(acc, essentials_only=True) == ["account", "verify_email", "whatsapp"]
    assert ob.get_state(acc)["paused"] == []


@pytest.mark.django_db
def test_whatsapp_account_with_whatsapp_off_is_paused(db, all_wired):
    _switch_off("whatsapp")
    acc = _make_account(Account.Services.WHATSAPP)
    keys = _keys(acc)
    assert "whatsapp_paused" in keys
    # It is not pushed into email setup it never chose.
    assert not {"domain", "verify"} & set(keys)
    assert ob.get_state(acc)["complete"] is False


@pytest.mark.django_db
def test_both_with_one_channel_off(db, all_wired):
    _switch_off("email")
    acc = _make_account(Account.Services.BOTH)
    state = ob.get_state(acc, essentials_only=True)
    # What can be done today comes first, and is what "next up" points at.
    assert [s["key"] for s in state["steps"]] == [
        "account",
        "verify_email",
        "whatsapp",
        "email_paused",
    ]
    assert state["next_step"]["key"] == "whatsapp"

    # The live channel still moves the stored state along...
    assert ob.advance_onboarding(acc) == "/whatsapp/numbers/"
    acc.refresh_from_db()
    assert acc.onboarding_state == Account.Onboarding.WHATSAPP_SETUP

    # ...but once it's done, COMPLETED waits for the paused one.
    n = _wa_number(acc, registration_status="registered")
    n.connection_tests.create(recipient="+260971234567", status="sent")
    assert ob.advance_onboarding(acc) == ""
    acc.refresh_from_db()
    assert acc.onboarding_state == Account.Onboarding.WHATSAPP_SETUP
    assert ob.get_state(acc)["next_step"]["key"] == "email_paused"


@pytest.mark.django_db
def test_both_channels_off(db, all_wired):
    _switch_off("email", "whatsapp")
    acc = _make_account(Account.Services.BOTH)
    assert _keys(acc, essentials_only=True) == [
        "account",
        "verify_email",
        "whatsapp_paused",
        "email_paused",
    ]
    assert ob.advance_onboarding(acc) == ""
    acc.refresh_from_db()
    assert acc.onboarding_state == Account.Onboarding.ACCOUNT_CREATED


@pytest.mark.django_db
def test_unknown_channel_state_never_completes_onboarding(account, monkeypatch):
    from apps.core import channels as ch

    monkeypatch.setattr(ch, "disabled_channels", lambda: None)
    assert "email_paused" in _keys(account)
    assert ob.advance_onboarding(account) == ""
    account.refresh_from_db()
    assert account.onboarding_state == Account.Onboarding.ACCOUNT_CREATED


@pytest.mark.django_db
def test_onboarding_page_with_email_off(client, account, all_wired):
    _switch_off("email")
    client.force_login(account.owner)
    resp = client.get("/onboarding/")
    assert resp.status_code == 200
    assert b"Email setup is paused" in resp.content
    assert b"getting-started" not in resp.content
    assert b"all set up" not in resp.content


@pytest.mark.django_db
def test_state_reports_which_channels_are_being_set_up(db, all_wired):
    _switch_off("email")
    acc = _make_account(Account.Services.BOTH)
    state = ob.get_state(acc, essentials_only=True)
    assert state["wants_whatsapp"] is True and state["wants_email"] is False


# --- Prerequisites that aren't on the checklist -----------------------------


@pytest.mark.django_db
def test_automation_step_needs_whatsapp(account, monkeypatch):
    """The starters run on WhatsApp, so an email-only business isn't offered them."""
    _usable(monkeypatch, {"automations"})
    keys = _keys(account)
    assert "build" not in keys
    assert "profile" in keys


@pytest.mark.django_db
def test_paused_prerequisite_drops_the_dependent_steps(db, all_wired, monkeypatch):
    _switch_off("whatsapp")
    _usable(monkeypatch, {"automations", "whatsapp_campaigns"})
    acc = _make_account(Account.Services.WHATSAPP)
    keys = set(_keys(acc))
    assert not {"build", "wa_template", "wa_campaign"} & keys


@pytest.mark.django_db
def test_post_step_needs_somewhere_to_publish(account, monkeypatch, settings):
    settings.INSTAGRAM_ENABLED = True
    settings.WHATSAPP_APP_ID = ""
    _usable(monkeypatch, set())  # Instagram not in the plan, no Facebook app
    assert "post" not in _keys(account)

    _usable(monkeypatch, {"instagram"})
    post = next(s for s in ob.get_state(account)["steps"] if s["key"] == "post")
    assert post["url"] is None and post["locked"] == "Connect Instagram"


# --- Email template step ------------------------------------------------------


@pytest.mark.django_db
def test_only_a_template_the_business_created_counts(account, monkeypatch):
    from apps.email.models import EmailTemplate
    from apps.email.starter_templates import STARTER_TEMPLATES

    _usable(monkeypatch, {"email_templates"})

    def done():
        return next(
            s for s in ob.get_state(account)["steps"] if s["key"] == "email_template"
        )["done"]

    assert EmailTemplate.objects.filter(account=account).exists()  # seeded on create
    assert done() is False

    # A starter whose content differs from today's copy (edited, or seeded from an
    # older version) proves nothing either way.
    EmailTemplate.objects.filter(
        account=account, slug=STARTER_TEMPLATES[0]["slug"]
    ).update(subject="Different")
    assert done() is False

    EmailTemplate.objects.create(account=account, name="Ours", slug="ours")
    assert done() is True


@pytest.mark.django_db
def test_one_request_computes_the_checklist_once(rf, account, monkeypatch):
    calls = []
    real = ob._whatsapp_step

    def spy(acc):
        calls.append(acc.pk)
        return real(acc)

    monkeypatch.setattr(ob, "_whatsapp_step", spy)
    account.selected_services = Account.Services.BOTH
    account.save()
    request = rf.get("/dashboard/")
    first = ob.get_state(account, essentials_only=True, request=request)
    assert ob.get_state(account, essentials_only=True, request=request) is first
    ob.get_state(account, request=request)  # the full list reuses the essentials' work
    assert len(calls) <= 1


@pytest.mark.django_db
def test_background_htmx_request_skips_the_checklist(rf, account):
    from apps.accounts.context_processors import onboarding_status

    request = rf.get("/dashboard/work-queue/", HTTP_HX_REQUEST="true")
    request.user = account.owner
    assert onboarding_status(request) == {}


# --- Context processor + widget rendering --------------------------------


@pytest.mark.django_db
def test_widget_and_tour_render_when_incomplete(client, account):
    client.force_login(account.owner)
    resp = client.get("/dashboard/")
    assert resp.status_code == 200
    assert b"Finish setup" in resp.content
    assert b"open-welcome-tour" in resp.content


@pytest.mark.django_db
def test_widget_hidden_once_setup_complete(client, account):
    EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED
    )
    EmailApiKey.objects.create(account=account)
    client.force_login(account.owner)
    resp = client.get("/dashboard/")
    assert b"Finish setup" not in resp.content
    assert b"open-welcome-tour" not in resp.content


@pytest.mark.django_db
def test_widget_not_shown_on_onboarding_page(client, account):
    client.force_login(account.owner)
    resp = client.get("/onboarding/")
    assert resp.status_code == 200
    assert b"Finish setup" not in resp.content


# --- WhatsApp step follows the setup console -----------------------------


def _wa_account(settings, services=Account.Services.WHATSAPP):
    settings.WHATSAPP_ENABLED = True
    return _make_account(services)


def _wa_step(acc):
    return next(s for s in ob.get_state(acc)["steps"] if s["key"] == "whatsapp")


def _wa_number(acc, **kw):
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    kw.setdefault("phone_number_id", "PNID")
    kw.setdefault("access_token", "tok")
    kw.setdefault("waba_id", "W")
    return WhatsAppBusinessNumber.objects.create(account=acc, **kw)


@pytest.mark.django_db
def test_whatsapp_step_not_done_for_bare_number(settings):
    acc = _wa_account(settings)
    _wa_number(acc)  # PENDING registration
    step = _wa_step(acc)
    assert not step["done"]
    assert step["cta"] == "Register number"


@pytest.mark.django_db
def test_whatsapp_step_failed_registration_offers_retry(settings):
    acc = _wa_account(settings)
    _wa_number(acc, registration_status="failed")
    assert _wa_step(acc)["cta"] == "Retry registration"


@pytest.mark.django_db
def test_whatsapp_step_registered_but_untested_asks_for_test(settings):
    acc = _wa_account(settings)
    _wa_number(acc, registration_status="registered")
    step = _wa_step(acc)
    assert not step["done"]
    assert step["cta"] == "Open WhatsApp"  # message-first guide precedes the test


@pytest.mark.django_db
def test_whatsapp_step_done_after_successful_test_without_reply(settings):
    acc = _wa_account(settings)
    n = _wa_number(acc, registration_status="registered")
    n.connection_tests.create(recipient="+260971234567", status="sent")
    step = _wa_step(acc)
    assert step["done"]  # a reply is recommended, never blocking
    assert step["cta"] is None


@pytest.mark.django_db
def test_failed_test_does_not_complete_step(settings):
    acc = _wa_account(settings)
    n = _wa_number(acc, registration_status="registered")
    n.connection_tests.create(recipient="+260971234567", status="failed")
    assert not _wa_step(acc)["done"]


@pytest.mark.django_db
def test_advance_onboarding_waits_for_registered_number(settings):
    acc = _wa_account(settings, Account.Services.BOTH)
    n = _wa_number(acc)  # exists, but not registered
    assert ob.advance_onboarding(acc) == "/whatsapp/numbers/"
    n.registration_status = "registered"
    n.save()
    # Registered is enough to move on to domain setup; a test is checklist-only.
    assert ob.advance_onboarding(acc) == "/email/domains/"


@pytest.mark.django_db
def test_dashboard_panels_skip_the_full_checklist_until_setup_is_done(
    client, account, monkeypatch
):
    calls = []
    real = ob.get_state

    def spy(acc, **kw):
        calls.append(kw.get("essentials_only", False))
        return real(acc, **kw)

    monkeypatch.setattr(ob, "get_state", spy)
    client.force_login(account.owner)
    resp = client.get("/dashboard/panels/", HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    assert False not in calls  # only the cheap essentials check ran
    assert b'data-testid="next-recommended"' not in resp.content

    EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED
    )
    resp = client.get("/dashboard/panels/", HTTP_HX_REQUEST="true")
    assert b'data-testid="next-recommended"' in resp.content
