"""Start the operator's provider choice from what was switched on before (Stripe only if it was
on and Flutterwave wasn't), and record which provider each paid subscription is on."""

from django.db import migrations


def forwards(apps, schema_editor):
    PaymentMethod = apps.get_model("billing", "PaymentMethod")
    SiteSettings = apps.get_model("core", "SiteSettings")
    Subscription = apps.get_model("billing", "Subscription")

    enabled = set(
        PaymentMethod.objects.filter(is_enabled=True).values_list("code", flat=True)
    )
    provider = (
        "stripe"
        if "stripe" in enabled and "flutterwave" not in enabled
        else "flutterwave"
    )
    SiteSettings.objects.filter(pk=1).update(billing_provider=provider)

    for code in ("stripe", "flutterwave", "manual"):
        Subscription.objects.filter(payment_method=code, provider="").update(
            provider=code
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0026_providers_payments_credits"),
        ("core", "0016_sitesettings_billing_provider"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
