"""Expire the "purchase_intent" proposals Instagram DMs used to create.

They were created PENDING with no model run behind them, so the inbox showed an AI
card that never finished. Instagram DMs now open a lead the same way WhatsApp does
(``capture_opportunity``). Comment-originated ones (no conversation) are left alone.
"""

from django.db import migrations
from django.utils import timezone


def expire_dm_purchase_intent(apps, schema_editor):
    AIProposal = apps.get_model("ai", "AIProposal")
    AIProposal.objects.filter(
        action="purchase_intent", status="pending", conversation__isnull=False
    ).update(status="expired", expired_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0006_knowledgebaseentry"),
    ]

    operations = [
        migrations.RunPython(expire_dm_purchase_intent, migrations.RunPython.noop),
    ]
