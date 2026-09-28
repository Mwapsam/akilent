# Generated migration for Phase 3 settings

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_remove_sitesettings_bitrix_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailprovidersettings",
            name="ses_send_rate_limit",
            field=models.PositiveIntegerField(
                default=14,
                help_text="Max sends per second for SES (default 14, AWS SES sandbox default; can go higher with send limit increase)",
            ),
        ),
        migrations.AddField(
            model_name="mailprovidersettings",
            name="smtp_require_tls",
            field=models.BooleanField(
                default=True,
                help_text="Enforce TLS for SMTP relay connections (security best-practice; set False only for dev/testing)",
            ),
        ),
    ]
