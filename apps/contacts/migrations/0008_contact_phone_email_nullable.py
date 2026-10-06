"""
Ensure phone and email columns are nullable in the DB.

The Django model already declares both as null=True, but the production schema
may have them as NOT NULL if an older version of the initial migration was used
to set up the database. This migration uses RunSQL to explicitly drop the NOT
NULL constraint so that channel-only contacts (e.g. Instagram) can be created
without an email address or phone number.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0007_contact_intelligence"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE contacts_contact
                    ALTER COLUMN phone  DROP NOT NULL,
                    ALTER COLUMN email  DROP NOT NULL;
            """,
            reverse_sql="""
                UPDATE contacts_contact SET phone = '' WHERE phone IS NULL;
                UPDATE contacts_contact SET email = '' WHERE email IS NULL;
                ALTER TABLE contacts_contact
                    ALTER COLUMN phone  SET NOT NULL,
                    ALTER COLUMN email  SET NOT NULL;
            """,
        ),
    ]
