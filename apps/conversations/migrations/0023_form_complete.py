import secrets

from django.db import migrations, models

import apps.conversations.models


def fill_public_ids(apps, schema_editor):
    ConversationForm = apps.get_model("conversations", "ConversationForm")
    for form in ConversationForm.objects.filter(public_id=""):
        form.public_id = "form_" + secrets.token_hex(12)
        form.save(update_fields=["public_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0022_followup_complete"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversationform",
            name="public_id",
            field=models.CharField(default="", editable=False, max_length=40),
            preserve_default=False,
        ),
        migrations.RunPython(fill_public_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="conversationform",
            name="public_id",
            field=models.CharField(
                default=apps.conversations.models._form_public_id,
                editable=False,
                max_length=40,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="intro_text",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="cta_label",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="screen_title",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="submit_label",
            field=models.CharField(blank=True, default="", max_length=35),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="thanks_text",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="timeout_hours",
            field=models.PositiveIntegerField(default=72),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="lead_action",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Nothing"),
                    ("create", "Track them as interested"),
                    ("qualify", "Track them as interested and mark them qualified"),
                ],
                default="",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="conversationform",
            name="flow_waba_id",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AlterField(
            model_name="conversationform",
            name="flow_status",
            field=models.CharField(
                choices=[
                    ("not_created", "Not created on Meta"),
                    ("draft", "Draft (not published)"),
                    ("publishing", "Publishing to WhatsApp"),
                    ("published", "Published"),
                    ("needs_republish", "Questions changed since publish"),
                    ("error", "Meta rejected the last publish attempt"),
                ],
                default="not_created",
                max_length=20,
            ),
        ),
    ]
