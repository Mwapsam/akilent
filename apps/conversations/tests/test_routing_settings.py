"""Phase B.2 settings screens: /inbox/teams/ and /inbox/routing-rules/."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership, Team
from apps.conversations.models import RoutingRule


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.mark.django_db
class TestTeams:
    def test_creating_a_team(self, logged_in):
        client, account, _ = logged_in
        resp = client.post("/inbox/teams/", {"name": "Sales"})
        assert resp.status_code == 302
        assert Team.objects.filter(account=account, name="Sales").exists()

    def test_duplicate_team_name_is_rejected(self, logged_in):
        client, account, _ = logged_in
        Team.objects.create(account=account, name="Sales")
        client.post("/inbox/teams/", {"name": "Sales"})
        assert Team.objects.filter(account=account, name="Sales").count() == 1

    def test_a_blank_name_is_rejected(self, logged_in):
        client, account, _ = logged_in
        client.post("/inbox/teams/", {"name": "  "})
        assert not Team.objects.filter(account=account).exists()

    def test_adding_and_removing_a_member(self, logged_in):
        client, account, _ = logged_in
        teammate = User.objects.create_user("sam", "sam@example.com", "pw")
        Membership.objects.create(
            user=teammate, account=account, role=Membership.Role.MEMBER
        )
        team = Team.objects.create(account=account, name="Sales")

        client.post(
            f"/inbox/teams/{team.pk}/",
            {"action": "add_member", "user_id": teammate.pk},
        )
        assert list(team.members.all()) == [teammate]

        client.post(
            f"/inbox/teams/{team.pk}/",
            {"action": "remove_member", "user_id": teammate.pk},
        )
        assert list(team.members.all()) == []

    def test_cannot_add_someone_outside_the_account(self, logged_in):
        client, account, _ = logged_in
        stranger = User.objects.create_user("stranger", "s@example.com", "pw")
        other = Account.objects.create(company_name="Other Co")
        Membership.objects.create(
            user=stranger, account=other, role=Membership.Role.OWNER
        )
        team = Team.objects.create(account=account, name="Sales")
        client.post(
            f"/inbox/teams/{team.pk}/",
            {"action": "add_member", "user_id": stranger.pk},
        )
        assert list(team.members.all()) == []

    def test_cannot_manage_another_accounts_team(self, logged_in):
        client, _, _ = logged_in
        other = Account.objects.create(company_name="Other Co")
        other_team = Team.objects.create(account=other, name="Support")
        resp = client.get(f"/inbox/teams/{other_team.pk}/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestRoutingRules:
    def test_creating_a_default_rule(self, logged_in):
        client, account, _ = logged_in
        team = Team.objects.create(account=account, name="Support")
        resp = client.post(
            "/inbox/routing-rules/",
            {"name": "Default", "team": team.pk, "priority": "0"},
        )
        assert resp.status_code == 302
        rule = RoutingRule.objects.get(account=account, name="Default")
        assert rule.team_id == team.id
        assert rule.conditions == {}

    def test_creating_a_rule_with_conditions(self, logged_in):
        client, account, _ = logged_in
        team = Team.objects.create(account=account, name="Sales")
        client.post(
            "/inbox/routing-rules/",
            {
                "name": "VIP WhatsApp",
                "team": team.pk,
                "priority": "0",
                "channel": "whatsapp",
                "tag": "vip",
                "has_open_lead": "1",
            },
        )
        rule = RoutingRule.objects.get(account=account, name="VIP WhatsApp")
        assert rule.conditions == {
            "channel": "whatsapp",
            "tag": "vip",
            "has_open_lead": True,
        }

    def test_a_rule_without_a_team_is_rejected(self, logged_in):
        client, account, _ = logged_in
        client.post("/inbox/routing-rules/", {"name": "Orphan", "team": "999"})
        assert not RoutingRule.objects.filter(account=account).exists()

    def test_toggling_a_rule_off_and_on(self, logged_in):
        client, account, _ = logged_in
        team = Team.objects.create(account=account, name="Support")
        rule = RoutingRule.objects.create(account=account, name="Default", team=team)
        client.post(f"/inbox/routing-rules/{rule.pk}/toggle/")
        rule.refresh_from_db()
        assert rule.is_active is False
        client.post(f"/inbox/routing-rules/{rule.pk}/toggle/")
        rule.refresh_from_db()
        assert rule.is_active is True

    def test_deleting_a_rule(self, logged_in):
        client, account, _ = logged_in
        team = Team.objects.create(account=account, name="Support")
        rule = RoutingRule.objects.create(account=account, name="Default", team=team)
        client.post(f"/inbox/routing-rules/{rule.pk}/delete/")
        assert not RoutingRule.objects.filter(pk=rule.pk).exists()

    def test_cannot_toggle_another_accounts_rule(self, logged_in):
        client, _, _ = logged_in
        other = Account.objects.create(company_name="Other Co")
        other_team = Team.objects.create(account=other, name="Support")
        other_rule = RoutingRule.objects.create(
            account=other, name="Default", team=other_team
        )
        resp = client.post(f"/inbox/routing-rules/{other_rule.pk}/toggle/")
        assert resp.status_code == 404
        other_rule.refresh_from_db()
        assert other_rule.is_active is True
