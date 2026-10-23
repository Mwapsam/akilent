from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.billing import payments
from apps.billing.models import Subscription
from apps.billing.providers import ProviderError, registry

LEGACY = (("stripe", "stripe_subscription_id"), ("flutterwave", "fw_subscription_id"))


class Command(BaseCommand):
    help = (
        "Move subscriptions still renewed by a Stripe or Flutterwave recurring subscription onto "
        "Akilent's renewal engine. The provider stops renewing at the end of the paid period "
        "(nothing is refunded or cut short), the saved card is kept where the provider gives it "
        "to us, and from then on renewals use credit first and the active provider."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, dry_run, **options):
        legacy = Subscription.objects.filter(
            Q(stripe_subscription_id__isnull=False) & ~Q(stripe_subscription_id="")
            | Q(fw_subscription_id__isnull=False) & ~Q(fw_subscription_id="")
        ).select_related("account", "plan")
        moved = failed = 0
        for sub in legacy:
            for code, field in LEGACY:
                remote_id = getattr(sub, field)
                if not remote_id:
                    continue
                name = f"{sub.account.company_name} ({code} {remote_id})"
                if dry_run:
                    self.stdout.write(f"Would move {name}")
                    continue
                provider = registry.get(code)
                try:
                    saved = provider.cancel_legacy(sub) if provider else None
                except ProviderError as exc:
                    failed += 1
                    self.stderr.write(f"Couldn't stop {name}: {exc}")
                    continue
                if saved is not None and saved.token:
                    payments.save_method(sub.account, code, saved)
                setattr(sub, field, None)
                sub.provider = sub.provider or code
                sub.save(update_fields=[field, "provider", "updated_at"])
                moved += 1
                kept = "card kept" if saved else "customer pays by link at renewal"
                self.stdout.write(f"Moved {name}: {kept}")
        self.stdout.write(self.style.SUCCESS(f"{moved} moved, {failed} failed"))
