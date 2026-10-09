"""Give Instagram businesses with no moderation rules the recommended set.

The rules are copied here, not imported, so this migration keeps doing the same
thing if the recommended set changes later. Businesses that already have any
moderation rule are left alone.
"""

from django.db import migrations

SPAM_KEYWORDS = (
    "DM me, message me, contact me, whatsapp me, telegram me, earn money, "
    "make money, easy money, free money, investment opportunity, crypto, bitcoin, "
    "forex, double your money, guaranteed profit, congratulations you won, "
    "claim your prize"
)

RULES = [
    ("Spam and scams", "spam_detection", SPAM_KEYWORDS, "hide", 10),
    ("Abusive language", "toxicity", "", "flag", 20),
    ("Complaints", "complaint", "", "flag", 30),
]


def add_defaults(apps, schema_editor):
    InstagramBusinessAccount = apps.get_model("instagram", "InstagramBusinessAccount")
    ModerationRule = apps.get_model("instagram", "ModerationRule")

    account_ids = set(
        InstagramBusinessAccount.objects.values_list("account_id", flat=True)
    ) - set(ModerationRule.objects.values_list("account_id", flat=True))
    ModerationRule.objects.bulk_create(
        ModerationRule(
            account_id=account_id,
            name=name,
            match_type=match_type,
            keywords=keywords,
            moderation_action=action,
            automation_trigger="none",
            priority=priority,
        )
        for account_id in account_ids
        for name, match_type, keywords, action, priority in RULES
    )


class Migration(migrations.Migration):
    dependencies = [
        ("instagram", "0011_comment_flagged_state"),
    ]

    operations = [
        migrations.RunPython(add_defaults, migrations.RunPython.noop),
    ]
