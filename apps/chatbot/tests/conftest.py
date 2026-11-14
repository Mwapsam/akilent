import pytest

from apps.accounts.models import Account
from apps.chatbot.models import ChatbotAction, ChatbotConfig, ChatSession


@pytest.fixture(autouse=True)
def _plan_includes_website_chat(request, monkeypatch):
    """Test businesses have no plan; let them use website chat unless a test checks the gate
    itself (mark it ``real_plan``)."""
    if request.node.get_closest_marker("real_plan"):
        return
    from apps.billing import api as billing_api

    real = billing_api.usable

    def usable(account, key):
        return True if key == "website_chat" else real(account, key)

    monkeypatch.setattr(billing_api, "usable", usable)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme Corp")


@pytest.fixture
def another_account(db):
    return Account.objects.create(company_name="Rival Corp")


@pytest.fixture
def chatbot(account):
    return ChatbotConfig.objects.create(
        account=account,
        name="Acme Bot",
        allowed_domains=["https://acme.com"],
    )


@pytest.fixture
def chatbot_no_domains(account):
    return ChatbotConfig.objects.create(
        account=account,
        name="Unconfigured Bot",
        allowed_domains=[],
    )


@pytest.fixture
def session(chatbot):
    return ChatSession.objects.create(chatbot=chatbot)


@pytest.fixture
def action(chatbot):
    return ChatbotAction.objects.create(
        chatbot=chatbot,
        slug="check_payment_status",
        label="Check payment status",
        description="Look up a payment by ID.",
        is_enabled=True,
    )


@pytest.fixture
def disabled_action(chatbot):
    return ChatbotAction.objects.create(
        chatbot=chatbot,
        slug="disabled_action",
        label="Disabled",
        description="Should never run.",
        is_enabled=False,
    )
