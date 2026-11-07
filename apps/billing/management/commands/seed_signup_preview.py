"""Make a development database sell what production sells, to try the signup wizard against it.

Reads the plans and channels from a live signup page's public wizard config (the JSON in
#signup-wizard-config: plan prices, trial days, the channels each plan gives, the rows the
cards compare and what every plan includes) and applies them here:

- each plan's name, monthly price and trial days (created if missing);
- each plan's channels, compared features and the conversations limit;
- the site's channel switches: the channels production offers are made public, the rest
  switched off.

Before changing anything it saves what it is about to change to a backup file, and
``--undo`` puts that back. Refuses to run unless DEBUG is on, so it can't touch production.

A channel switched off in settings (e.g. WHATSAPP_ENABLED=False in .env) stays off: the
command says so, and that one is yours to turn on.

    python manage.py seed_signup_preview                    # from https://akilent.com/signup/
    python manage.py seed_signup_preview --file offer.html  # a saved copy of the page
    python manage.py seed_signup_preview --undo
"""

import json
import re
import urllib.request
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEFAULT_URL = "https://akilent.com/signup/"
LIMIT_RE = re.compile(r"^(Unlimited|[\d,]+) (.+)$")


def _backup_path() -> Path:
    return Path(settings.BASE_DIR) / ".signup-preview-backup.json"


def wizard_config(html: str) -> dict:
    """The JSON in a signup page's #signup-wizard-config script tag."""
    marker = 'id="signup-wizard-config"'
    if marker not in html:
        raise CommandError("That page has no signup wizard config.")
    start = html.index(">", html.index(marker)) + 1
    return json.loads(html[start : html.index("</script>", start)])


class Command(BaseCommand):
    help = "Copy production's signup plans and channels into this development database."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=DEFAULT_URL, help="Signup page to read.")
        parser.add_argument("--file", help="A saved copy of the signup page instead.")
        parser.add_argument(
            "--undo", action="store_true", help="Put back what the last run changed."
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Development only: DEBUG is off.")
        if options["undo"]:
            self._undo()
            return
        if options["file"]:
            html = Path(options["file"]).read_text(encoding="utf-8")
        else:
            with urllib.request.urlopen(options["url"], timeout=20) as resp:  # noqa: S310
                html = resp.read().decode("utf-8")
        self._apply(wizard_config(html))

    # ---- apply ------------------------------------------------------------------------------

    @transaction.atomic
    def _apply(self, cfg: dict) -> None:
        from apps.billing import api as billing_api
        from apps.billing import features as catalog
        from apps.billing.models import Plan, PlanFeature, PlanLimit
        from apps.core.channels import available_channels, invalidate_channel_cache
        from apps.core.models import SiteSettings

        offered = [c["value"] for c in cfg.get("channelChoices", [])]
        by_name = {
            f.name: f.key
            for f in catalog.matrix_features()
            if f.key not in catalog.CHANNEL_FEATURES
        }
        limit_keys = {label: key for key, label, _ in billing_api._PRICING_LIMITS}
        site = SiteSettings.load()
        slugs = [p["slug"] for p in cfg.get("plans", [])]
        if not _backup_path().exists():
            _backup_path().write_text(json.dumps(self._snapshot(site, slugs), indent=1))
            self.stdout.write(f"Saved the current values to {_backup_path()}")

        for data in cfg.get("plans", []):
            plan, created = Plan.objects.update_or_create(
                slug=data["slug"],
                defaults={
                    "name": data["name"],
                    "price_monthly": Decimal(str(data["priceMonthly"])),
                    "trial_days": data.get("trialDays") or 0,
                    "is_active": True,
                },
            )
            billing_api.set_plan_channels(plan, data.get("channels", []))
            keep, drop = set(), set()
            for name in cfg.get("included", []):
                if name in by_name:
                    keep.add(by_name[name])
            for row in data.get("compare", []):
                label, ok = row["label"], row["ok"]
                if label in by_name:
                    (keep if ok else drop).add(by_name[label])
                    continue
                m = LIMIT_RE.match(label)
                if m and m.group(2) in limit_keys:
                    raw = m.group(1)
                    value = -1 if raw == "Unlimited" else int(raw.replace(",", ""))
                    PlanLimit.objects.update_or_create(
                        plan=plan, key=limit_keys[m.group(2)], defaults={"value": value}
                    )
            PlanFeature.objects.bulk_create(
                [PlanFeature(plan=plan, key=k) for k in keep], ignore_conflicts=True
            )
            PlanFeature.objects.filter(plan=plan, key__in=drop - keep).delete()
            self.stdout.write(
                f"{'Created' if created else 'Updated'} {plan.name}: "
                f"${plan.price_monthly}/mo, channels {', '.join(data.get('channels', []))}"
            )
        Plan.objects.filter(is_active=True).exclude(slug__in=slugs).update(
            is_active=False
        )

        known = set(available_channels()) | set(offered)
        site.disabled_channels = sorted(known - set(offered))
        site.restricted_channels = sorted(
            set(site.restricted_channels or []) - set(offered)
        )
        site.save(update_fields=["disabled_channels", "restricted_channels"])
        billing_api.forget()
        invalidate_channel_cache()

        off_in_settings = sorted(set(offered) - set(available_channels()))
        self.stdout.write(
            f"Channels offered: {', '.join(offered)}; "
            f"switched off: {', '.join(site.disabled_channels) or 'none'}."
        )
        if off_in_settings:
            names = ", ".join(f"{c.upper()}_ENABLED=True" for c in off_in_settings)
            self.stdout.write(
                self.style.WARNING(
                    f"Off in settings, so not offered here yet: set {names} in .env "
                    "and restart the server."
                )
            )
        self.stdout.write(self.style.SUCCESS("Done. Undo with --undo."))

    def _snapshot(self, site, slugs) -> dict:
        from apps.billing.models import Plan, PlanFeature, PlanLimit

        plans = {}
        for plan in Plan.objects.all():
            plans[plan.slug] = {
                "name": plan.name,
                "price_monthly": str(plan.price_monthly),
                "trial_days": plan.trial_days,
                "is_active": plan.is_active,
                "features": sorted(
                    PlanFeature.objects.filter(plan=plan).values_list("key", flat=True)
                ),
                "limits": dict(
                    PlanLimit.objects.filter(plan=plan).values_list("key", "value")
                ),
            }
        return {
            "plans": plans,
            "new_plans": [s for s in slugs if s not in plans],
            "disabled_channels": list(site.disabled_channels or []),
            "restricted_channels": list(site.restricted_channels or []),
        }

    # ---- undo -------------------------------------------------------------------------------

    @transaction.atomic
    def _undo(self) -> None:
        from apps.billing import api as billing_api
        from apps.billing.models import Plan, PlanFeature, PlanLimit
        from apps.core.channels import invalidate_channel_cache
        from apps.core.models import SiteSettings

        path = _backup_path()
        if not path.exists():
            raise CommandError("Nothing to undo: no backup file.")
        saved = json.loads(path.read_text())
        for slug, data in saved["plans"].items():
            plan = Plan.objects.filter(slug=slug).first()
            if plan is None:
                continue
            plan.name = data["name"]
            plan.price_monthly = Decimal(data["price_monthly"])
            plan.trial_days = data["trial_days"]
            plan.is_active = data["is_active"]
            plan.save()
            PlanFeature.objects.filter(plan=plan).exclude(
                key__in=data["features"]
            ).delete()
            PlanFeature.objects.bulk_create(
                [PlanFeature(plan=plan, key=k) for k in data["features"]],
                ignore_conflicts=True,
            )
            PlanLimit.objects.filter(plan=plan).exclude(key__in=data["limits"]).delete()
            for key, value in data["limits"].items():
                PlanLimit.objects.update_or_create(
                    plan=plan, key=key, defaults={"value": value}
                )
        # Plans the seed created are switched off rather than deleted (a subscription made
        # while trying signup may point at one).
        Plan.objects.filter(slug__in=saved.get("new_plans", [])).update(is_active=False)
        site = SiteSettings.load()
        site.disabled_channels = saved["disabled_channels"]
        site.restricted_channels = saved["restricted_channels"]
        site.save(update_fields=["disabled_channels", "restricted_channels"])
        billing_api.forget()
        invalidate_channel_cache()
        path.unlink()
        self.stdout.write(
            self.style.SUCCESS("Put back the values from before the seed.")
        )
