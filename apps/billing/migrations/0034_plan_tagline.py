"""Plan.tagline: who the plan is for, one line on the landing page's pricing card.

The seeded plans get a starting line; an operator edits it in Operator Console → Plans. A plan
that already has one keeps it.
"""

from django.db import migrations, models

TAGLINES = {
    "trial": "Try it with your own customers before you choose.",
    "starter": "For a business bringing its customer chats into one place.",
    "professional": "For teams answering customers every day.",
    "business": "For busy teams with a high volume of chats.",
}


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    for slug, line in TAGLINES.items():
        Plan.objects.filter(slug=slug, tagline="").update(tagline=line)


class Migration(migrations.Migration):
    dependencies = [("billing", "0033_plans_sell_every_channel")]
    operations = [
        migrations.AddField(
            model_name="plan",
            name="tagline",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
