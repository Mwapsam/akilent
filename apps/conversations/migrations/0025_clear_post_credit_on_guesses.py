"""Remove post and insight credit from recent-conversation guesses written before 2026-10-25.

Until then ``attribution.record()`` copied the conversation's entry point (and so its post,
channel and campaign) and the insight whose DM opened it, even when the conversation was
only guessed (method RECENT_CONVERSATION), which let a guess read as a tracked sale.
The writer is fixed; this clears the copied fields on the rows already written, so nothing that filters on
``source_post`` can count them.

Attributions are otherwise immutable (``save()`` refuses changes); this is a one-off data
correction through ``update()``. The conversation, method, subject and time are kept, so
the guess itself stays on record. It cannot be reversed: the cleared credit was never
earned.
"""

from django.db import migrations
from django.db.models import Q


def clear(apps, schema_editor):
    Attribution = apps.get_model("conversations", "ConversationAttribution")
    rows = Attribution.objects.filter(method="recent_conversation").filter(
        Q(source_post__isnull=False)
        | Q(source_variant__isnull=False)
        | Q(source_campaign__isnull=False)
        | Q(entry_point__isnull=False)
        | Q(originated_from__isnull=False)
    )
    cleared = rows.update(
        source_post=None,
        source_variant=None,
        source_campaign=None,
        entry_point=None,
        originated_from=None,
    )
    if cleared:
        print(f"\n  Cleared post/insight credit from {cleared} guessed attribution(s).")


class Migration(migrations.Migration):
    dependencies = [("conversations", "0024_automation_pause")]

    operations = [migrations.RunPython(clear, migrations.RunPython.noop)]
