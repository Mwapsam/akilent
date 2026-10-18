from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.billing.models import Plan

PLANS = [
    {
        "slug": Plan.TRIAL,
        "name": "Trial",
        "service_type": Plan.SERVICE_BOTH,
        "price_monthly": Decimal("0.00"),
        "max_conversations_per_month": 100,
        "max_emails_per_month": 500,
        "max_automation_rules": 2,
        "max_whatsapp_numbers": 1,
        "trial_days": 14,
        "has_priority_support": False,
        "email_apis": True,
        "inbound_email": False,
        "tracking_webhooks": False,
        "detailed_analytics": False,
        "log_retention_days": 3,
        "flutterwave_plan_id": None,
        "is_active": True,
    },
    {
        "slug": Plan.STARTER,
        "name": "Starter",
        "service_type": Plan.SERVICE_EMAIL,
        "price_monthly": Decimal("19.00"),
        "max_conversations_per_month": 1000,
        "max_emails_per_month": 10000,
        "max_automation_rules": 10,
        "max_whatsapp_numbers": 1,
        "trial_days": 0,
        "has_priority_support": False,
        "email_apis": True,
        "inbound_email": True,
        "tracking_webhooks": False,
        "detailed_analytics": False,
        "bulk_email": True,
        "max_bulk_recipients_per_campaign": 1000,
        "log_retention_days": 14,
        "flutterwave_plan_id": None,
        "is_active": True,
    },
    {
        "slug": Plan.PROFESSIONAL,
        "name": "Professional",
        "service_type": Plan.SERVICE_WHATSAPP,
        "price_monthly": Decimal("49.00"),
        "max_conversations_per_month": 5000,
        "max_emails_per_month": 50000,
        "max_automation_rules": 50,
        "max_whatsapp_numbers": 1,  # one until per-number routing (billing 0025)
        "trial_days": 0,
        "has_priority_support": True,
        "email_apis": True,
        "inbound_email": True,
        "tracking_webhooks": True,
        "detailed_analytics": True,
        "bulk_email": True,
        "max_bulk_recipients_per_campaign": 5000,
        "log_retention_days": 30,
        "flutterwave_plan_id": None,
        "is_active": True,
    },
    {
        "slug": Plan.BUSINESS,
        "name": "Business",
        "service_type": Plan.SERVICE_BOTH,
        "price_monthly": Decimal("99.00"),
        "max_conversations_per_month": -1,
        "max_emails_per_month": -1,
        "max_automation_rules": -1,
        "max_whatsapp_numbers": 1,  # one until per-number routing (billing 0025)
        "trial_days": 0,
        "has_priority_support": True,
        "email_apis": True,
        "inbound_email": True,
        "tracking_webhooks": True,
        "detailed_analytics": True,
        "bulk_email": True,
        "max_bulk_recipients_per_campaign": -1,
        "log_retention_days": 90,
        "flutterwave_plan_id": None,
        "is_active": True,
    },
]


class Command(BaseCommand):
    help = "Seed the four subscription plans (Trial/Starter/Professional/Business)"

    def handle(self, *args, **options):
        for data in PLANS:
            slug = data.pop("slug")
            plan, created = Plan.objects.update_or_create(slug=slug, defaults=data)
            action = "Created" if created else "Updated"
            self.stdout.write(f"{action}: {plan.name} (${plan.price_monthly}/mo)")
        self.stdout.write(self.style.SUCCESS("Plans seeded successfully."))
