"""Activate a vertical Starter pack for an account: enable its modules and
create/publish its bundled Workflows. Idempotent — re-activating re-syncs
both without creating duplicates, so it's safe to offer "Activate" again
after a template's definition changes in a later release.
"""
from __future__ import annotations

from apps.automation import api as automation_api
from apps.automation.workflow_engine import validate_definition
from apps.verticals.models import VerticalActivation
from apps.verticals.registry import get_vertical


class VerticalNotFound(Exception):
    pass


class VerticalTemplateInvalid(Exception):
    """A bundled workflow definition failed validation — a bug in the
    template itself, not something the activating account did wrong."""


def activate_vertical(account, key: str) -> VerticalActivation:
    vertical = get_vertical(key)
    if vertical is None:
        raise VerticalNotFound(f"no vertical registered as {key!r}")

    for wf_spec in vertical["workflows"]:
        errors = validate_definition(wf_spec["definition"], account=account)
        blocking = [e for e in errors if e.get("severity", "error") != "warning"]
        if blocking:
            raise VerticalTemplateInvalid(
                f"vertical {key!r} workflow {wf_spec['slug']!r} is invalid: "
                + "; ".join(e["message"] for e in blocking)
            )

    from apps.billing import api as billing_api

    for module in vertical["modules"]:
        billing_api.enable_module(account, module)

    for wf_spec in vertical["workflows"]:
        automation_api.upsert_published_workflow(
            account, slug=wf_spec["slug"], name=wf_spec["name"], definition=wf_spec["definition"],
        )

    activation, _ = VerticalActivation.objects.get_or_create(account=account, key=key)
    return activation


def activated_vertical_keys(account) -> set[str]:
    return set(VerticalActivation.objects.filter(account=account).values_list("key", flat=True))
