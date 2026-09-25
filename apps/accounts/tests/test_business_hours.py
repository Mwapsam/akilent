"""Opening hours and the "reply when you're closed" automation.

The service is checked in the business's own timezone (Lusaka is UTC+2, no daylight
saving, so a UTC time and the local time differ by a known two hours), then the settings
page, the workflow branch, and the starter end to end.
"""
from datetime import datetime, timezone as dt_timezone

import pytest
from django.contrib.auth.models import User

from apps.accounts import business_hours as bh
from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import enroll_for_trigger, validate_definition
from apps.contacts.models import Contact

WEEKDAYS = {day: {"open": "08:00", "close": "17:00"} for day in ("mon", "tue", "wed", "thu", "fri")}
# 2026-09-28 is a Monday, 2026-10-03 a Saturday.
MON_9AM_LUSAKA = datetime(2026, 9, 28, 7, 0, tzinfo=dt_timezone.utc)
MON_6PM_LUSAKA = datetime(2026, 9, 28, 16, 0, tzinfo=dt_timezone.utc)
SAT_NOON_LUSAKA = datetime(2026, 10, 3, 10, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


# --- the service --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_hours_set_means_always_open(account):
    assert not bh.is_configured(account)
    assert bh.is_open(account, SAT_NOON_LUSAKA) is True


@pytest.mark.django_db
def test_open_and_closed_in_the_businesss_own_timezone(account):
    bh.save_hours(account, tz="Africa/Lusaka", schedule=WEEKDAYS)
    assert bh.is_open(account, MON_9AM_LUSAKA) is True
    assert bh.is_open(account, MON_6PM_LUSAKA) is False       # after 17:00 local
    assert bh.is_open(account, SAT_NOON_LUSAKA) is False      # a day with no window
    # The same instant is 07:00 UTC: only "open" because the timezone shifts it to 09:00.
    bh.save_hours(account, tz="UTC", schedule=WEEKDAYS)
    assert bh.is_open(account, datetime(2026, 9, 28, 7, 30, tzinfo=dt_timezone.utc)) is False


@pytest.mark.django_db
def test_opening_time_is_included_and_closing_time_is_not(account):
    bh.save_hours(account, tz="UTC", schedule=WEEKDAYS)
    assert bh.is_open(account, datetime(2026, 9, 28, 8, 0, tzinfo=dt_timezone.utc)) is True
    assert bh.is_open(account, datetime(2026, 9, 28, 17, 0, tzinfo=dt_timezone.utc)) is False


@pytest.mark.django_db
def test_saving_again_replaces_rather_than_duplicates_and_empty_clears(account):
    bh.save_hours(account, tz="UTC", schedule=WEEKDAYS)
    bh.save_hours(account, tz="UTC", schedule={"sat": {"open": "10:00", "close": "14:00"}})
    assert list(bh.get_hours(account).schedule) == ["sat"]
    bh.save_hours(account, tz="UTC", schedule={})
    assert not bh.is_configured(account) and bh.is_open(account, MON_6PM_LUSAKA) is True


@pytest.mark.django_db
def test_hours_are_per_business(account):
    other = Account.objects.create(company_name="Other")
    bh.save_hours(account, tz="UTC", schedule=WEEKDAYS)
    assert not bh.is_configured(other)


@pytest.mark.parametrize("schedule, message", [
    ({"mon": {"open": "17:00", "close": "08:00"}}, "closing time must be after"),
    ({"mon": {"open": "09:00", "close": "09:00"}}, "closing time must be after"),
    ({"mon": {"open": "nine", "close": "17:00"}}, "Enter times like 09:00"),
    ({"funday": {"open": "09:00", "close": "17:00"}}, "not a day"),
])
def test_bad_schedules_are_refused_in_plain_words(schedule, message):
    with pytest.raises(bh.HoursError, match=message):
        bh.clean_schedule(schedule)


@pytest.mark.django_db
def test_an_unknown_timezone_is_refused(account):
    with pytest.raises(bh.HoursError):
        bh.save_hours(account, tz="Mars/Olympus", schedule=WEEKDAYS)


@pytest.mark.django_db
def test_a_corrupt_stored_timezone_does_not_break_replies(account):
    from apps.accounts.models import BusinessHours

    BusinessHours.objects.create(account=account, timezone="Not/AZone", schedule=WEEKDAYS)
    assert isinstance(bh.is_open(account, MON_9AM_LUSAKA), bool)


# --- the settings page -----------------------------------------------------------------------------


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def form(**overrides):
    data = {"timezone": "Africa/Lusaka"}
    for day in ("mon", "tue", "wed", "thu", "fri"):
        data.update({f"{day}_open": "on", f"{day}_from": "08:00", f"{day}_to": "17:00"})
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_the_page_offers_weekday_defaults_before_anything_is_saved(owner):
    body = owner.get("/settings/hours/").content.decode()
    assert "Opening hours" in body and 'value="09:00"' in body and 'value="17:00"' in body


@pytest.mark.django_db
def test_an_owner_saves_hours_and_sees_the_current_state(owner, account):
    resp = owner.post("/settings/hours/", form(), follow=True)
    assert "Opening hours saved." in resp.content.decode()
    hours = bh.get_hours(account)
    assert hours.timezone == "Africa/Lusaka" and set(hours.schedule) == {"mon", "tue", "wed", "thu", "fri"}
    assert "Right now you" in owner.get("/settings/hours/").content.decode()


@pytest.mark.django_db
def test_a_bad_submission_saves_nothing(owner, account):
    resp = owner.post("/settings/hours/", form(mon_from="18:00", mon_to="08:00"), follow=True)
    assert "closing time must be after" in resp.content.decode()
    assert bh.get_hours(account) is None


@pytest.mark.django_db
def test_unticking_every_day_clears_the_hours(owner, account):
    owner.post("/settings/hours/", form())
    resp = owner.post("/settings/hours/", {"timezone": "UTC"}, follow=True)
    assert "always open" in resp.content.decode()
    assert not bh.is_configured(account)


@pytest.mark.django_db
def test_a_plain_member_cannot_change_hours(client, account):
    user = User.objects.create_user("member", "member@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    client.force_login(user)
    resp = client.post("/settings/hours/", form(), follow=True)
    assert "Only an owner or admin" in resp.content.decode()
    assert bh.get_hours(account) is None


# --- the workflow branch and the starter ---------------------------------------------------------------


def _branch_definition(**step):
    return {"trigger": {"type": "manual"}, "steps": [
        {"id": "b", "type": "branch", "field": "within_business_hours", "on_true": "stop", "on_false": "tag",
         **step},
        {"id": "tag", "type": "add_tag", "tag": "after-hours", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ]}


def _tagged_after_hours(account, contact, definition, at, monkeypatch):
    from apps.automation import api as automation_api
    from apps.automation.workflow_engine import enroll

    monkeypatch.setattr(bh, "timezone", type("T", (), {"now": staticmethod(lambda: at)}))
    workflow = automation_api.upsert_published_workflow(account, slug="b", name="b", definition=definition)
    enroll(workflow, contact)
    return contact.tags.filter(slug="after-hours").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("step, at, expected", [
    ({}, MON_9AM_LUSAKA, False),                       # open: true branch, no tag
    ({}, MON_6PM_LUSAKA, True),                        # closed: false branch
    ({"value": "true"}, MON_6PM_LUSAKA, True),         # the editor sends text
    # "matches while closed" (value false, or operator ne) sends the closed case down on_true.
    ({"value": False, "on_true": "tag", "on_false": "stop"}, MON_6PM_LUSAKA, True),
    ({"value": False, "on_true": "tag", "on_false": "stop"}, MON_9AM_LUSAKA, False),
    ({"operator": "ne", "on_true": "tag", "on_false": "stop"}, MON_6PM_LUSAKA, True),
    ({"operator": "ne", "on_true": "tag", "on_false": "stop"}, MON_9AM_LUSAKA, False),
])
def test_within_business_hours_branch(account, monkeypatch, step, at, expected):
    bh.save_hours(account, tz="Africa/Lusaka", schedule=WEEKDAYS)
    contact = Contact.objects.create(account=account, phone="+260971234567")
    assert _tagged_after_hours(account, contact, _branch_definition(**step), at, monkeypatch) is expected


@pytest.mark.django_db
def test_the_branch_treats_a_business_without_hours_as_open(account, monkeypatch):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    assert _tagged_after_hours(account, contact, _branch_definition(), SAT_NOON_LUSAKA, monkeypatch) is False


@pytest.mark.django_db
def test_the_after_hours_starter_needs_hours_first(owner, account):
    resp = owner.post("/automations/starters/install/",
                      {"starter": "reply-when-closed", "reply_text": "We're closed."}, follow=True)
    assert "Set your opening hours first" in resp.content.decode()
    assert not Workflow.objects.filter(account=account, slug="reply-when-closed").exists()


@pytest.mark.django_db
def test_the_after_hours_starter_replies_only_when_closed(owner, account, monkeypatch):
    from apps.conversations.models import Conversation
    from apps.whatsapp.models import Conversation as WhatsAppConversation
    from apps.whatsapp.models import OutboundMessage, WhatsAppContact

    bh.save_hours(account, tz="Africa/Lusaka", schedule=WEEKDAYS)
    owner.post("/automations/starters/install/",
               {"starter": "reply-when-closed", "reply_text": "We open at 8am."})
    workflow = Workflow.objects.get(account=account, slug="reply-when-closed")
    assert workflow.status == Workflow.Status.PUBLISHED
    assert not validate_definition(workflow.definition, account=account)

    contact = Contact.objects.create(account=account, phone="+260971234567", source="whatsapp")
    wa = WhatsAppContact.objects.create(account=account, phone_number=contact.phone, contact=contact)
    conversation = Conversation.get_or_create_for_whatsapp(WhatsAppConversation.get_or_open(wa))

    def customer_writes(at):
        monkeypatch.setattr(bh, "timezone", type("T", (), {"now": staticmethod(lambda: at)}))
        return enroll_for_trigger(account.id, "conversation.message_received", contact, context={
            "conversation_id": conversation.public_id, "message": {"body": "Hello?", "type": "text"}})

    customer_writes(MON_9AM_LUSAKA)                           # open: silent
    assert not OutboundMessage.objects.exists()
    WorkflowRun.objects.all().delete()                        # a fresh evening, past the cooldown
    customer_writes(MON_6PM_LUSAKA)
    assert [m.payload["body"] for m in OutboundMessage.objects.all()] == ["We open at 8am."]
    assert customer_writes(SAT_NOON_LUSAKA) == 0              # already told within 8 hours
    assert OutboundMessage.objects.count() == 1
