import django.db.models.functions.text
from django.db import migrations, models


def refuse_if_case_duplicates(apps, schema_editor):
    """Stop with a clear instruction instead of a database error mid-deploy."""
    from django.db.models import Count
    from django.db.models.functions import Lower

    Contact = apps.get_model("contacts", "Contact")
    clashes = (
        Contact.objects.filter(email__isnull=False)
        .annotate(e=Lower("email"))
        .values("account_id", "e")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
    )
    if clashes:
        raise RuntimeError(
            f"{clashes} email address(es) are used by more than one customer when "
            "upper/lower case is ignored. Run `python manage.py check_contact_emails "
            "--merge` to merge them, then migrate again."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0020_passkey_registration_challenge"),
        ("contacts", "0009_customattributedef_entity_archived"),
    ]

    operations = [
        migrations.RunPython(refuse_if_case_duplicates, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="contact",
            name="unique_account_contact_email",
        ),
        migrations.AddConstraint(
            model_name="contact",
            constraint=models.UniqueConstraint(
                models.F("account"),
                django.db.models.functions.text.Lower("email"),
                condition=models.Q(("email__isnull", False)),
                name="unique_account_contact_email_ci",
            ),
        ),
    ]
