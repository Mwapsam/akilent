import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0004_lead_deal_attributes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OwnerChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("occurred_at", models.DateTimeField()),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="owner_changes",
                        to="accounts.account",
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="owner_changes_acted",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "deal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="owner_changes",
                        to="crm.deal",
                    ),
                ),
                (
                    "from_owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="owner_changes_from",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="owner_changes",
                        to="crm.lead",
                    ),
                ),
                (
                    "to_owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="owner_changes_to",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            Q(lead__isnull=False, deal__isnull=True)
                            | Q(lead__isnull=True, deal__isnull=False)
                        ),
                        name="owner_change_exactly_one_of_lead_deal",
                    )
                ]
            },
        ),
        migrations.CreateModel(
            name="StageTransition",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("from_value", models.CharField(blank=True, max_length=50)),
                ("to_value", models.CharField(max_length=50)),
                ("occurred_at", models.DateTimeField()),
                (
                    "provenance",
                    models.CharField(
                        choices=[
                            ("observed", "Observed"),
                            ("backfilled_event", "Backfilled from event"),
                            ("estimated", "Estimated"),
                        ],
                        default="observed",
                        max_length=20,
                    ),
                ),
                (
                    "source_event_id",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stage_transitions",
                        to="accounts.account",
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="stage_transitions_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "deal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stage_transitions",
                        to="crm.deal",
                    ),
                ),
                (
                    "lead",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stage_transitions",
                        to="crm.lead",
                    ),
                ),
                (
                    "owner_at_transition",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="stage_transitions_owned",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["lead", "from_value"], name="crm_stageti_lead_id_idx"
                    ),
                    models.Index(
                        fields=["deal", "from_value"], name="crm_stageti_deal_id_idx"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            Q(lead__isnull=False, deal__isnull=True)
                            | Q(lead__isnull=True, deal__isnull=False)
                        ),
                        name="stage_transition_exactly_one_of_lead_deal",
                    ),
                    models.UniqueConstraint(
                        fields=["source_event_id"],
                        condition=Q(source_event_id__gt=""),
                        name="unique_stage_transition_source_event_id",
                    ),
                ],
            },
        ),
    ]
