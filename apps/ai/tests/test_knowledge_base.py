"""Phase C: the knowledge base — apps.ai.models.KnowledgeBaseEntry, its role in
apps.ai.facts.build()/written_text() (so a KB-quoted fact isn't flagged as
unsupported), and its settings screen."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.ai import facts as business_facts
from apps.ai.autonomy import unsupported_facts
from apps.ai.models import AISettings, KnowledgeBaseEntry


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="Sunrise Solar")
    AISettings.objects.create(account=acc, enabled=True)
    return acc


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, user


@pytest.mark.django_db
class TestFactsIntegration:
    def test_active_entries_are_included_in_facts(self, account):
        e = KnowledgeBaseEntry.objects.create(
            account=account, title="Returns", content="14 days with a receipt."
        )
        facts = business_facts.build(account)
        assert facts["knowledge"] == [
            {"id": f"k{e.pk}", "title": "Returns", "content": "14 days with a receipt."}
        ]

    def test_inactive_entries_are_excluded(self, account):
        KnowledgeBaseEntry.objects.create(
            account=account, title="Old policy", content="Outdated.", is_active=False
        )
        facts = business_facts.build(account)
        assert facts["knowledge"] == []

    def test_entries_are_scoped_to_the_account(self, account):
        other = Account.objects.create(company_name="Other Co")
        KnowledgeBaseEntry.objects.create(
            account=other, title="Not yours", content="Shouldn't appear."
        )
        facts = business_facts.build(account)
        assert facts["knowledge"] == []

    def test_everything_goes_in_while_it_fits(self, account):
        for i in range(40):
            KnowledgeBaseEntry.objects.create(
                account=account, title=f"Entry {i:03d}", content="x"
            )
        facts = business_facts.build(account)
        assert len(facts["knowledge"]) == 40

    def test_past_the_budget_the_entry_being_asked_about_still_goes_in(self, account):
        """Not the first N by title: the 200th entry wins when it's the one asked about."""
        from apps.ai.facts import MAX_KNOWLEDGE_CHARS

        filler = "y" * (MAX_KNOWLEDGE_CHARS // 50)
        for i in range(200):
            KnowledgeBaseEntry.objects.create(
                account=account, title=f"Topic {i:03d}", content=filler
            )
        wanted = KnowledgeBaseEntry.objects.create(
            account=account,
            title="Zz How do I join TaskCentro as a freelancer?",
            content="Create an account, then set up your freelancer profile.",
        )
        facts = business_facts.build(
            account, query="How can I join TaskCentro as a freelancer?"
        )
        ids = [e["id"] for e in facts["knowledge"]]
        assert ids[0] == f"k{wanted.pk}"
        assert (
            sum(len(e["content"]) for e in facts["knowledge"])
            <= MAX_KNOWLEDGE_CHARS + 100
        )

    def test_knowledge_content_is_part_of_written_text(self, account):
        KnowledgeBaseEntry.objects.create(
            account=account, title="Delivery", content="K50 delivery fee in Lusaka."
        )
        facts = business_facts.build(account)
        assert "K50 delivery fee" in business_facts.written_text(facts)

    def test_a_price_from_the_knowledge_base_is_not_flagged_as_unsupported(
        self, account
    ):
        KnowledgeBaseEntry.objects.create(
            account=account,
            title="Delivery",
            content="Delivery costs K50 within Lusaka.",
        )
        facts = business_facts.build(account)
        reply = "Delivery is K50 within Lusaka."
        assert unsupported_facts(reply, facts) == []

    def test_a_price_not_in_the_knowledge_base_is_still_flagged(self, account):
        KnowledgeBaseEntry.objects.create(
            account=account,
            title="Delivery",
            content="Delivery costs K50 within Lusaka.",
        )
        facts = business_facts.build(account)
        reply = "Delivery is K999 within Lusaka."
        assert "K999" in unsupported_facts(reply, facts)


@pytest.mark.django_db
class TestSettingsView:
    def test_creating_an_entry(self, owner, account):
        client, _ = owner
        resp = client.post(
            "/settings/ai/knowledge/",
            {
                "title": "Return policy",
                "content": "14 days with a receipt.",
                "source_type": "policy",
            },
        )
        assert resp.status_code == 302
        entry = KnowledgeBaseEntry.objects.get(account=account, title="Return policy")
        assert entry.source_type == "policy"
        assert entry.is_active is True

    def test_a_blank_title_or_content_is_rejected(self, owner, account):
        client, _ = owner
        client.post("/settings/ai/knowledge/", {"title": "", "content": "x"})
        client.post("/settings/ai/knowledge/", {"title": "x", "content": ""})
        assert not KnowledgeBaseEntry.objects.filter(account=account).exists()

    def test_toggling_an_entry_off_and_on(self, owner, account):
        client, _ = owner
        entry = KnowledgeBaseEntry.objects.create(
            account=account, title="Hours", content="9-5 Mon-Sat"
        )
        client.post(f"/settings/ai/knowledge/{entry.pk}/toggle/")
        entry.refresh_from_db()
        assert entry.is_active is False
        client.post(f"/settings/ai/knowledge/{entry.pk}/toggle/")
        entry.refresh_from_db()
        assert entry.is_active is True

    def test_deleting_an_entry(self, owner, account):
        client, _ = owner
        entry = KnowledgeBaseEntry.objects.create(
            account=account, title="Hours", content="9-5 Mon-Sat"
        )
        client.post(f"/settings/ai/knowledge/{entry.pk}/delete/")
        assert not KnowledgeBaseEntry.objects.filter(pk=entry.pk).exists()

    def test_a_member_without_admin_rights_cannot_edit(self, client, account):
        user = User.objects.create_user("member", "m@example.com", "pw")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.MEMBER
        )
        client.force_login(user)
        client.post("/settings/ai/knowledge/", {"title": "x", "content": "y"})
        assert not KnowledgeBaseEntry.objects.filter(account=account).exists()

    def test_cannot_manage_another_accounts_entry(self, owner):
        client, _ = owner
        other = Account.objects.create(company_name="Other Co")
        other_entry = KnowledgeBaseEntry.objects.create(
            account=other, title="Not yours", content="x"
        )
        resp = client.post(f"/settings/ai/knowledge/{other_entry.pk}/toggle/")
        assert resp.status_code == 404
