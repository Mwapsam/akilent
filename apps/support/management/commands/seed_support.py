"""Idempotently seed support configuration: categories, queues, and SLA policies."""

from django.core.management.base import BaseCommand

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

# (slug, name, parent_slug | None)
CATEGORIES = [
    # Top-level
    ("account", "Account", None),
    ("billing", "Billing", None),
    ("payments", "Payments", None),
    ("technical", "Technical", None),
    ("whatsapp", "WhatsApp", None),
    ("email", "Email", None),
    ("conversations", "Conversations", None),
    ("contacts", "Contacts", None),
    ("leads", "Leads", None),
    ("orders", "Orders", None),
    ("integrations", "Integrations", None),
    ("onboarding", "Onboarding", None),
    ("security", "Security", None),
    ("general", "General", None),
    # Subcategories — payments
    ("payment-failed", "Payment Failed", "payments"),
    ("payment-pending", "Payment Pending", "payments"),
    ("payment-reversed", "Payment Reversed", "payments"),
    ("settlement", "Settlement", "payments"),
    ("reconciliation", "Reconciliation", "payments"),
    # Subcategories — whatsapp
    ("whatsapp-connection", "Connection", "whatsapp"),
    ("whatsapp-message-delivery", "Message Delivery", "whatsapp"),
    ("whatsapp-verification", "Verification", "whatsapp"),
    # Subcategories — technical
    ("technical-bugs", "Bugs", "technical"),
    ("technical-performance", "Performance", "technical"),
    ("technical-downtime", "Downtime", "technical"),
    ("technical-data", "Data Issues", "technical"),
    # Subcategories — account
    ("account-login", "Login", "account"),
    ("account-password", "Password", "account"),
    ("account-mfa", "MFA", "account"),
    ("account-team", "Team Members", "account"),
]

# (slug, name, restricted)
QUEUES = [
    ("general", "General Support", False),
    ("account", "Account & Access", False),
    ("payments", "Payments", False),
    ("billing", "Billing", False),
    ("technical", "Technical", False),
    ("whatsapp", "WhatsApp", False),
    ("email", "Email", False),
    ("integrations", "Integrations", False),
    ("security", "Security", True),
]

# (customer_tier, priority, first_response_minutes, update_frequency_minutes,
#  resolution_minutes, escalation_after_minutes)
SLA_POLICIES = [
    # Tier 1 — Standard
    (1, "p1", 480, None, 1440, None),
    (1, "p2", 480, None, 2880, None),
    (1, "p3", 480, None, 4320, None),
    (1, "p4", 480, None, 7200, None),
    # Tier 2 — Growth
    (2, "p1", 240, 120, 960, None),
    (2, "p2", 240, None, 1440, None),
    (2, "p3", 480, None, 2880, None),
    (2, "p4", 480, None, 7200, None),
    # Tier 3 — Business
    (3, "p1", 120, 60, 480, 30),
    (3, "p2", 240, None, 960, None),
    (3, "p3", 480, None, 2880, None),
    (3, "p4", 480, None, 7200, None),
    # Tier 4 — Enterprise / Strategic
    (4, "p1", 30, 30, 240, 15),
    (4, "p2", 60, 60, 480, 30),
    (4, "p3", 120, None, 960, None),
    (4, "p4", 480, None, 4320, None),
]


class Command(BaseCommand):
    help = "Seed support categories, queues, and SLA policies (idempotent)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print what would be created without saving.",
        )

    def handle(self, *args, **options):
        from apps.support.models import SLAPolicy, SupportCategory, SupportQueue

        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(
                self.style.WARNING("Dry run — no changes will be saved.\n")
            )

        # ------------------------------------------------------------------ #
        # Categories
        # ------------------------------------------------------------------ #
        self.stdout.write("Categories:")
        created_cats = 0
        category_map: dict[str, SupportCategory] = {}

        # Two passes: parents first, then children.
        for slug, name, parent_slug in CATEGORIES:
            parent = category_map.get(parent_slug) if parent_slug else None

            if dry_run:
                self.stdout.write(
                    f"  {'[sub] ' if parent_slug else '      '}{slug} — {name}"
                )
                continue

            obj, created = SupportCategory.objects.get_or_create(
                slug=slug,
                parent=parent,
                defaults={"name": name, "is_active": True},
            )
            if not created and obj.name != name:
                obj.name = name
                obj.save(update_fields=["name"])
            category_map[slug] = obj
            if created:
                created_cats += 1
                self.stdout.write(f"  + {slug}")
            else:
                self.stdout.write(f"    {slug} (exists)")

        if not dry_run:
            self.stdout.write(f"  → {created_cats} created.\n")

        # ------------------------------------------------------------------ #
        # Queues
        # ------------------------------------------------------------------ #
        self.stdout.write("Queues:")
        created_queues = 0

        for slug, name, restricted in QUEUES:
            if dry_run:
                self.stdout.write(
                    f"  {slug} — {name}{'  [restricted]' if restricted else ''}"
                )
                continue

            queue, created = SupportQueue.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "restricted": restricted, "is_active": True},
            )
            if not created and (queue.name != name or queue.restricted != restricted):
                queue.name = name
                queue.restricted = restricted
                queue.save(update_fields=["name", "restricted"])
            if created:
                created_queues += 1
                self.stdout.write(f"  + {slug}")
            else:
                self.stdout.write(f"    {slug} (exists)")

        if not dry_run:
            self.stdout.write(f"  → {created_queues} created.\n")

        # ------------------------------------------------------------------ #
        # SLA policies
        # ------------------------------------------------------------------ #
        self.stdout.write("SLA Policies:")
        created_sla = 0

        for (
            tier,
            priority,
            first_resp,
            update_freq,
            resolution,
            escalation_after,
        ) in SLA_POLICIES:
            if dry_run:
                self.stdout.write(
                    f"  T{tier}/{priority.upper()} — first_response={first_resp}m "
                    f"resolution={resolution}m"
                )
                continue

            sla, created = SLAPolicy.objects.get_or_create(
                customer_tier=tier,
                priority=priority,
                defaults={
                    "first_response_minutes": first_resp,
                    "update_frequency_minutes": update_freq,
                    "resolution_minutes": resolution,
                    "escalation_after_minutes": escalation_after,
                    "is_active": True,
                },
            )
            if not created:
                updated = False
                for field, value in [
                    ("first_response_minutes", first_resp),
                    ("update_frequency_minutes", update_freq),
                    ("resolution_minutes", resolution),
                    ("escalation_after_minutes", escalation_after),
                ]:
                    if getattr(sla, field) != value:
                        setattr(sla, field, value)
                        updated = True
                if updated:
                    sla.save()

            if created:
                created_sla += 1
                self.stdout.write(f"  + T{tier}/{priority.upper()}")
            else:
                self.stdout.write(f"    T{tier}/{priority.upper()} (exists)")

        if not dry_run:
            self.stdout.write(f"  → {created_sla} created.\n")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry run complete. Run without --dry-run to apply."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
