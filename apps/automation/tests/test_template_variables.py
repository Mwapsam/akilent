"""Template blanks: each customer gets their own value, never another customer's."""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation import variables as v
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import _resolve_variable_mapping, validate_definition
from apps.contacts.models import Contact
from apps.whatsapp.models import MessageTemplate

INSTALL_URL = "/automations/starters/install/"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def person(account, first, phone, **attrs):
    return Contact.objects.create(account=account, first_name=first, last_name="X", phone=phone, attributes=attrs)


def run_for(account, contact):
    wf = Workflow.objects.get_or_create(account=account, slug="w", defaults={"name": "w"})[0]
    return WorkflowRun(workflow=wf, contact=contact, context={})


def test_each_customer_gets_their_own_name(account):
    ada, bo = person(account, "Ada", "+260971000001"), person(account, "Bo", "+260971000002")
    mapping = {"name": "contact.first_name", "company": "account.company_name"}
    a = _resolve_variable_mapping(["name", "company"], mapping, run_for(account, ada), ada)
    b = _resolve_variable_mapping(["name", "company"], mapping, run_for(account, bo), bo)
    assert (a["name"], b["name"]) == ("Ada", "Bo")
    assert a["company"] == b["company"] == "Mwamba Kitchen"


def test_a_customer_without_a_name_gets_the_fallback(account):
    nameless = Contact.objects.create(account=account, phone="+260971000003")
    params = _resolve_variable_mapping(
        ["name"], {"name": "contact.first_name"}, run_for(account, nameless), nameless,
        fallbacks={"name": "there"})
    assert params == {"name": "there"}


def test_no_value_and_no_fallback_stops_the_send_with_the_blank_named(account):
    nameless = Contact.objects.create(account=account, phone="+260971000004")
    with pytest.raises(ValueError, match="'name'"):
        _resolve_variable_mapping(["name"], {"name": "contact.first_name"}, run_for(account, nameless), nameless)


def test_custom_fields_and_the_old_mapping_forms_still_work(account):
    c = person(account, "Ada", "+260971000005", plan="Gold")
    params = _resolve_variable_mapping(
        ["a", "b"], {"a": "contact.plan", "b": "Same for all"}, run_for(account, c), c)
    assert params == {"a": "Gold", "b": "Same for all"}


def test_smart_defaults_never_pick_fixed_text_for_names():
    assert v.default_choice("name") == "first_name"
    assert v.default_choice("first_name") == "first_name"
    assert v.default_choice("customer_name") == "first_name"
    assert v.default_choice("company") == "business"
    assert v.default_choice("full_name") == "full_name"
    assert v.default_choice("1") == "literal"


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    template = MessageTemplate.objects.create(
        account=account, name="Checking in", whatsapp_template_name="checking_in",
        content="Hi {{name}} from {{company}}", variables=["name", "company"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED)
    return client, template


@pytest.mark.django_db
def test_the_setup_form_stores_per_customer_sources_and_fallbacks(account, owner):
    client, t = owner
    client.post(INSTALL_URL, {
        "starter": "quiet-customer-check-in", "template_id": t.pk,
        f"var__{t.pk}__name": "first_name", f"fb__{t.pk}__name": "friend",
        f"var__{t.pk}__company": "business",
    })
    wf = Workflow.objects.get(account=account, slug="quiet-customer-check-in")
    send = next(s for s in wf.definition["steps"] if s["type"] == "send_whatsapp")
    assert send["variable_mapping"] == {"name": "contact.first_name", "company": "account.company_name"}
    assert send["variable_fallbacks"] == {"name": "friend"}


@pytest.mark.django_db
def test_a_typed_name_is_stored_as_fixed_text_but_flagged(account, owner):
    client, t = owner
    client.post(INSTALL_URL, {
        "starter": "quiet-customer-check-in", "template_id": t.pk,
        f"var__{t.pk}__name": "literal", f"lit__{t.pk}__name": "Ada",
        f"var__{t.pk}__company": "business",
    })
    wf = Workflow.objects.get(account=account, slug="quiet-customer-check-in")
    warnings = [e for e in validate_definition(wf.definition, account=account) if e.get("severity") == "warning"
                or getattr(e, "severity", "") == "warning"]
    assert any("same name" in str(w) for w in warnings)


@pytest.mark.django_db
def test_the_form_offers_the_choices_with_the_safe_default(account, owner):
    client, _ = owner
    html = client.get("/automations/").content.decode()
    assert "Customer&#x27;s first name" in html or "Customer's first name" in html
    assert "Every customer will get exactly this text" in html
    assert "pick: 'first_name'" in html and "pick: 'business'" in html
