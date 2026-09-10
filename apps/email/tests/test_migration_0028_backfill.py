"""Regression test for the 0028 version-number backfill.

Prod had multiple EmailTemplateVersion rows per template, all implicitly
number=0; adding the (template, number) unique index failed until 0028 grew a
RunPython backfill. This drives the migration backwards to 0027 and forwards
again with duplicate rows present.
"""
import pytest
from django.db.migrations.executor import MigrationExecutor
from django.db import connection

_APP = "email_service"
_BEFORE = "0027_templatecomponent"
_AFTER = "0028_alter_emailtemplateversion_options_and_more"


@pytest.mark.django_db(transaction=True)
def test_backfill_runs_forward_with_duplicate_unnumbered_rows():
    executor = MigrationExecutor(connection)
    executor.migrate([(_APP, _BEFORE)])
    executor.loader.build_graph()

    old_apps = executor.loader.project_state([(_APP, _BEFORE)]).apps
    Account = old_apps.get_model("accounts", "Account")
    Template = old_apps.get_model(_APP, "EmailTemplate")
    Version = old_apps.get_model(_APP, "EmailTemplateVersion")

    acc = Account.objects.create(company_name="Acme")
    t = Template.objects.create(account=acc, name="T", slug="t", subject="s")
    for s in ("a", "b", "c"):
        Version.objects.create(template=t, subject=s)

    executor = MigrationExecutor(connection)
    executor.migrate([(_APP, _AFTER)])

    new_apps = executor.loader.project_state([(_APP, _AFTER)]).apps
    NewVersion = new_apps.get_model(_APP, "EmailTemplateVersion")
    rows = list(
        NewVersion.objects.filter(template_id=t.pk)
        .order_by("created_at", "pk")
        .values_list("number", "is_active")
    )
    assert rows == [(1, False), (2, False), (3, True)]

    # leave the DB migrated forward for the rest of the suite
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
