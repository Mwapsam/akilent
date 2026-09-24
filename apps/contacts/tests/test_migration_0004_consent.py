"""The 0004 consent backfill must not invent consent it cannot evidence.

Existing rows predate consent tracking. Marking them OPTED_IN would be a claim
we couldn't back if AWS (or a recipient) asked, so they land as UNKNOWN. Only
already-unsubscribed rows carry over unambiguously.
"""
import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

_APP = "contacts"
_BEFORE = "0003_contact_optional_email_and_phone_identity"
_AFTER = "0004_contact_consent"


@pytest.mark.django_db(transaction=True)
def test_backfill_records_unknown_not_opted_in():
    executor = MigrationExecutor(connection)
    executor.migrate([(_APP, _BEFORE)])
    executor.loader.build_graph()

    old_apps = executor.loader.project_state([(_APP, _BEFORE)]).apps
    Account = old_apps.get_model("accounts", "Account")
    Contact = old_apps.get_model(_APP, "Contact")

    acc = Account.objects.create(company_name="Acme")
    Contact.objects.create(account=acc, email="sub@x.com", status="subscribed")
    Contact.objects.create(account=acc, email="gone@x.com", status="unsubscribed")
    Contact.objects.create(account=acc, email="hard@x.com", status="bounced")

    executor = MigrationExecutor(connection)
    executor.migrate([(_APP, _AFTER)])

    new_apps = executor.loader.project_state([(_APP, _AFTER)]).apps
    NewContact = new_apps.get_model(_APP, "Contact")

    sub = NewContact.objects.get(email="sub@x.com")
    assert sub.consent_status == "unknown"
    assert sub.consent_source == "pre_consent_backfill"
    assert sub.consent_at is None

    gone = NewContact.objects.get(email="gone@x.com")
    assert gone.consent_status == "opted_out"
    assert gone.opt_out_reason == "pre_consent_backfill"

    # A bounce is a delivery failure, not a consent withdrawal.
    hard = NewContact.objects.get(email="hard@x.com")
    assert hard.consent_status == "unknown"

    # Nothing anywhere was granted consent it can't prove.
    assert not NewContact.objects.filter(consent_status="opted_in").exists()


@pytest.mark.django_db(transaction=True)
def test_migration_reverses_cleanly():
    executor = MigrationExecutor(connection)
    executor.migrate([(_APP, _AFTER)])
    executor.loader.build_graph()
    executor.migrate([(_APP, _BEFORE)])
    executor.loader.build_graph()
    # Leave the test DB at the latest state for whatever runs next.
    MigrationExecutor(connection).migrate([(_APP, _AFTER)])
