"""Tag actions for the shared Action Registry (workflows, API, and anything later)."""
from __future__ import annotations

from apps.core.actions import Action, ActionError, register


class _TagAction(Action):
    scope_kwarg = "contact"

    def input_schema(self) -> dict:
        return {"required": ["contact", "tag"]}

    def _change(self, contact, tag):  # pragma: no cover - overridden
        raise NotImplementedError

    def execute(self, context: dict, *, contact, tag: str) -> dict:
        from apps.contacts.tags import TagError

        try:
            changed = self._change(contact, tag)
        except TagError as exc:
            raise ActionError(str(exc)) from exc
        return {"contact_id": contact.pk, "tag": tag, "changed": changed}


class AddTagAction(_TagAction):
    """Put a tag on a customer (no-op if they already have it)."""

    name = "add_tag"

    def _change(self, contact, tag):
        from apps.contacts.tags import add_tag

        return add_tag(contact, tag)


class RemoveTagAction(_TagAction):
    """Take a tag off a customer (no-op if they do not have it)."""

    name = "remove_tag"

    def _change(self, contact, tag):
        from apps.contacts.tags import remove_tag

        return remove_tag(contact, tag)


register(AddTagAction())
register(RemoveTagAction())
