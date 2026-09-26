"""Represent today's access in the feature catalog. This is not a pricing decision.

Every existing plan keeps exactly what its businesses can do today:
- each old Plan boolean that is on becomes the matching feature;
- the module features (automations, AI, sales, orders, WhatsApp campaigns, verification codes)
  were never tied to a plan, so every existing plan includes them.

A business the old module switches had blocked keeps that block, as an operator removal it can
clear. Sales and Orders switches stay what they are: the owner's own choice. WhatsApp templates
become core (the one deliberate widening: without them the inbox can't reopen a conversation).

Also adds a hidden "Pilot" plan with every feature, to move pilot businesses onto before the
real tiers are set.

Keys are written out here rather than imported from the catalog, so this migration keeps meaning
what it meant when it ran.
"""
from decimal import Decimal

from django.db import migrations

LEGACY_FLAGS = {
    "email_apis": "email_sending",
    "email_templates": "email_templates",
    "bulk_email": "email_campaigns",
    "tracking_webhooks": "email_tracking",
    "outbound_webhooks": "webhooks",
    "detailed_analytics": "insights",
    "has_priority_support": "priority_support",
}
MODULE_FEATURES = ["whatsapp_campaigns", "verification_codes", "automations", "ai_assistant",
                   "sales", "orders"]
ALL_SELLABLE = MODULE_FEATURES + list(LEGACY_FLAGS.values())
# A disabled module row that was a commercial block becomes removal(s).
MODULE_REMOVALS = {
    "whatsapp": ["whatsapp_campaigns", "verification_codes"],
    "automation": ["automations"],
    "ai": ["ai_assistant"],
}
NOTE = "Carried over from the old module switch"


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanFeature = apps.get_model("billing", "PlanFeature")
    ModuleSubscription = apps.get_model("billing", "ModuleSubscription")
    AccountFeatureOverride = apps.get_model("billing", "AccountFeatureOverride")

    for plan in Plan.objects.all():
        keys = {key for flag, key in LEGACY_FLAGS.items() if getattr(plan, flag, False)}
        keys.update(MODULE_FEATURES)
        for key in keys:
            PlanFeature.objects.get_or_create(plan=plan, key=key)

    for row in ModuleSubscription.objects.filter(enabled=False, module__in=list(MODULE_REMOVALS)):
        for key in MODULE_REMOVALS[row.module]:
            AccountFeatureOverride.objects.get_or_create(
                account_id=row.account_id, key=key, defaults={"grant": False, "note": NOTE})

    pilot, created = Plan.objects.get_or_create(slug="pilot", defaults={
        "name": "Pilot", "service_type": "both", "price_monthly": Decimal("0.00"),
        "max_conversations_per_month": -1, "max_emails_per_month": -1, "max_automation_rules": -1,
        "max_whatsapp_numbers": 10, "max_bulk_recipients_per_campaign": -1,
        "log_retention_days": 90, "is_active": False,
        "email_apis": True, "email_templates": True, "bulk_email": True, "tracking_webhooks": True,
        "outbound_webhooks": True, "detailed_analytics": True, "has_priority_support": True,
    })
    if created:
        for key in ALL_SELLABLE:
            PlanFeature.objects.get_or_create(plan=pilot, key=key)


def backwards(apps, schema_editor):
    apps.get_model("billing", "PlanFeature").objects.all().delete()
    apps.get_model("billing", "AccountFeatureOverride").objects.filter(note=NOTE).delete()
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(slug="pilot", subscriptions__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("billing", "0019_plan_features")]
    operations = [migrations.RunPython(forwards, backwards)]
