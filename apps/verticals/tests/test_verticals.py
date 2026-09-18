import pytest

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.billing.models import ModuleSubscription
from apps.contacts.models import Contact
from apps.crm.models import Lead
from apps.crm.services import create_lead
from apps.verticals.models import VerticalActivation
from apps.verticals.registry import list_verticals
from apps.verticals.services import (
    VerticalNotFound,
    activate_vertical,
    activated_vertical_keys,
)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.mark.django_db
def test_registry_lists_both_starter_verticals():
    keys = {v["key"] for v in list_verticals()}
    assert keys == {"restaurant", "real_estate"}


@pytest.mark.django_db
def test_activate_vertical_enables_modules_and_publishes_workflows(account):
    activate_vertical(account, "restaurant")

    assert ModuleSubscription.objects.filter(account=account, module="commerce", enabled=True).exists()
    workflows = Workflow.objects.filter(account=account, slug__startswith="restaurant-")
    assert workflows.count() == 2
    assert all(wf.status == Workflow.Status.PUBLISHED for wf in workflows)


@pytest.mark.django_db
def test_activate_vertical_is_idempotent(account):
    activate_vertical(account, "restaurant")
    activate_vertical(account, "restaurant")

    assert Workflow.objects.filter(account=account, slug__startswith="restaurant-").count() == 2
    assert VerticalActivation.objects.filter(account=account, key="restaurant").count() == 1


@pytest.mark.django_db
def test_activate_unknown_vertical_raises(account):
    with pytest.raises(VerticalNotFound):
        activate_vertical(account, "not_a_real_vertical")


@pytest.mark.django_db
def test_activated_vertical_keys(account):
    assert activated_vertical_keys(account) == set()
    activate_vertical(account, "real_estate")
    assert activated_vertical_keys(account) == {"real_estate"}


@pytest.mark.django_db
def test_real_estate_capture_enquiry_workflow_creates_lead_on_message(account):
    activate_vertical(account, "real_estate")
    contact = Contact.objects.create(account=account, phone="+260971111111")

    from apps.automation.workflow_engine import enroll_for_trigger
    enroll_for_trigger(account.id, "conversation.message_received", contact)

    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.source == "whatsapp_enquiry"


@pytest.mark.django_db
def test_real_estate_capture_enquiry_does_not_duplicate_lead_on_repeat_message(account):
    """Regression guard: a returning customer's second message must not spawn
    a second Lead — this is the dedup fix in apps.crm.services.create_lead."""
    activate_vertical(account, "real_estate")
    contact = Contact.objects.create(account=account, phone="+260971111111")

    from apps.automation.workflow_engine import enroll_for_trigger
    enroll_for_trigger(account.id, "conversation.message_received", contact)
    enroll_for_trigger(account.id, "conversation.message_received", contact)
    enroll_for_trigger(account.id, "conversation.message_received", contact)

    assert Lead.objects.filter(account=account, contact=contact).count() == 1


@pytest.mark.django_db
def test_create_lead_returns_existing_open_lead(account):
    contact = Contact.objects.create(account=account, phone="+260971111111")
    first = create_lead(account, contact, source="a")
    second = create_lead(account, contact, source="b")

    assert first.pk == second.pk
    assert Lead.objects.filter(account=account, contact=contact).count() == 1


@pytest.mark.django_db
def test_create_lead_creates_new_after_previous_converted(account):
    from apps.crm.services import convert_lead_to_deal

    contact = Contact.objects.create(account=account, phone="+260971111111")
    first = create_lead(account, contact)
    convert_lead_to_deal(first)

    second = create_lead(account, contact)
    assert second.pk != first.pk
