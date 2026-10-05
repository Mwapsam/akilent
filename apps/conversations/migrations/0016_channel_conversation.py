"""Add ChannelConversation join table; backfill from existing per-channel FKs.

Stage 1 of the spine refactor. The old whatsapp_conversation and
instagram_conversation FKs are kept alive during this migration so all
existing code keeps working unchanged. A subsequent migration will remove
them once all call sites have been updated.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_channel_conversations(apps, schema_editor):
    """Populate ChannelConversation from the existing per-channel FK columns."""
    Conversation = apps.get_model("conversations", "Conversation")
    ChannelConversation = apps.get_model("conversations", "ChannelConversation")

    rows = []
    for conv in Conversation.objects.exclude(
        whatsapp_conversation__isnull=True
    ).iterator():
        rows.append(
            ChannelConversation(
                conversation=conv,
                channel="whatsapp",
                object_id=conv.whatsapp_conversation_id,
            )
        )
    for conv in Conversation.objects.exclude(
        instagram_conversation__isnull=True
    ).iterator():
        rows.append(
            ChannelConversation(
                conversation=conv,
                channel="instagram",
                object_id=conv.instagram_conversation_id,
            )
        )

    if rows:
        ChannelConversation.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0015_conversation_instagram_conversation_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChannelConversation",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False
                    ),
                ),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="channel_conversations",
                        to="conversations.conversation",
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("whatsapp", "WhatsApp"),
                            ("email", "Email"),
                            ("sms", "SMS"),
                            ("website_chat", "Website Chat"),
                            ("instagram", "Instagram"),
                        ],
                        max_length=20,
                    ),
                ),
                ("object_id", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="channelconversation",
            constraint=models.UniqueConstraint(
                fields=["channel", "object_id"],
                name="unique_channel_conversation_binding",
            ),
        ),
        migrations.AddIndex(
            model_name="channelconversation",
            index=models.Index(
                fields=["channel", "object_id"],
                name="conversations_channelconv_channel_objid",
            ),
        ),
        migrations.RunPython(backfill_channel_conversations, migrations.RunPython.noop),
    ]
