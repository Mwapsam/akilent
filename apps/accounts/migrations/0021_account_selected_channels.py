"""Record the channels each business chose, which onboarding now reads.

Existing accounts chose a service (Email / WhatsApp / both) at signup, so their channels are
that service's channels, plus Instagram or a Facebook Page they have already connected. The
service map is written out rather than imported, so this migration keeps meaning what it
meant when it ran. Not reversed: the field is simply dropped going back.
"""

from django.db import migrations, models

SERVICE_CHANNELS = {
    "email": ["email"],
    "whatsapp": ["whatsapp"],
    "both": ["email", "whatsapp"],
}


def backfill(apps, schema_editor):
    Account = apps.get_model("accounts", "Account")
    InstagramBusinessAccount = apps.get_model("instagram", "InstagramBusinessAccount")
    FacebookPageConnection = apps.get_model("publishing", "FacebookPageConnection")

    with_instagram = set(
        InstagramBusinessAccount.objects.filter(is_active=True).values_list(
            "account_id", flat=True
        )
    )
    with_facebook = set(
        FacebookPageConnection.objects.exclude(page_id="").values_list(
            "account_id", flat=True
        )
    )
    for account in Account.objects.filter(selected_channels=[]).only(
        "pk", "selected_services"
    ):
        channels = set(SERVICE_CHANNELS.get(account.selected_services, []))
        if account.pk in with_instagram:
            channels.add("instagram")
        if account.pk in with_facebook:
            channels.add("facebook")
        if channels:
            Account.objects.filter(pk=account.pk).update(
                selected_channels=sorted(channels)
            )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0020_passkey_registration_challenge"),
        ("instagram", "0019_post_attribution"),
        ("publishing", "0018_post_client_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="selected_channels",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="account",
            name="onboarding_state",
            field=models.CharField(
                choices=[
                    ("account_created", "Account created"),
                    ("whatsapp_setup", "WhatsApp setup"),
                    ("instagram_setup", "Instagram setup"),
                    ("facebook_setup", "Facebook Page setup"),
                    ("domain_setup", "Domain setup"),
                    ("completed", "Completed"),
                ],
                default="completed",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
