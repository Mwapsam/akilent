from django.db import migrations


def seed_payment_methods(apps, schema_editor):
    PaymentMethod = apps.get_model("billing", "PaymentMethod")
    PaymentMethod.objects.get_or_create(
        code="flutterwave",
        defaults={"name": "Card (Flutterwave)", "is_enabled": True, "sort_order": 0},
    )
    PaymentMethod.objects.get_or_create(
        code="manual",
        defaults={"name": "Bank transfer", "is_enabled": False, "sort_order": 1},
    )


def remove_payment_methods(apps, schema_editor):
    PaymentMethod = apps.get_model("billing", "PaymentMethod")
    PaymentMethod.objects.filter(code__in=["flutterwave", "manual"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0009_paymentmethod_subscription_payment_method_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_payment_methods, remove_payment_methods),
    ]
