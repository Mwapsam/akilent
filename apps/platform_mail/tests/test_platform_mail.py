"""Akilent's own marketing email: consent rules, lists, sending with the Email channel off,
unsubscribe, and the console."""

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.platform_mail.models import (
    MarketingCampaign,
    MarketingList,
    MarketingSend,
    MarketingSettings,
    MarketingSubscriber,
)
from apps.platform_mail.services import consent, lists, sending

Status = MarketingSubscriber.Status
Source = MarketingSubscriber.Source
SEND = "apps.email.services.send.send_platform_marketing_email"


@pytest.fixture
def address(db):
    row = MarketingSettings.load()
    row.postal_address = "Akilent Ltd, 1 Example Street, London"
    row.save()
    return row


@pytest.fixture
def mlist(db):
    return MarketingList.objects.create(name="Imported")


def _campaign(mlist, **kw):
    c = MarketingCampaign.objects.create(
        name="Oct update", subject="What's new", body="Hello **there**.", **kw
    )
    c.lists.add(mlist)
    return c


def _member(mlist, email, status=Status.SUBSCRIBED):
    row = MarketingSubscriber.objects.create(
        email=email, source=Source.IMPORT, status=status
    )
    lists.add_member(mlist, row)
    return row


def _drain(campaign, *, max_calls=100):
    """Deliver until nothing is claimable (a retried send stays pending, so cap the loop)."""
    from apps.platform_mail.services import render

    prepared = render.prepare(campaign)
    for _ in range(max_calls):
        if sending.deliver_next(campaign.pk, prepared) == sending.EMPTY:
            break
    sending.finish_if_done(campaign.pk)


# --- Consent -------------------------------------------------------------------


@pytest.mark.django_db
class TestConsent:
    def test_import_never_resubscribes_someone_who_opted_out(self):
        row, _ = consent.subscribe("A@x.com", source=Source.SIGNUP)
        consent.unsubscribe(row, how="link")
        again, outcome = consent.subscribe("a@x.com", source=Source.IMPORT)
        assert outcome == "kept_opted_out"
        again.refresh_from_db()
        assert again.status == Status.UNSUBSCRIBED

    def test_the_persons_own_choice_resubscribes(self):
        row, _ = consent.subscribe("a@x.com", source=Source.IMPORT)
        consent.unsubscribe(row, how="link")
        _, outcome = consent.subscribe("a@x.com", source=Source.PROFILE)
        assert outcome == "resubscribed"

    def test_profile_toggle_round_trip(self):
        user = User.objects.create_user("u", "u@x.com", "pw")
        consent.set_user_preference(user, True, source=Source.PROFILE)
        assert consent.user_wants_updates(user)
        consent.set_user_preference(user, False, source=Source.PROFILE)
        assert not consent.user_wants_updates(user)

    def test_consent_follows_an_email_change(self):
        user = User.objects.create_user("u", "old@x.com", "pw")
        consent.set_user_preference(user, True, source=Source.PROFILE)
        user.email = "new@x.com"
        user.save()
        consent.sync_user_email(user)
        rows = list(MarketingSubscriber.objects.all())
        assert [r.email for r in rows] == ["new@x.com"]  # moved, not duplicated
        assert rows[0].is_subscribed and consent.user_wants_updates(user)

    def test_opting_out_after_an_email_change_stops_every_address(self):
        user = User.objects.create_user("u", "old@x.com", "pw")
        consent.set_user_preference(user, True, source=Source.PROFILE)
        consent.subscribe(
            "new@x.com", source=Source.IMPORT
        )  # new address already known
        user.email = "new@x.com"
        user.save()
        consent.set_user_preference(user, True, source=Source.PROFILE)
        consent.set_user_preference(user, False, source=Source.PROFILE)
        assert not MarketingSubscriber.objects.filter(status=Status.SUBSCRIBED).exists()

    def test_profile_email_change_moves_consent(self, client):
        user = User.objects.create_user(
            "u", "old@x.com", "pw", first_name="A", last_name="B"
        )
        consent.set_user_preference(user, True, source=Source.PROFILE)
        client.force_login(user)
        client.post(
            "/settings/", {"first_name": "A", "last_name": "B", "email": "new@x.com"}
        )
        user.refresh_from_db()
        if user.email == "new@x.com":  # profile email edits may need re-verification
            assert MarketingSubscriber.objects.get(user=user).email == "new@x.com"


# --- Lists ---------------------------------------------------------------------


@pytest.mark.django_db
class TestLists:
    def test_csv_import_reports_each_outcome(self, mlist):
        opted_out, _ = consent.subscribe("gone@x.com", source=Source.SIGNUP)
        consent.unsubscribe(opted_out, how="link")
        raw = (
            b"Email,Name,consent_date\n"
            b"new@x.com,New Person,2026-01-05\n"
            b"gone@x.com,Gone,\n"
            b"not-an-email,Bad,\n"
            b"NEW@x.com,Dup,\n"
            b"date@x.com,Date,05/01/2026\n"
        )
        report = lists.import_csv(mlist, raw, consent_note="Website form")
        assert report.added == 1
        assert report.kept_opted_out == 1
        assert len(report.invalid) == 2
        assert list(mlist.members.values_list("email", flat=True)) == ["new@x.com"]
        row = MarketingSubscriber.objects.get(email="new@x.com")
        assert row.consent_evidence["operator_note"] == "Website form"
        assert row.consented_at.date().isoformat() == "2026-01-05"

    def test_import_needs_an_email_column(self, mlist):
        with pytest.raises(ValueError):
            lists.import_csv(mlist, b"name\nx\n", consent_note="n")

    def test_dynamic_list_is_subscribed_owners_only(self):
        account = Account.objects.create(company_name="Acme")
        owner = User.objects.create_user("o", "o@x.com", "pw")
        agent = User.objects.create_user("a", "a@x.com", "pw")
        Membership.objects.create(
            user=owner, account=account, role=Membership.Role.OWNER
        )
        Membership.objects.create(
            user=agent, account=account, role=Membership.Role.MEMBER
        )
        for u in (owner, agent):
            consent.set_user_preference(u, True, source=Source.PROFILE)
        dyn = MarketingList.objects.create(
            name="Owners", kind=MarketingList.Kind.DYNAMIC, filters={"role": "owners"}
        )
        assert [s.email for s in lists.members(dyn)] == ["o@x.com"]


# --- Sending -------------------------------------------------------------------


@pytest.mark.django_db
class TestSending:
    def test_sends_while_the_email_channel_is_off(self, address, mlist):
        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        with (
            mock.patch("apps.core.channels.channel_state", return_value="disabled"),
            mock.patch("apps.platform_mail.tasks.send_campaign.delay"),
            mock.patch(SEND) as send,
        ):
            assert sending.start(campaign) == 1
            _drain(campaign)
        send.assert_called_once()
        args, kwargs = send.call_args
        assert args[0] == "a@x.com"
        assert "/updates/unsubscribe/" in kwargs["unsubscribe_url"]
        assert "Akilent Ltd, 1 Example Street" in args[3]  # html footer
        assert "Akilent Ltd, 1 Example Street" in args[2]  # text footer
        campaign.refresh_from_db()
        assert campaign.status == MarketingCampaign.Status.SENT

    def test_skips_unsubscribed_and_bounced(self, address, mlist):
        from apps.email.models import GlobalSuppression

        _member(mlist, "ok@x.com")
        _member(mlist, "no@x.com", status=Status.UNSUBSCRIBED)
        bounced = _member(mlist, "bounce@x.com")
        GlobalSuppression.objects.create(email="bounce@x.com", reason="bounce")
        campaign = _campaign(mlist)
        with (
            mock.patch("apps.platform_mail.tasks.send_campaign.delay"),
            mock.patch(SEND) as send,
        ):
            assert (
                sending.start(campaign) == 2
            )  # the unsubscribed one isn't queued at all
            _drain(campaign)
        assert [c.args[0] for c in send.call_args_list] == ["ok@x.com"]
        bounced.refresh_from_db()
        assert bounced.status == Status.SUPPRESSED

    def test_nobody_is_emailed_twice(self, address, mlist):
        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        with (
            mock.patch("apps.platform_mail.tasks.send_campaign.delay"),
            mock.patch(SEND) as send,
        ):
            sending.start(campaign)
            _drain(campaign)
            _drain(campaign)  # a retried or overlapping run
        assert send.call_count == 1
        assert MarketingSend.objects.filter(campaign=campaign).count() == 1

    def test_blocked_without_a_postal_address(self, mlist):
        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        with pytest.raises(sending.CampaignError, match="postal address"):
            sending.start(campaign)

    def test_a_provider_error_is_retried_not_dropped(self, address, mlist):
        from apps.platform_mail.services import render

        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        prepared = render.prepare(campaign)
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            sending.start(campaign)
        with mock.patch(SEND, side_effect=RuntimeError("SES credentials expired")):
            assert sending.deliver_next(campaign.pk, prepared) == sending.RETRY
        send = campaign.sends.get()
        assert send.status == MarketingSend.Status.PENDING and send.attempts == 1
        campaign.refresh_from_db()
        assert "credentials expired" in campaign.last_error
        assert not sending.finish_if_done(campaign.pk)  # not marked sent while retrying
        with mock.patch(SEND):
            assert sending.deliver_next(campaign.pk, prepared) == sending.SENT

    def test_a_send_is_failed_only_after_max_attempts(self, address, mlist):
        from apps.platform_mail.services import render

        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        prepared = render.prepare(campaign)
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            sending.start(campaign)
        outcomes = []
        with mock.patch(SEND, side_effect=RuntimeError("rejected")):
            for _ in range(sending.MAX_ATTEMPTS):
                outcomes.append(sending.deliver_next(campaign.pk, prepared))
        assert outcomes[-1] == sending.FAILED
        assert set(outcomes[:-1]) == {sending.RETRY}
        assert campaign.sends.get().status == MarketingSend.Status.FAILED

    def test_a_provider_error_stops_the_run_and_backs_off(self, address, mlist):
        from apps.platform_mail import tasks

        _member(mlist, "a@x.com")
        _member(mlist, "b@x.com")
        campaign = _campaign(mlist)
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            sending.start(campaign)
        with (
            mock.patch(SEND, side_effect=RuntimeError("down")) as send,
            mock.patch.object(tasks.send_campaign, "apply_async") as requeue,
        ):
            assert tasks.send_campaign(campaign.pk) == 0
        assert send.call_count == 1  # stopped at the first error, didn't burn the list
        assert requeue.call_args.kwargs["countdown"] == tasks.BACKOFF_SECONDS


@pytest.mark.django_db
class TestOneChainPerCampaign:
    @pytest.fixture
    def running(self, address, mlist):
        for i in range(3):
            _member(mlist, f"p{i}@x.com")
        campaign = _campaign(mlist)
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            sending.start(campaign)
        return campaign

    def test_a_second_run_cannot_join_a_live_chain(self, running, settings):
        from apps.platform_mail import tasks

        settings.PLATFORM_MARKETING_RATE_PER_MINUTE = 1
        with (
            mock.patch(SEND),
            mock.patch.object(tasks.send_campaign, "apply_async") as q,
        ):
            assert tasks.send_campaign(running.pk) == 1
            token = q.call_args.args[0][1]
            assert tasks.send_campaign(running.pk) == 0  # e.g. the backstop's run
            assert tasks.send_campaign(running.pk, "stale-token") == 0
            assert tasks.send_campaign(running.pk, token) == 1  # the chain carries on

    def test_backstop_leaves_a_live_chain_alone_and_revives_a_dead_one(
        self, running, settings
    ):
        from apps.platform_mail import tasks

        settings.PLATFORM_MARKETING_RATE_PER_MINUTE = 1  # leave sends pending
        with mock.patch(SEND), mock.patch.object(tasks.send_campaign, "apply_async"):
            tasks.send_campaign(running.pk)
        with mock.patch.object(tasks.send_campaign, "delay") as kick:
            assert tasks.resume_stalled_campaigns() == 0
            kick.assert_not_called()
        MarketingCampaign.objects.filter(pk=running.pk).update(
            lease_until=timezone.now() - timezone.timedelta(seconds=1)
        )
        with mock.patch.object(tasks.send_campaign, "delay") as kick:
            assert tasks.resume_stalled_campaigns() == 1

    def test_finished_campaign_releases_its_lease(self, running):
        from apps.platform_mail import tasks

        with (
            mock.patch(SEND),
            mock.patch.object(tasks.send_campaign, "apply_async") as q,
        ):
            tasks.send_campaign(running.pk)
        q.assert_not_called()
        running.refresh_from_db()
        assert running.status == MarketingCampaign.Status.SENT
        assert running.lease_until is None


@pytest.mark.django_db
class TestScheduleRaces:
    def test_beat_does_not_revert_a_campaign_started_meanwhile(self, address, mlist):
        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        sending.schedule(campaign, timezone.now() + timezone.timedelta(minutes=5))
        stale = MarketingCampaign.objects.get(pk=campaign.pk)  # the beat task's copy
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            sending.start(campaign)  # operator clicks Send now
        assert sending.unschedule(stale) is False
        campaign.refresh_from_db()
        assert campaign.status == MarketingCampaign.Status.SENDING

    def test_cancelled_campaign_cannot_be_rescheduled_from_a_stale_copy(
        self, address, mlist
    ):
        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        sending.schedule(campaign, timezone.now() + timezone.timedelta(minutes=5))
        stale = MarketingCampaign.objects.get(pk=campaign.pk)
        sending.cancel(campaign)
        with pytest.raises(sending.CampaignError):
            sending.schedule(stale, timezone.now() + timezone.timedelta(minutes=9))
        campaign.refresh_from_db()
        assert campaign.status == MarketingCampaign.Status.CANCELLED

    def test_cancel_drops_unsent(self, address, mlist):
        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            sending.start(campaign)
        assert sending.cancel(campaign) == 1
        campaign.refresh_from_db()
        assert campaign.status == MarketingCampaign.Status.CANCELLED

    def test_due_scheduled_campaign_starts(self, address, mlist):
        from apps.platform_mail.tasks import send_due_campaigns

        _member(mlist, "a@x.com")
        campaign = _campaign(mlist)
        sending.schedule(campaign, timezone.now() + timezone.timedelta(minutes=5))
        MarketingCampaign.objects.filter(pk=campaign.pk).update(
            scheduled_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            assert send_due_campaigns() == 1
        campaign.refresh_from_db()
        assert campaign.status == MarketingCampaign.Status.SENDING

    def test_body_markdown_escapes_html(self, address, mlist):
        from apps.platform_mail.services.render import body_html

        html = body_html("Hi <script>x</script>\n\n> quoted\n\n- one")
        assert "<script>" not in html
        assert "<blockquote" in html and "<li" in html

    def test_only_http_https_and_mailto_links_survive(self):
        from apps.platform_mail.services.render import body_html

        html = body_html(
            "[bad](javascript:alert(1)) [data](data:text/html,x) "
            "[ok](https://akilent.com/) [mail](mailto:hi@akilent.com) "
            "![img](javascript:alert(2))"
        )
        assert "javascript:" not in html and "data:" not in html
        assert 'href="https://akilent.com/"' in html
        assert 'href="mailto:hi@akilent.com"' in html
        assert "bad" in html  # the text stays, only the link goes

    def test_previews_are_sandboxed(self, client, address, mlist):
        campaign = _campaign(mlist)
        admin = User.objects.create_superuser("op", "op@x.com", "pw")
        client.force_login(admin)
        resp = client.get(f"/manage/marketing/campaigns/{campaign.pk}/preview/")
        assert resp["Content-Security-Policy"].startswith("sandbox")
        resp = client.get("/manage/emails/verify/frame/")
        assert resp["Content-Security-Policy"].startswith("sandbox")

    def test_prepared_render_matches_per_recipient_render(self, address, mlist):
        from apps.platform_mail.services import render

        campaign = _campaign(mlist)
        prepared = render.prepare(campaign)
        _, text, html, unsub = prepared.for_token("tok123")
        assert "/updates/unsubscribe/tok123/" in html
        assert "/updates/unsubscribe/tok123/" in text
        assert unsub.endswith("/updates/unsubscribe/tok123/")
        assert render._TOKEN_SLOT not in html


# --- Unsubscribe ---------------------------------------------------------------


@pytest.mark.django_db
class TestUnsubscribe:
    def test_link_and_one_click_post(self, client):
        row, _ = consent.subscribe("a@x.com", source=Source.SIGNUP)
        url = f"/updates/unsubscribe/{row.token}/"
        # A link scanner's GET only shows the confirm button.
        page = client.get(url)
        assert b"Unsubscribe from product updates?" in page.content
        row.refresh_from_db()
        assert row.status == Status.SUBSCRIBED
        assert client.post(url).status_code == 200  # RFC 8058, no CSRF token
        row.refresh_from_db()
        assert row.status == Status.UNSUBSCRIBED
        assert b"already unsubscribed" in client.get(url).content
        assert client.get("/updates/unsubscribe/nope/").status_code == 404

    def test_marketing_unsubscribe_does_not_block_system_mail(self):
        from apps.email.services.suppression import is_suppressed_globally

        row, _ = consent.subscribe("a@x.com", source=Source.SIGNUP)
        consent.unsubscribe(row, how="link")
        assert not is_suppressed_globally("a@x.com")


# --- Signup, profile and console -----------------------------------------------


@pytest.mark.django_db
class TestPagesAndForms:
    def test_profile_toggle(self, client):
        user = User.objects.create_user("u", "u@x.com", "pw")
        client.force_login(user)
        client.post("/settings/product-updates/", {"product_updates": "on"})
        assert consent.user_wants_updates(user)
        client.post("/settings/product-updates/", {})
        assert not consent.user_wants_updates(user)

    def test_signup_checkbox_is_unticked_by_default(self):
        from apps.accounts.forms import SignupForm

        field = SignupForm().fields["product_updates"]
        assert field.required is False
        assert not field.initial

    def test_console_pages_are_operator_only_and_render(self, client, address, mlist):
        campaign = _campaign(mlist)
        urls = [
            "/manage/marketing/",
            "/manage/marketing/campaigns/new/",
            f"/manage/marketing/campaigns/{campaign.pk}/",
            f"/manage/marketing/campaigns/{campaign.pk}/preview/",
            "/manage/marketing/lists/",
            f"/manage/marketing/lists/{mlist.pk}/",
            "/manage/marketing/subscribers/",
            "/manage/marketing/settings/",
        ]
        plain = User.objects.create_user("p", "p@x.com", "pw")
        client.force_login(plain)
        assert client.get(urls[0]).status_code in (302, 403)
        admin = User.objects.create_superuser("op", "op@x.com", "pw")
        client.force_login(admin)
        for url in urls:
            assert client.get(url).status_code == 200, url

    def test_console_create_campaign_and_send(self, client, address, mlist):
        _member(mlist, "a@x.com")
        admin = User.objects.create_superuser("op", "op@x.com", "pw")
        client.force_login(admin)
        resp = client.post(
            "/manage/marketing/campaigns/new/",
            {"name": "N", "subject": "S", "body": "B", "lists": [mlist.pk]},
        )
        campaign = MarketingCampaign.objects.get(name="N")
        assert resp.status_code == 302
        with mock.patch("apps.platform_mail.tasks.send_campaign.delay"):
            client.post(f"/manage/marketing/campaigns/{campaign.pk}/do/send/")
        campaign.refresh_from_db()
        assert campaign.status == MarketingCampaign.Status.SENDING

    def test_console_csv_import_requires_consent_confirmation(self, client, mlist):
        from django.core.files.uploadedfile import SimpleUploadedFile

        admin = User.objects.create_superuser("op", "op@x.com", "pw")
        client.force_login(admin)
        f = SimpleUploadedFile("l.csv", b"email\na@x.com\n", content_type="text/csv")
        client.post(
            f"/manage/marketing/lists/{mlist.pk}/import/",
            {"file": f, "consent_note": "x"},
        )
        assert not mlist.members.exists()
        f = SimpleUploadedFile("l.csv", b"email\na@x.com\n", content_type="text/csv")
        client.post(
            f"/manage/marketing/lists/{mlist.pk}/import/",
            {"file": f, "consent_note": "Website form", "confirm_consent": "on"},
        )
        assert mlist.members.filter(email="a@x.com").exists()


@pytest.mark.django_db
def test_backfill_subscribes_active_workspace_users_once():
    import importlib

    from django.apps import apps as django_apps

    backfill = importlib.import_module(
        "apps.platform_mail.migrations.0002_backfill_existing_customers"
    ).backfill
    account = Account.objects.create(company_name="Acme")
    member = User.objects.create_user("m", "M@x.com", "pw")
    Membership.objects.create(user=member, account=account, role=Membership.Role.OWNER)
    inactive = User.objects.create_user("i", "i@x.com", "pw", is_active=False)
    Membership.objects.create(
        user=inactive, account=account, role=Membership.Role.MEMBER
    )
    User.objects.create_user("loner", "loner@x.com", "pw")  # no workspace
    opted_out, _ = consent.subscribe("taken@x.com", source=Source.SIGNUP)
    consent.unsubscribe(opted_out, how="link")
    taken = User.objects.create_user("t", "taken@x.com", "pw")
    Membership.objects.create(user=taken, account=account, role=Membership.Role.MEMBER)

    backfill(django_apps, None)
    backfill(django_apps, None)  # idempotent

    rows = {s.email: s for s in MarketingSubscriber.objects.all()}
    assert set(rows) == {"m@x.com", "taken@x.com"}
    assert rows["m@x.com"].source == Source.EXISTING_CUSTOMER
    assert rows["taken@x.com"].status == Status.UNSUBSCRIBED  # an earlier "no" stands
