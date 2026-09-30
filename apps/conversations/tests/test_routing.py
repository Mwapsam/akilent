"""Phase B.2: structured-signal routing (apps.conversations.routing), the
RouteConversationAction that wires it to team/agent assignment, and the
team-assignment audit trail (Conversation.set_team)."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership, Team
from apps.contacts.models import Contact
from apps.conversations.actions import run_action
from apps.conversations.models import Conversation, Event, RoutingRule
from apps.conversations.routing import match_team


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def sales(account):
    return Team.objects.create(account=account, name="Sales")


@pytest.fixture
def support(account):
    return Team.objects.create(account=account, name="Support")


def _conversation(account, *, channel=Conversation.Channel.WHATSAPP, tags=()):
    contact = Contact.objects.create(
        account=account, phone=f"+2609700000{Contact.objects.count():02d}"
    )
    if tags:
        from apps.contacts.tags import add_tag

        for tag in tags:
            add_tag(contact, tag)
    return Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )


@pytest.mark.django_db
class TestMatchTeam:
    def test_an_empty_conditions_rule_is_a_catchall(self, account, support):
        RoutingRule.objects.create(account=account, name="Default", team=support)
        conv = _conversation(account)
        assert match_team(conv) == support

    def test_channel_condition_matches(self, account, support):
        RoutingRule.objects.create(
            account=account,
            name="WhatsApp",
            team=support,
            conditions={"channel": "whatsapp"},
        )
        conv = _conversation(account, channel=Conversation.Channel.WHATSAPP)
        assert match_team(conv) == support

    def test_channel_condition_does_not_match_a_different_channel(
        self, account, support
    ):
        RoutingRule.objects.create(
            account=account,
            name="Email only",
            team=support,
            conditions={"channel": "email"},
        )
        conv = _conversation(account, channel=Conversation.Channel.WHATSAPP)
        assert match_team(conv) is None

    def test_tag_condition_matches_a_contact_tag(self, account, sales):
        RoutingRule.objects.create(
            account=account, name="VIP", team=sales, conditions={"tag": "vip"}
        )
        conv = _conversation(account, tags=["vip"])
        assert match_team(conv) == sales

    def test_tag_condition_does_not_match_an_untagged_contact(self, account, sales):
        RoutingRule.objects.create(
            account=account, name="VIP", team=sales, conditions={"tag": "vip"}
        )
        conv = _conversation(account)
        assert match_team(conv) is None

    def test_is_new_customer_signal(self, account, support):
        RoutingRule.objects.create(
            account=account,
            name="New customers",
            team=support,
            conditions={"is_new_customer": True},
        )
        first = _conversation(account)
        assert match_team(first) == support

        # A second conversation for the *same* contact is no longer new.
        second = Conversation.objects.create(
            account=account, contact=first.contact, channel=Conversation.Channel.SMS
        )
        assert match_team(second) is None

    def test_priority_order_first_match_wins(self, account, sales, support):
        RoutingRule.objects.create(
            account=account,
            name="VIP to sales",
            team=sales,
            conditions={"tag": "vip"},
            priority=0,
        )
        RoutingRule.objects.create(
            account=account, name="Default to support", team=support, priority=10
        )
        conv = _conversation(account, tags=["vip"])
        assert match_team(conv) == sales

        conv2 = _conversation(account)
        assert match_team(conv2) == support

    def test_an_inactive_rule_is_skipped(self, account, sales):
        RoutingRule.objects.create(
            account=account, name="Off", team=sales, is_active=False
        )
        conv = _conversation(account)
        assert match_team(conv) is None

    def test_no_matching_rule_returns_none(self, account, sales):
        RoutingRule.objects.create(
            account=account, name="VIP", team=sales, conditions={"tag": "vip"}
        )
        conv = _conversation(account)
        assert match_team(conv) is None


@pytest.mark.django_db
class TestRouteConversationAction:
    def test_matched_rule_assigns_team_and_an_active_agent(self, account, sales):
        agent = User.objects.create_user("agent", "a@example.com", "pw")
        Membership.objects.create(
            user=agent, account=account, role=Membership.Role.MEMBER
        )
        sales.members.add(agent)
        RoutingRule.objects.create(account=account, name="Default", team=sales)

        conv = _conversation(account)
        result = run_action("route_conversation", {}, conversation=conv)
        conv.refresh_from_db()

        assert conv.assigned_team_id == sales.id
        assert conv.assigned_to_id == agent.id
        assert result == {
            "conversation_id": conv.id,
            "team_id": sales.id,
            "assigned_to_id": agent.id,
        }

    def test_team_with_no_active_members_still_sets_the_team(self, account, sales):
        """Fallback per the plan: team found, no agent available -> team/unassigned,
        never an error and never fully unassigned when a team did match."""
        RoutingRule.objects.create(account=account, name="Default", team=sales)
        conv = _conversation(account)

        result = run_action("route_conversation", {}, conversation=conv)
        conv.refresh_from_db()

        assert conv.assigned_team_id == sales.id
        assert conv.assigned_to_id is None
        assert result["assigned_to_id"] is None

    def test_no_matching_rule_leaves_the_conversation_fully_unassigned(self, account):
        conv = _conversation(account)
        result = run_action("route_conversation", {}, conversation=conv)
        conv.refresh_from_db()

        assert conv.assigned_team_id is None
        assert conv.assigned_to_id is None
        assert result == {
            "conversation_id": conv.id,
            "team_id": None,
            "assigned_to_id": None,
        }

    def test_routing_picks_the_least_busy_member_of_the_matched_team(
        self, account, sales
    ):
        busy = User.objects.create_user("busy", "b@example.com", "pw")
        free = User.objects.create_user("free", "f@example.com", "pw")
        for u in (busy, free):
            Membership.objects.create(
                user=u, account=account, role=Membership.Role.MEMBER
            )
            sales.members.add(u)
        # Give "busy" an existing open conversation so they're not picked.
        already = _conversation(account)
        already.assign(busy)

        RoutingRule.objects.create(account=account, name="Default", team=sales)
        conv = _conversation(account)
        run_action("route_conversation", {}, conversation=conv)
        conv.refresh_from_db()
        assert conv.assigned_to_id == free.id

    def test_a_member_of_another_team_is_never_picked(self, account, sales, support):
        outsider = User.objects.create_user("out", "o@example.com", "pw")
        Membership.objects.create(
            user=outsider, account=account, role=Membership.Role.MEMBER
        )
        support.members.add(outsider)  # on Support, not Sales

        RoutingRule.objects.create(account=account, name="Default", team=sales)
        conv = _conversation(account)
        result = run_action("route_conversation", {}, conversation=conv)
        assert result["assigned_to_id"] is None


@pytest.mark.django_db
class TestTeamAssignmentAudit:
    def test_set_team_records_an_event(self, account, sales):
        conv = _conversation(account)
        conv.set_team(sales, actor="automation:route_conversation")
        event = Event.objects.get(
            subject_type="conversation",
            subject_id=str(conv.pk),
            type="conversation.team_assigned",
        )
        assert event.actor == "automation:route_conversation"
        assert event.payload == {
            "assigned_team_id": sales.id,
            "previous_team_id": None,
        }

    def test_setting_the_same_team_again_records_nothing(self, account, sales):
        conv = _conversation(account)
        conv.set_team(sales)
        conv.set_team(sales)
        assert (
            Event.objects.filter(
                subject_type="conversation", type="conversation.team_assigned"
            ).count()
            == 1
        )

    def test_clearing_the_team_records_an_unassign_event(self, account, sales):
        conv = _conversation(account)
        conv.set_team(sales)
        conv.set_team(None)
        event = Event.objects.get(
            subject_type="conversation", type="conversation.team_unassigned"
        )
        assert event.payload == {
            "assigned_team_id": None,
            "previous_team_id": sales.id,
        }
