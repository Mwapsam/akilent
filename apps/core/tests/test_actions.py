"""Phase 4: Action Registry input validation and account-scoped permissions."""
import pytest

from apps.accounts.models import Account
from apps.core.actions import Action, ActionError, available_actions, register, run_action


class _EchoAction(Action):
    name = "test_echo"
    scope_kwarg = "account"

    def input_schema(self) -> dict:
        return {"required": ["account", "message"], "optional": ["loud"]}

    def execute(self, context, *, account, message, loud=False):
        return {"message": message.upper() if loud else message}


@pytest.fixture(autouse=True)
def _register_echo():
    register(_EchoAction())
    yield
    from apps.core.actions import _REGISTRY

    _REGISTRY.pop("test_echo", None)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.mark.django_db
def test_run_action_executes_registered_action(account):
    result = run_action("test_echo", {}, account=account, message="hi", loud=True)
    assert result == {"message": "HI"}


def test_run_action_unknown_name_raises():
    with pytest.raises(ActionError):
        run_action("does_not_exist", {})


@pytest.mark.django_db
def test_run_action_missing_required_field_raises(account):
    with pytest.raises(ActionError, match="missing required input"):
        run_action("test_echo", {}, account=account)


@pytest.mark.django_db
def test_run_action_permits_when_no_account_in_context(account):
    # No caller-supplied account context (e.g. a trusted internal call, or a
    # test) — permissive by design.
    result = run_action("test_echo", {}, account=account, message="hi")
    assert result == {"message": "hi"}


@pytest.mark.django_db
def test_run_action_permits_when_context_account_matches_target(account):
    result = run_action("test_echo", {"account": account}, account=account, message="hi")
    assert result == {"message": "hi"}


@pytest.mark.django_db
def test_run_action_rejects_when_context_account_differs_from_target(account):
    other = Account.objects.create(company_name="Other Co")
    with pytest.raises(ActionError, match="not permitted"):
        run_action("test_echo", {"account": other}, account=account, message="hi")


def test_available_actions_includes_registered_names():
    assert "test_echo" in available_actions()
