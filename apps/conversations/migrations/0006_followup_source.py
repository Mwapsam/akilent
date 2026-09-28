from django.db import migrations, models

# Kept here rather than imported: a migration must not change if recovery's wording does.
RECOVERY_NOTE = "No reply sent - customer has been waiting"


def mark_recovery_followups(apps, schema_editor):
    """Follow-ups made by missed-conversation recovery before ``source`` existed."""
    FollowUp = apps.get_model("conversations", "FollowUp")
    FollowUp.objects.filter(note=RECOVERY_NOTE, created_by__isnull=True).update(
        source="missed"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0005_benchmark"),
    ]

    operations = [
        migrations.AddField(
            model_name="followup",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "Set by a person"),
                    ("missed", "Missed conversation"),
                ],
                default="manual",
                max_length=12,
            ),
        ),
        migrations.AddIndex(
            model_name="followup",
            index=models.Index(
                fields=["account", "source", "created_at"],
                name="followup_account_source_idx",
            ),
        ),
        migrations.RunPython(mark_recovery_followups, migrations.RunPython.noop),
    ]
