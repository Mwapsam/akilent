import pytest

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.contacts.models import Contact
from apps.conversations.models import Event
from apps.core.actions import ActionError, run_action
from apps.crm.models import Deal, Lead, Pipeline
from apps.crm.services import convert_lead_to_deal, create_lead, move_deal_stage


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme Real Estate")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971111111")


@pytest.mark.django_db
def test_create_lead_emits_event(account, contact):
    lead = create_lead(account, contact, source="whatsapp")

    assert lead.status == Lead.Status.NEW
    assert Event.objects.filter(type="lead.created", subject_id=lead.public_id).exists()


@pytest.mark.django_db
def test_convert_lead_creates_deal_in_default_pipeline_first_stage(account, contact):
    lead = create_lead(account, contact)

    deal = convert_lead_to_deal(lead, title="3BR house", value=250000)

    lead.refresh_from_db()
    assert lead.status == Lead.Status.CONVERTED
    assert lead.converted_to_deal_id == deal.id
    assert deal.pipeline.is_default is True
    assert deal.stage.name == "New"
    assert Event.objects.filter(type="deal.created", subject_id=deal.public_id).exists()


@pytest.mark.django_db
def test_convert_already_converted_lead_raises(account, contact):
    lead = create_lead(account, contact)
    convert_lead_to_deal(lead)

    with pytest.raises(ValueError):
        convert_lead_to_deal(lead)


@pytest.mark.django_db
def test_move_deal_to_won_stage_closes_deal_and_emits_event(account, contact):
    lead = create_lead(account, contact)
    deal = convert_lead_to_deal(lead)
    won_stage = deal.pipeline.stages.get(is_won=True)

    move_deal_stage(deal, won_stage)

    deal.refresh_from_db()
    assert deal.status == Deal.Status.WON
    assert deal.closed_at is not None
    assert Event.objects.filter(type="deal.stage_changed", subject_id=deal.public_id).exists()


@pytest.mark.django_db
def test_move_deal_to_stage_from_other_pipeline_rejected(account, contact):
    lead = create_lead(account, contact)
    deal = convert_lead_to_deal(lead)
    other_pipeline = Pipeline.objects.create(account=account, name="Other", slug="other")
    from apps.crm.models import Stage
    other_stage = Stage.objects.create(pipeline=other_pipeline, name="Somewhere", order=0)

    with pytest.raises(ValueError):
        move_deal_stage(deal, other_stage)


@pytest.mark.django_db
def test_lead_created_enrolls_published_workflow(account, contact):
    Workflow.objects.create(
        account=account, name="New lead follow-up", slug="new-lead-followup",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "lead.created"},
            "steps": [{"id": "stop", "type": "stop"}],
        },
    )
    create_lead(account, contact)
    assert WorkflowRun.objects.filter(contact=contact).exists()


@pytest.mark.django_db
def test_action_registry_create_lead_and_create_deal(account, contact):
    result = run_action("create_lead", {}, account=account, contact=contact, source="ad")
    lead = Lead.objects.get(public_id=result["lead_id"])

    deal_result = run_action("create_deal", {}, lead=lead, title="Deal via registry")
    assert Deal.objects.filter(public_id=deal_result["deal_id"]).exists()


@pytest.mark.django_db
def test_action_registry_change_deal_stage_rejects_foreign_stage(account, contact):
    lead = create_lead(account, contact)
    deal = convert_lead_to_deal(lead)
    other_pipeline = Pipeline.objects.create(account=account, name="Other2", slug="other2")
    from apps.crm.models import Stage
    other_stage = Stage.objects.create(pipeline=other_pipeline, name="Elsewhere", order=0)

    with pytest.raises(ActionError):
        run_action("change_deal_stage", {}, deal=deal, stage=other_stage)


@pytest.mark.django_db
def test_pipeline_ensure_default_is_idempotent(account):
    p1 = Pipeline.ensure_default(account)
    p2 = Pipeline.ensure_default(account)
    assert p1.id == p2.id
    assert p1.stages.count() == 4
