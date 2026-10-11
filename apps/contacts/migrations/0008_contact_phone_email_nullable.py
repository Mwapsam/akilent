"""
Ensure phone and email columns are nullable in the DB.

The Django model already declares both as null=True. This migration uses
AlterField (cross-database compatible) instead of raw SQL so it works on
both SQLite (tests) and PostgreSQL (production).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0007_contact_intelligence"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contact",
            name="phone",
            field=models.CharField(max_length=20, blank=True, null=True, default=None),
        ),
        migrations.AlterField(
            model_name="contact",
            name="email",
            field=models.EmailField(blank=True, null=True, default=None),
        ),
    ]
