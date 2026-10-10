from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("contacts", "0008_contact_phone_email_nullable"),
    ]

    operations = [
        # Add entity field (all existing rows become "contact").
        migrations.AddField(
            model_name="customattributedef",
            name="entity",
            field=models.CharField(
                choices=[
                    ("contact", "Contact"),
                    ("lead", "Lead"),
                    ("deal", "Deal"),
                ],
                default="contact",
                max_length=10,
            ),
        ),
        # Add archived_at.
        migrations.AddField(
            model_name="customattributedef",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Add options for choice-type attributes.
        migrations.AddField(
            model_name="customattributedef",
            name="options",
            field=models.JSONField(blank=True, default=list),
        ),
        # Add "choice" to the type choices (data-only; existing rows unchanged).
        migrations.AlterField(
            model_name="customattributedef",
            name="type",
            field=models.CharField(
                choices=[
                    ("string", "Text"),
                    ("number", "Number"),
                    ("boolean", "Yes / no"),
                    ("date", "Date"),
                    ("choice", "Choice"),
                ],
                default="string",
                max_length=10,
            ),
        ),
        # Drop old (account, key) constraint and replace with (account, entity, key).
        migrations.RemoveConstraint(
            model_name="customattributedef",
            name="uniq_custom_attr_account_key",
        ),
        migrations.AddConstraint(
            model_name="customattributedef",
            constraint=models.UniqueConstraint(
                fields=["account", "entity", "key"],
                name="uniq_custom_attr_account_entity_key",
            ),
        ),
        # Update ordering to include entity.
        migrations.AlterModelOptions(
            name="customattributedef",
            options={"ordering": ["entity", "key"]},
        ),
    ]
