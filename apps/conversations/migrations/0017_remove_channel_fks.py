"""Remove per-channel OneToOne FKs from Conversation; ChannelConversation is now the sole anchor.

Stage 2 of the spine refactor. The ChannelConversation join table (added in
0016 and backfilled) is the authoritative lookup. All application code was
updated to go through ChannelConversation before this migration runs.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("conversations", "0016_channel_conversation"),
        ("instagram", "0001_initial"),
        ("whatsapp", "0020_token_expiry"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="conversation",
            name="unique_generic_conversation_per_whatsapp_conversation",
        ),
        migrations.RemoveConstraint(
            model_name="conversation",
            name="unique_generic_conversation_per_instagram_conversation",
        ),
        migrations.RemoveField(
            model_name="conversation",
            name="whatsapp_conversation",
        ),
        migrations.RemoveField(
            model_name="conversation",
            name="instagram_conversation",
        ),
    ]
