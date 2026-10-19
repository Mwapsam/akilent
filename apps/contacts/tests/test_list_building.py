"""Filling a customer list: paste, CSV, selection — every path ends with people a
WhatsApp campaign can actually reach."""

from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account
from apps.contacts import list_building as lb
from apps.contacts.models import Contact, ContactImport, ContactList
from apps.whatsapp.models import WhatsAppContact

_Opt = WhatsAppContact.OptInStatus


@pytest.fixture
def ctx(db):
    account = Account.objects.create(company_name="Mwamba Kitchen")
    user = User.objects.create_user("owner", "o@example.com", "pw")
    return SimpleNamespace(
        account=account,
        user=user,
        clist=ContactList.objects.create(account=account, name="VIPs"),
    )


# --- parsing ---------------------------------------------------------------


def test_parse_pasted_accepts_numbers_with_optional_names():
    rows = lb.parse_pasted(
        "+260971234567, Ada Mwape\n\n0977000111\t Bwalya\n+260955000222"
    )
    assert [(r.line, r.phone, r.first_name, r.last_name) for r in rows] == [
        (1, "+260971234567", "Ada", "Mwape"),
        (3, "0977000111", "Bwalya", ""),
        (4, "+260955000222", "", ""),
    ]


def test_parse_pasted_limits():
    with pytest.raises(lb.ListBuildError):
        lb.parse_pasted("   \n  ")
    with pytest.raises(lb.ListBuildError, match="up to 1,000"):
        lb.parse_pasted("\n".join(f"+26097100{i:04d}" for i in range(1001)))


def test_parse_csv_detects_phone_first_columns():
    data = b"Name,Mobile,Tags\nAda Mwape,+260971234567,vip; regular\n,,\nBwalya,0977000111,\n"
    rows = lb.parse_csv(data)
    assert [(r.line, r.phone, r.first_name, r.last_name, r.tags) for r in rows] == [
        (2, "+260971234567", "Ada", "Mwape", ["vip", "regular"]),
        (4, "0977000111", "Bwalya", "", []),
    ]


def test_parse_csv_without_a_phone_column_says_what_to_do():
    with pytest.raises(lb.ListBuildError, match="phone number column"):
        lb.parse_csv(b"email,name\na@b.co,Ada\n")


def test_parse_csv_size_limit():
    with pytest.raises(lb.ListBuildError, match="2 MB"):
        lb.parse_csv(b"phone\n" + b"+260971234567\n" * 200_000)


def test_semicolon_csv_from_excel_is_read():
    rows = lb.parse_csv(b"phone;first name\n+260971234567;Ada\n")
    assert (rows[0].phone, rows[0].first_name) == ("+260971234567", "Ada")


# --- adding ------------------------------------------------------------------


def test_adds_new_and_existing_people_and_reports_bad_lines(ctx):
    Contact.objects.create(account=ctx.account, phone="+260971234567", first_name="Ada")
    rows = lb.parse_pasted(
        "+260971234567, Someone Else\n0977000111, Bwalya\nnope\n0977000111"
    )

    result = lb.add_people_to_list(ctx.account, ctx.clist, rows)

    assert (result.added, result.created, result.already_on_list) == (2, 1, 0)
    assert result.invalid == [
        (3, "nope", "Not a valid number — include the country code")
    ]
    assert ctx.clist.contacts.count() == 2
    # An existing customer is never renamed by a list upload.
    assert Contact.objects.get(phone="+260971234567").first_name == "Ada"
    assert Contact.objects.get(phone="+260977000111").first_name == "Bwalya"
    assert ContactImport.objects.filter(account=ctx.account).count() == 1


def test_everyone_added_gets_a_whatsapp_identity(ctx):
    result = lb.add_people_to_list(
        ctx.account, ctx.clist, lb.parse_pasted("0977000111\n0955000222")
    )

    assert result.added == 2
    identities = WhatsAppContact.objects.filter(account=ctx.account)
    assert identities.count() == 2
    assert {wc.contact_id for wc in identities} == set(
        ctx.clist.contacts.values_list("pk", flat=True)
    )
    assert set(identities.values_list("opt_in_status", flat=True)) == {_Opt.UNKNOWN}


def test_adding_again_reports_already_on_list(ctx):
    rows = lambda: lb.parse_pasted("0977000111")  # noqa: E731
    lb.add_people_to_list(ctx.account, ctx.clist, rows())
    result = lb.add_people_to_list(ctx.account, ctx.clist, rows())
    assert (result.added, result.already_on_list) == (0, 1)


def test_attestation_opts_people_in_but_never_overrides_stop(ctx):
    stopped = Contact.objects.create(account=ctx.account, phone="+260955000222")
    WhatsAppContact.objects.create(
        account=ctx.account,
        phone_number=stopped.phone,
        contact=stopped,
        opt_in_status=_Opt.OPTED_OUT,
    )

    result = lb.add_people_to_list(
        ctx.account,
        ctx.clist,
        lb.parse_pasted("0977000111\n0955000222"),
        attested_by=ctx.user,
    )

    assert (result.opted_in, result.opted_out_unchanged) == (1, 1)
    new = WhatsAppContact.objects.get(phone_number="+260977000111")
    assert new.opt_in_status == _Opt.OPTED_IN
    assert new.opt_in_source == f"owner_attestation:user:{ctx.user.pk}"
    assert WhatsAppContact.objects.get(contact=stopped).opt_in_status == _Opt.OPTED_OUT


def test_without_attestation_opt_in_is_left_alone(ctx):
    lb.add_people_to_list(ctx.account, ctx.clist, lb.parse_pasted("0977000111"))
    assert WhatsAppContact.objects.get().opt_in_status == _Opt.UNKNOWN


def test_plan_customer_limit_stops_new_customers_but_not_existing(ctx):
    Contact.objects.create(account=ctx.account, phone="+260971234567")
    with mock.patch("apps.billing.api.limit", return_value=1):
        result = lb.add_people_to_list(
            ctx.account, ctx.clist, lb.parse_pasted("+260971234567\n0977000111")
        )
    assert (result.added, result.over_limit) == (1, 1)
    assert not Contact.objects.filter(phone="+260977000111").exists()


def test_never_touches_another_accounts_customers(ctx):
    other = Account.objects.create(company_name="Other")
    theirs = Contact.objects.create(
        account=other, phone="+260977000111", first_name="Theirs"
    )

    lb.add_people_to_list(ctx.account, ctx.clist, lb.parse_pasted("0977000111, Mine"))

    mine = Contact.objects.get(account=ctx.account, phone="+260977000111")
    assert mine.pk != theirs.pk
    assert list(ctx.clist.contacts.all()) == [mine]


def test_add_existing_customers_by_queryset(ctx):
    a = Contact.objects.create(account=ctx.account, phone="+260977000111")
    b = Contact.objects.create(account=ctx.account, email="b@example.com")
    other = Contact.objects.create(
        account=Account.objects.create(company_name="Other"), phone="+260977000111"
    )
    WhatsAppContact.objects.filter(contact=a).delete()

    result = lb.add_contacts_to_list(
        ctx.account, ctx.clist, Contact.objects.filter(pk__in=[a.pk, b.pk, other.pk])
    )

    assert result.added == 2
    assert set(ctx.clist.contacts.all()) == {a, b}
    assert WhatsAppContact.objects.filter(contact=a).exists()


def test_tags_from_csv_are_applied(ctx):
    rows = lb.parse_csv(b"phone,tags\n0977000111,vip\n")
    lb.add_people_to_list(ctx.account, ctx.clist, rows)
    assert list(Contact.objects.get().tags.values_list("name", flat=True)) == ["vip"]


def test_invalid_rows_csv_neutralises_formulas():
    result = lb.ListAddResult(
        invalid=[(3, "=HYPERLINK(1)", "bad"), (4, "+2609", "bad")]
    )
    out = lb.invalid_rows_csv(result)
    assert "'=HYPERLINK(1)" in out
    assert "+2609" in out and "'+2609" not in out
