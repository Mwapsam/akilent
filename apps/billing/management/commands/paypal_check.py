"""Pre-flight check of the PayPal set-up, run before switching payments on or after a deploy.

Read-only: it fetches a token and reads the webhook, and changes nothing at PayPal or here.
Secrets are reported as set or missing, never printed."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

WEBHOOK_PATH_NAME = "billing:webhook-provider"


def required_events(subscriptions: bool) -> set[str]:
    """Every event the code acts on; the live webhook must deliver all of them."""
    from apps.billing.providers import paypal as orders
    from apps.billing.providers import paypal_subscriptions as subs

    events = {"CHECKOUT.ORDER.APPROVED", *orders.CAPTURE_EVENTS, *orders.REFUND_EVENTS}
    if subscriptions:
        events |= {
            *subs.SALE_EVENTS,
            *subs.SALE_REFUND_EVENTS,
            *subs.ENDED_EVENTS,
            subs.ACTIVATED_EVENT,
        }
    return events


class Command(BaseCommand):
    help = (
        "Check the PayPal set-up: settings, credentials (a token is fetched), the webhook's "
        "URL and events, and whether PayPal is the active provider. Changes nothing."
    )

    def handle(self, *args, **options):
        from apps.billing.paypal import PayPalError, config, get_paypal_client
        from apps.billing.providers import paypal_subscriptions as subs
        from apps.billing.providers import registry
        from apps.billing.providers.paypal import REQUIRED_SETTINGS
        from apps.core.models import SiteSettings

        failures: list[str] = []
        warnings: list[str] = []

        def ok(line):
            self.stdout.write(self.style.SUCCESS(f"OK    {line}"))

        def fail(line):
            failures.append(line)
            self.stdout.write(self.style.ERROR(f"FAIL  {line}"))

        def warn(line):
            warnings.append(line)
            self.stdout.write(self.style.WARNING(f"WARN  {line}"))

        # --- settings ------------------------------------------------------------------------
        env = config("PAYPAL_ENV")
        self.stdout.write(f"PAYPAL_ENV={env or '(empty)'}")
        for name in REQUIRED_SETTINGS:
            (ok if config(name) else fail)(
                f"{name} {'set' if config(name) else 'missing'}"
            )
        provider = registry.get("paypal")
        problem = (
            provider.config_status() if provider else "PayPal provider not registered."
        )
        if problem:
            fail(problem)
        else:
            ok("Provider settings complete")
        if env == "sandbox":
            warn(
                "Sandbox: payments are test money. Use PAYPAL_ENV=live to charge customers."
            )
        subscriptions = subs.enabled()
        (ok if subscriptions else warn)(
            "PAYPAL_SUBSCRIPTIONS on: renewals are automatic"
            if subscriptions
            else "PAYPAL_SUBSCRIPTIONS off: renewals go out as pay links"
        )

        active = SiteSettings.load().billing_provider
        (ok if active == "paypal" else warn)(f"Active billing provider: {active}")

        can_call = (
            config("PAYPAL_CLIENT_ID")
            and config("PAYPAL_CLIENT_SECRET")
            and env in ("sandbox", "live")
        )
        if not can_call:
            raise CommandError(f"PayPal check: {len(failures)} problem(s).")

        # --- credentials ---------------------------------------------------------------------
        try:
            client = get_paypal_client()
            client._token(fresh=True)
        except PayPalError as exc:
            fail(f"Credentials refused by PayPal {env}: {exc}")
            raise CommandError(f"PayPal check: {len(failures)} problem(s).") from exc
        ok(f"Credentials accepted by PayPal {env}")

        # --- webhook -------------------------------------------------------------------------
        webhook_id = config("PAYPAL_WEBHOOK_ID")
        if webhook_id:
            try:
                hook = client.get_webhook(webhook_id)
            except PayPalError as exc:
                fail(f"Webhook {webhook_id} not found for these credentials: {exc}")
            else:
                self._check_webhook(hook, subscriptions, ok, fail, warn)

        self.stdout.write(
            "Not checkable here: 'PayPal account optional' (card button) and invoice-id "
            "uniqueness in the PayPal account settings; PAYPAL_MERCHANT_ID matching the payee "
            "is checked on the first capture."
        )
        if failures:
            raise CommandError(f"PayPal check: {len(failures)} problem(s).")
        self.stdout.write(
            self.style.SUCCESS(f"PayPal check passed ({len(warnings)} warning(s)).")
        )

    def _check_webhook(self, hook, subscriptions, ok, fail, warn):
        expected_url = settings.SITE_URL.rstrip("/") + reverse(
            WEBHOOK_PATH_NAME, kwargs={"provider": "paypal"}
        )
        url = hook.get("url") or ""
        if url.rstrip("/") == expected_url.rstrip("/"):
            ok(f"Webhook URL {url}")
        else:
            fail(f"Webhook URL is {url or '(none)'}, expected {expected_url}")

        names = {e.get("name") for e in hook.get("event_types") or []}
        if "*" in names:
            warn(
                "Webhook subscribes to all events (*); live should list only the events used"
            )
            return
        missing = sorted(required_events(subscriptions) - names)
        if missing:
            fail(f"Webhook is missing events: {', '.join(missing)}")
        else:
            ok(
                f"Webhook delivers all {len(required_events(subscriptions))} events used"
            )
