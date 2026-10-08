"""Clear Page ids stored on Instagram Login accounts.

Those accounts have no Facebook Page; a Page id on one (entered by hand) made
webhooks for a different Instagram account resolve to it.
"""

from django.db import migrations


def clear_page_ids(apps, schema_editor):
    InstagramBusinessAccount = apps.get_model("instagram", "InstagramBusinessAccount")
    for iba in InstagramBusinessAccount.objects.exclude(page_id=""):
        if (iba.access_token or "").startswith("IG"):
            iba.page_id = ""
            iba.save(update_fields=["page_id"])


class Migration(migrations.Migration):
    dependencies = [("instagram", "0009_outboundmessage_media")]

    operations = [migrations.RunPython(clear_page_ids, migrations.RunPython.noop)]
