"""The Action Registry — the single execution surface Workflows, AI, the API,
and webhooks share (Phase 1 of the platform roadmap).

Lives in ``apps.core`` because it's platform infrastructure, not owned by any
one business module: ``apps.conversations`` registers messaging/inbox
actions here, ``apps.crm`` registers CRM actions here, Commerce will do the
same in Phase 3. Every action implements the same shape (name, version,
input schema, permission check, execute) so callers never need to know which
module owns the underlying state — see ``run_action``, the one call site
every caller should go through.
"""
from __future__ import annotations

import abc
import logging

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """Raised when an action's input is invalid or it cannot be executed."""


class Action(abc.ABC):
    """Base contract every registry action implements."""

    name: str
    version: int = 1

    def input_schema(self) -> dict:
        """JSON-schema-like description of accepted kwargs. Advisory in Phase 1."""
        return {}

    def has_permission(self, context: dict) -> bool:
        """Whether the caller (given in ``context``) may run this action.

        Phase 1 default is permissive (any authenticated account context);
        modules that need finer-grained checks override this.
        """
        return True

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
    """Look up, permission-check, and execute a registered action.

    This is the one call site every caller (Workflow steps, future AI,
    API endpoints, webhooks) should go through — never call an action's
    ``execute`` directly.
    """
    action = get_action(name)
    if not action.has_permission(context):
        raise ActionError(f"action {name!r} not permitted for this context")
    return action.execute(context, **kwargs)
