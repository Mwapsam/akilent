"""Make Contact.email optional and enforce the canonical-identity invariant.

Contact becomes creatable from a phone-only (e.g. WhatsApp-first) interaction:
email is no longer required, and uniqueness on both (account, email) and
(account, phone) is scoped to non-null values only, so any number of
phone-only or email-only Contacts can coexist per account.

Duplicate detection runs before the new constraints are added. It never
merges or deletes data automatically — an actual duplicate is a data problem
that must be resolved by hand before this migration can proceed.
"""
from __future__ import annotations

from django.db import migrations, models


def _normalize_blanks_to_null(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    Contact.objects.filter(email="").update(email=None)
    Contact.objects.filter(phone="").update(phone=None)


def _reverse_normalize_blanks_to_null(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    Contact.objects.filter(email__isnull=True).update(email="")
    Contact.objects.filter(phone__isnull=True).update(phone="")


def _check_no_duplicates(apps, schema_editor):
    from django.db.models import Count

    Contact = apps.get_model("contacts", "Contact")

    dup_emails = list(
        Contact.objects.filter(email__isnull=False)
        .values("account_id", "email")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    dup_phones = list(
        Contact.objects.filter(phone__isnull=False)
        .values("account_id", "phone")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    if dup_emails or dup_phones:
        lines = ["Cannot add canonical-identity constraints: duplicate Contacts found."]
        for row in dup_emails:
            lines.append(f"  duplicate email: account={row['account_id']} email={row['email']!r} ({row['n']} contacts)")
        for row in dup_phones:
            lines.append(f"  duplicate phone: account={row['account_id']} phone={row['phone']!r} ({row['n']} contacts)")
        lines.append("Resolve these Contacts manually (merge or clear the field) before re-running this migration.")
        raise RuntimeError("\n".join(lines))


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("contacts", "0002_contact_phone_contact_contacts_co_account_dde65c_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contact",
            name="email",
            field=models.EmailField(blank=True, default=None, max_length=254, null=True),
        ),
        migrations.RunPython(_normalize_blanks_to_null, _reverse_normalize_blanks_to_null),
        migrations.RunPython(_check_no_duplicates, _noop_reverse),
        migrations.RemoveConstraint(
            model_name="contact",
            name="uniq_contact_account_email",
        ),
        migrations.AddConstraint(
            model_name="contact",
            constraint=models.UniqueConstraint(
                condition=models.Q(("email__isnull", False)),
                fields=("account", "email"),
                name="unique_account_contact_email",
            ),
        ),
        migrations.AddConstraint(
            model_name="contact",
            constraint=models.UniqueConstraint(
                condition=models.Q(("phone__isnull", False)),
                fields=("account", "phone"),
                name="unique_account_contact_phone",
            ),
        ),
    ]
