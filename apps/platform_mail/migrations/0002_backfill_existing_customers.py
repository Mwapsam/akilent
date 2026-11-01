"""Subscribe current Akilent users to product updates (soft opt-in for existing customers).

Decision 2026-11-01: existing customers are included by default; every email carries a
one-click unsubscribe and the profile page has a toggle. New signups only join by ticking
the (unticked) signup checkbox. Only active users who belong to a workspace are included.
"""

import secrets

from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Membership = apps.get_model("accounts", "Membership")
    Subscriber = apps.get_model("platform_mail", "MarketingSubscriber")

    now = timezone.now()
    known = set(Subscriber.objects.values_list("email", flat=True))
    member_ids = Membership.objects.values("user_id")
    rows = []
    for user in User.objects.filter(is_active=True, pk__in=member_ids).exclude(
        email=""
    ):
        email = user.email.strip().lower()
        if email in known:
            continue
        known.add(email)
        name = f"{user.first_name} {user.last_name}".strip()
        rows.append(
            Subscriber(
                email=email,
                name=name[:200],
                user_id=user.pk,
                status="subscribed",
                source="existing_customer",
                consent_evidence={
                    "basis": "soft opt-in: existing Akilent customer",
                    "backfilled_at": now.isoformat(),
                },
                consented_at=now,
                token=secrets.token_urlsafe(32),
            )
        )
    Subscriber.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("platform_mail", "0001_initial"),
        ("accounts", "0025_drop_service_type"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
