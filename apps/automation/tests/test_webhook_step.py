from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun, WorkflowWebhookDelivery
from apps.automation.tasks import deliver_workflow_webhook
from apps.automation.workflow_engine import advance_run, enroll, validate_definition
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="Acme")
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=1000, email_apis=True, api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    return acc


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, email="user@example.com", first_name="Ada")


def _wf(account, steps):
    return Workflow.objects.create(
        account=account, name="WF", status=Workflow.Status.PUBLISHED,
        definition={"trigger": {"type": "manual"}, "steps": steps},
    )


class _FakeResponse:
    status_code = 200
    def raise_for_status(self):
        pass


def test_validate_definition_flags_missing_webhook_url():
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "webhook", "next": "b"}, {"id": "b", "type": "stop"}],
    })
    e = next((e for e in errors if e["step_id"] == "a" and e["field"] == "url"), None)
    assert e is not None


@pytest.mark.django_db
def test_webhook_step_queues_delivery_and_completes(account, contact):
    """enroll() runs the (eager) delivery task inline, so both externals are mocked up front."""
    wf = _wf(account, [
        {"id": "a", "type": "webhook", "url": "https://example.com/hooks/akilent",
         "body": {"kind": "signup"}, "next": "b"},
        {"id": "b", "type": "stop"},
    ])

    with patch("apps.automation.tasks.requests.request", return_value=_FakeResponse()) as mock_req, \
         patch("apps.email.services.datafetch._assert_public_https", return_value="example.com"):
        run = enroll(wf, contact)

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    delivery = WorkflowWebhookDelivery.objects.get(run=run, step_id="a")
    assert delivery.status == WorkflowWebhookDelivery.Status.SUCCEEDED
    assert delivery.url == "https://example.com/hooks/akilent"
    assert mock_req.call_args.kwargs["timeout"] == 10
    assert mock_req.call_args.kwargs["allow_redirects"] is False


@pytest.mark.django_db
def test_webhook_delivery_retries_on_failure(account, contact):
    wf = _wf(account, [{"id": "a", "type": "webhook", "url": "https://example.com/hook"}])

    # Prevent the eager .delay() inside enroll() from making a live call —
    # create the row only, then drive delivery explicitly below.
    with patch("apps.automation.tasks.deliver_workflow_webhook.delay"):
        run = enroll(wf, contact)
    delivery = WorkflowWebhookDelivery.objects.get(run=run, step_id="a")

    with patch("apps.email.services.datafetch._assert_public_https", return_value="example.com"), \
         patch("apps.automation.tasks.requests.request", side_effect=ConnectionError("refused")):
        with pytest.raises(Exception):
            deliver_workflow_webhook(delivery.pk)

    delivery.refresh_from_db()
    assert delivery.status == WorkflowWebhookDelivery.Status.FAILED
    assert delivery.attempt_count == 1


@pytest.mark.django_db
def test_webhook_delivery_exhausts_after_max_retries(account, contact):
    wf = _wf(account, [{"id": "a", "type": "webhook", "url": "https://example.com/hook"}])

    with patch("apps.automation.tasks.deliver_workflow_webhook.delay"):
        run = enroll(wf, contact)
    delivery = WorkflowWebhookDelivery.objects.get(run=run, step_id="a")

    deliver_workflow_webhook.push_request(retries=6)
    try:
        with patch("apps.email.services.datafetch._assert_public_https", return_value="example.com"), \
             patch("apps.automation.tasks.requests.request", side_effect=ConnectionError("refused")):
            deliver_workflow_webhook(delivery.pk)
    finally:
        deliver_workflow_webhook.pop_request()

    delivery.refresh_from_db()
    assert delivery.status == WorkflowWebhookDelivery.Status.EXHAUSTED


@pytest.mark.django_db
def test_webhook_delivery_rejects_private_url_without_retry(account, contact):
    wf = _wf(account, [{"id": "a", "type": "webhook", "url": "https://127.0.0.1/admin"}])

    with patch("apps.automation.tasks.deliver_workflow_webhook.delay"):
        run = enroll(wf, contact)
    delivery = WorkflowWebhookDelivery.objects.get(run=run, step_id="a")

    with patch("apps.automation.tasks.requests.request") as mock_req:
        deliver_workflow_webhook(delivery.pk)

    mock_req.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == WorkflowWebhookDelivery.Status.EXHAUSTED
    assert "non-public" in delivery.last_error or "resolves" in delivery.last_error


@pytest.mark.django_db
def test_advance_run_does_not_double_queue_webhook_on_reentry(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "webhook", "url": "https://example.com/hook", "next": "w"},
        {"id": "w", "type": "wait", "seconds": 3600, "next": "stop"},
        {"id": "stop", "type": "stop"},
    ])

    with patch("apps.automation.tasks.requests.request", return_value=_FakeResponse()), \
         patch("apps.email.services.datafetch._assert_public_https", return_value="example.com"):
        run = enroll(wf, contact)
    run.refresh_from_db()

    with patch("apps.automation.tasks.requests.request", return_value=_FakeResponse()) as mock_req:
        advance_run(run)
        advance_run(run)

    mock_req.assert_not_called()
    assert WorkflowWebhookDelivery.objects.filter(run=run, step_id="a").count() == 1
