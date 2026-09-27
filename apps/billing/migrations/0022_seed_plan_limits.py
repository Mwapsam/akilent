"""Represent today's effective limits in the limit catalog. Not a pricing decision.

- Limits that were enforced from a Plan column keep that column's value: conversations, emails,
  WhatsApp numbers, recipients per email campaign.
- Everything else was never capped per plan, so it starts unlimited (-1): WhatsApp message
  counts, verification codes, recipients per WhatsApp campaign, emails per day, contacts, team
  members, and active automations (``max_automation_rules`` was shown but never enforced). AI
  keeps its existing site-wide daily ceiling (AI_DAILY_CALL_LIMIT) above every plan.
- Usage so far (UsageSummary) is copied into the new counters.

Keys are written out here so this migration keeps meaning what it meant when it ran.
"""
from django.db import migrations

FROM_COLUMN = {
    "conversations_month": "max_conversations_per_month",
    "whatsapp_numbers": "max_whatsapp_numbers",
    "emails_month": "max_emails_per_month",
    "email_campaign_recipients": "max_bulk_recipients_per_campaign",
}
UNLIMITED = [
    "whatsapp_marketing_msgs", "whatsapp_utility_msgs", "verification_codes_month",
    "whatsapp_campaign_recipients", "emails_day", "ai_actions_day", "automation_rules", "contacts",
    "team_members",
]


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanLimit = apps.get_model("billing", "PlanLimit")
    UsageSummary = apps.get_model("billing", "UsageSummary")
    UsageCounter = apps.get_model("billing", "UsageCounter")

    for plan in Plan.objects.all():
        for key, column in FROM_COLUMN.items():
            PlanLimit.objects.get_or_create(plan=plan, key=key, defaults={"value": getattr(plan, column)})
        for key in UNLIMITED:
            PlanLimit.objects.get_or_create(plan=plan, key=key, defaults={"value": -1})

    for row in UsageSummary.objects.all():
        for key, used in (("conversations_month", row.conversations_used), ("emails_month", row.emails_used)):
            if used:
                UsageCounter.objects.get_or_create(account_id=row.account_id, key=key,
                                                   period_start=row.period_start, defaults={"used": used})


def backwards(apps, schema_editor):
    apps.get_model("billing", "PlanLimit").objects.all().delete()
    apps.get_model("billing", "UsageCounter").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("billing", "0021_usage_limits")]
    operations = [migrations.RunPython(forwards, backwards)]
