"""Mark every account that exists before verify-by-code ships as email-verified.

From this release an unverified owner can't send email or WhatsApp campaigns. Accounts made
before it were never asked to verify for that, so none of them (the pilots) loses sending
on deploy. Accounts created afterwards verify with the code from signup."""

from django.db import migrations
from django.utils import timezone


def verify_existing(apps, schema_editor):
    Account = apps.get_model("accounts", "Account")
    Account.objects.filter(email_verified=False).update(
        email_verified=True, email_verified_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0028_emaillogincode_verify_purpose")]

    operations = [migrations.RunPython(verify_existing, migrations.RunPython.noop)]
