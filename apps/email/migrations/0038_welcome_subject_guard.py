"""The seeded "Welcome email" subject read "Welcome to {{ company_name }}, {{ first_name }}!",
which rendered as "Welcome to , !" (company_name was never passed; a contact without a first
name left a dangling comma). Rendering now fills company_name; this updates the subject of
every seeded copy the owner hasn't edited so the name only appears when there is one."""

from django.db import migrations

OLD = "Welcome to {{ company_name }}, {{ first_name }}!"
NEW = "Welcome to {{ company_name }}{% if first_name %}, {{ first_name }}{% endif %}!"


def forwards(apps, schema_editor):
    EmailTemplate = apps.get_model("email_service", "EmailTemplate")
    EmailTemplate.objects.filter(slug="welcome-email", subject=OLD).update(subject=NEW)


def backwards(apps, schema_editor):
    EmailTemplate = apps.get_model("email_service", "EmailTemplate")
    EmailTemplate.objects.filter(slug="welcome-email", subject=NEW).update(subject=OLD)


class Migration(migrations.Migration):
    dependencies = [("email_service", "0037_mail_from_dns_diagnostics")]

    operations = [migrations.RunPython(forwards, backwards)]
