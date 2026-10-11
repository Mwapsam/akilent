"""Management command: import and link HubSpot contacts to Akilent contacts.

Pages through HubSpot's contacts API, matches each record to an Akilent
Contact by email, and creates a HubSpotContactLink when a match is found.
Existing links are skipped (idempotent).

Usage:
    python manage.py hubspot_import_contacts
    python manage.py hubspot_import_contacts --account-id 42
    python manage.py hubspot_import_contacts --dry-run
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.hubspot.client import HubSpotAPIError, get_fresh_client
from apps.hubspot.field_map import CONTACT_FIELD_MAP
from apps.hubspot.models import HubSpotConnection, HubSpotContactLink


class Command(BaseCommand):
    help = (
        "Import HubSpot contacts and link them to matching Akilent contacts by email."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--account-id",
            type=int,
            help="Only process this Akilent account ID (default: all active connections).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be linked without writing anything.",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=200,
            help="Maximum pages to fetch per account (100 contacts/page).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        max_pages = options["max_pages"]

        qs = HubSpotConnection.objects.filter(
            status=HubSpotConnection.Status.ACTIVE
        ).select_related("account")
        if options["account_id"]:
            qs = qs.filter(account_id=options["account_id"])

        if not qs.exists():
            raise CommandError("No active HubSpot connections found.")

        for conn in qs:
            self.stdout.write(f"\nAccount: {conn.account} (portal {conn.portal_id})")
            try:
                total = self._import_for_connection(
                    conn, dry_run=dry_run, max_pages=max_pages
                )
                self.stdout.write(self.style.SUCCESS(f"  {total} contacts linked."))
            except HubSpotAPIError as exc:
                self.stderr.write(self.style.ERROR(f"  API error: {exc}"))

    def _import_for_connection(self, conn, *, dry_run: bool, max_pages: int) -> int:
        from apps.contacts.models import Contact

        client = get_fresh_client(conn)
        account = conn.account
        properties = ",".join(CONTACT_FIELD_MAP.values()) + ",email"
        linked = 0
        after = None

        for page in range(max_pages):
            params: dict = {"limit": 100, "properties": properties}
            if after:
                params["after"] = after

            data = client.get("/crm/v3/objects/contacts", **params)
            results = data.get("results", [])
            if not results:
                break

            for obj in results:
                hs_id = str(obj.get("id", ""))
                props = obj.get("properties", {})
                email = (props.get("email") or "").strip().lower()

                if not email or not hs_id:
                    continue

                # Already linked?
                if HubSpotContactLink.objects.filter(
                    account=account, hs_object_id=hs_id
                ).exists():
                    continue

                # Match by email
                contact = Contact.objects.filter(
                    account=account, email__iexact=email
                ).first()
                if contact is None:
                    self.stdout.write(f"  no match: {email} (hs {hs_id})")
                    continue

                if dry_run:
                    self.stdout.write(
                        f"  [dry-run] would link {email} → contact {contact.pk}"
                    )
                else:
                    HubSpotContactLink.objects.get_or_create(
                        account=account,
                        contact=contact,
                        defaults={"hs_object_id": hs_id},
                    )
                    self.stdout.write(f"  linked {email} → contact {contact.pk}")
                linked += 1

            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break

        return linked
