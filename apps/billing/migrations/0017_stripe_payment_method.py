from django.db import migrations


def add_stripe_payment_method(apps, schema_editor):
    PaymentMethod = apps.get_model("billing", "PaymentMethod")
    if not PaymentMethod.objects.filter(code="stripe").exists():
        next_sort_order = (
            PaymentMethod.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        ) + 1
        PaymentMethod.objects.create(
            code="stripe",
            name="Card (Stripe)",
            is_enabled=False,
            sort_order=next_sort_order,
        )


def remove_stripe_payment_method(apps, schema_editor):
    PaymentMethod = apps.get_model("billing", "PaymentMethod")
    PaymentMethod.objects.filter(code="stripe").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0016_subscription_billing_period_and_more'),
    ]

    operations = [
        migrations.RunPython(add_stripe_payment_method, remove_stripe_payment_method),
    ]
