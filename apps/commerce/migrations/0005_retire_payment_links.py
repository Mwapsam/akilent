"""Retire order payment links: Akilent no longer takes payments for businesses.

- Orders still waiting on a payment link, or left Failed by a rejected link payment, go back
  to Pending, so the business marks them paid when the customer pays; open link payments are
  withdrawn.
- Automations with a "request payment" step are archived (the action no longer exists, so a
  published one would fail on every new order), and runs already in progress on a version
  with that step are cancelled, as archiving does, instead of failing on their next step.

Paid orders and their payments are history and are left as they are.
"""

from django.db import migrations
from django.utils import timezone


def _uses_request_payment(node) -> bool:
    if isinstance(node, dict):
        if node.get("action") == "request_payment":
            return True
        return any(_uses_request_payment(v) for v in node.values())
    if isinstance(node, list):
        return any(_uses_request_payment(v) for v in node)
    return False


def retire(apps, schema_editor):
    Order = apps.get_model("commerce", "Order")
    Payment = apps.get_model("commerce", "Payment")
    Workflow = apps.get_model("automation", "Workflow")
    WorkflowVersion = apps.get_model("automation", "WorkflowVersion")
    WorkflowRun = apps.get_model("automation", "WorkflowRun")
    WorkflowInteraction = apps.get_model("automation", "WorkflowInteraction")

    for payment in Payment.objects.filter(status="pending"):
        payment.status = "failed"
        payment.raw_payload = {**(payment.raw_payload or {}), "error": "withdrawn"}
        payment.save(update_fields=["status", "raw_payload", "updated_at"])
    Order.objects.filter(status__in=["awaiting_payment", "failed"]).update(
        status="pending"
    )

    archived = []
    for workflow in Workflow.objects.exclude(status="archived").only(
        "pk", "status", "definition"
    ):
        if _uses_request_payment(workflow.definition):
            workflow.status = "archived"
            workflow.paused_at = None
            workflow.save(update_fields=["status", "paused_at"])
            archived.append(workflow.pk)
    charging_versions = [
        v.pk
        for v in WorkflowVersion.objects.only("pk", "definition")
        if _uses_request_payment(v.definition)
    ]

    in_flight = WorkflowRun.objects.filter(status__in=["active", "waiting"])
    runs = list(
        in_flight.filter(workflow_id__in=archived).values_list("pk", flat=True)
    ) + list(
        in_flight.filter(version_id__in=charging_versions).values_list("pk", flat=True)
    )
    now = timezone.now()
    WorkflowRun.objects.filter(pk__in=runs).update(status="cancelled", completed_at=now)
    WorkflowInteraction.objects.filter(run_id__in=runs, status="open").update(
        status="cancelled", closed_at=now
    )


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0004_order_paid_method"),
        ("automation", "0011_step_run_fb_outbound"),
    ]

    operations = [migrations.RunPython(retire, migrations.RunPython.noop)]
