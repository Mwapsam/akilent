"""The Action Registry — the single execution surface Workflows, AI, the API,
and webhooks share.

Phase 1 established the contract (name, version, input schema, permission
check, execute). Phase 4 matures it:

  - ``run_action`` now validates the caller supplied every ``required`` input
    the action declares, before ``execute`` ever runs — a typo'd or missing
    kwarg fails fast with a clear ``ActionError`` instead of a deep
    ``TypeError``.
  - ``has_permission`` now receives the same ``**kwargs`` as ``execute``, so
    an action can check the target object's tenancy, not just the caller's
    identity. The default implementation does this generically via
    ``scope_kwarg`` (see below) so most actions don't need to override it.
  - ``apps.automation.workflow_engine`` gained a generic ``action`` step type
    that calls through this same registry — Workflows no longer need a
    hand-written function per action (``_run_send_whatsapp`` etc. remain for
    backward compatibility with existing published Workflow definitions, but
    new capabilities are exposed as registry actions instead).

Lives in ``apps.core`` because it's platform infrastructure, not owned by any
one business module: ``apps.conversations``, ``apps.crm``, and
``apps.commerce`` all register their actions here.
"""
from __future__ import annotations

import abc
import logging

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """Raised when an action's input is invalid or it cannot be executed."""


class Action(abc.ABC):
    """Base contract every registry action implements.

    ``scope_kwarg``, when set, names the ``execute`` kwarg the default
    ``has_permission`` uses for a tenancy check: if ``context["account"]`` is
    given, the object passed as that kwarg (or the kwarg itself, when it names
    "account" directly) must belong to that account. Leave unset only for
    actions with no single tenant-scoped kwarg to check (rare) — those must
    override ``has_permission`` themselves.
    """

    name: str
    version: int = 1
    scope_kwarg: str | None = None
    # Catalog feature this action belongs to (an ``apps.billing.features`` key). When the
    # caller's account can't use it, ``run_action`` refuses to run it.
    module: str | None = None

    def input_schema(self) -> dict:
        """JSON-schema-like description of accepted kwargs.

        ``{"required": [...], "optional": [...]}`` — ``run_action`` enforces
        ``required`` before calling ``execute``.
        """
        return {}

    def has_permission(self, context: dict, **kwargs) -> bool:
        """Whether the caller (described by ``context``) may run this action
        with these ``kwargs``.

        Permissive when the caller supplies no ``account`` in ``context``
        (e.g. tests, or a trusted internal call) or when this action declares
        no ``scope_kwarg`` — otherwise the object named by ``scope_kwarg``
        must belong to ``context["account"]``.
        """
        account = context.get("account")
        if account is None or self.scope_kwarg is None:
            return True

        target = kwargs.get(self.scope_kwarg)
        if target is None:
            return True
        target_account = target if self.scope_kwarg == "account" else getattr(target, "account", None)
        if target_account is None:
            return True

        return getattr(target_account, "pk", target_account) == getattr(account, "pk", account)

    @abc.abstractmethod
    def execute(self, context: dict, **kwargs) -> dict:
        """Perform the action. Must return a JSON-serializable result dict."""
        raise NotImplementedError


_REGISTRY: dict[str, Action] = {}


def register(action: Action) -> Action:
    if action.name in _REGISTRY:
        raise ActionError(f"action {action.name!r} is already registered")
    _REGISTRY[action.name] = action
    return action


def get_action(name: str) -> Action:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ActionError(f"no action registered as {name!r}") from None


def available_actions() -> list[str]:
    return sorted(_REGISTRY)


def run_action(name: str, context: dict, **kwargs) -> dict:
    """Validate, permission-check, and execute a registered action.

    This is the one call site every caller (Workflow steps, future AI, API
    endpoints, webhooks) should go through — never call an action's
    ``execute`` directly, since that would skip validation and permissions.
    """
    action = get_action(name)

    required = action.input_schema().get("required", [])
    missing = [field for field in required if field not in kwargs]
    if missing:
        raise ActionError(f"action {name!r} missing required input(s): {', '.join(missing)}")

    if not action.has_permission(context, **kwargs):
        raise ActionError(f"action {name!r} not permitted for this context")

    account = context.get("account")
    if action.module and account is not None:
        from apps.billing import api as billing_api

        if not billing_api.usable(account, action.module):
            raise ActionError(
                f"action {name!r} requires the {action.module!r} feature, "
                "which is not available to this account"
            )
    return action.execute(context, **kwargs)
