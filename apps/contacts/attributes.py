"""Custom-attribute write service (Workstream C).

``set_attributes`` is the single supported writer for Contact, Lead, and Deal
custom attributes. Every new write path (workflow steps, ask_question, HubSpot
inbound) must call it.

Intentional compatibility exceptions (legacy, unmanaged-ingestion paths):
  - ``contacts.services.upsert_contact`` / ``upsert_contact_by_phone`` — raw
    attribute merge for CSV imports and external API ingestion; no schema
    enforcement, no events. These paths do not create leads/deals, so the
    lead/deal schema gap does not apply.
  - ``conversations.forms`` — chatbot-form answers written directly to
    ``contact.attributes``; considered a transient, display-only store.
  Both are documented as intentional and must not be silently widened.

Workflow ``set_attribute`` steps use a compatibility fallback: if no
``CustomAttributeDef`` exists for a contact key, the write proceeds as before
(direct merge, no event) with a deprecation warning. Lead/deal steps always
require a definition.

Lock order (always):
  target row (obj)  →  CustomAttributeDef rows (ascending pk)

``archive_attribute_def`` follows the same lock order and refuses to archive
while a published workflow or an active/waiting run still references the key.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Sentinel: an explicit None in ``values`` means "remove this key".
_REMOVE = None


def set_attributes(
    obj,
    values: dict[str, Any],
    *,
    actor: str = "",
    source: str,
) -> dict[str, tuple]:
    """Write custom attributes to a Contact, Lead, or Deal.

    Args:
        obj:     A Contact, Lead, or Deal instance.
        values:  ``{key: value}``. An explicit ``None`` value removes the key.
                 Omitted keys are left unchanged.
        actor:   User identifier — stored in the event, never logged as a value.
        source:  e.g. ``"workflow"``, ``"api"``, ``"import"``, ``"hubspot"``.

    Returns:
        ``{key: (old_value, new_value)}`` for every key that actually changed.
        Empty dict when nothing changed (no event is emitted in that case).

    Raises:
        ValueError: unknown key, archived definition, or type-coercion failure.
    """
    if not values:
        return {}

    entity = _entity_for(obj)
    keys = list(values.keys())

    with transaction.atomic():
        # Lock the target row first; read attributes from the locked instance
        # to avoid acting on a stale snapshot fetched before the lock.
        locked_obj = _lock_obj(obj)

        # Lock every referenced definition in pk order.
        defs = {
            d.key: d
            for d in _lock_defs(obj.account, entity, keys)
        }

        # Validate all keys up front before writing anything.
        for key in keys:
            if key not in defs:
                raise ValueError(
                    f"Unknown attribute key {key!r} for {entity} "
                    f"(account {obj.account_id})"
                )
            if defs[key].is_archived:
                raise ValueError(
                    f"Attribute {key!r} is archived and cannot be written"
                )

        # Read from the locked DB row, not the potentially-stale in-memory obj.
        current = dict(locked_obj.attributes or {})
        diff: dict[str, tuple] = {}

        for key, raw_value in values.items():
            defn = defs[key]
            old_value = current.get(key)

            if raw_value is _REMOVE:
                if key in current:
                    diff[key] = (old_value, None)
                    del current[key]
            else:
                new_value = _coerce(raw_value, defn)
                if new_value != old_value:
                    diff[key] = (old_value, new_value)
                    current[key] = new_value

        if not diff:
            return {}

        obj.attributes = current
        obj.save(update_fields=["attributes", "updated_at"])

        _emit_changed(obj, entity, diff, actor=actor, source=source)

    logger.info(
        "set_attributes: entity=%s id=%s keys=%s source=%s",
        entity,
        obj.pk,
        sorted(diff.keys()),
        source,
    )
    return diff


def archive_attribute_def(defn, *, actor: str = "") -> None:
    """Archive a CustomAttributeDef.

    Blocked if any published workflow or any active/waiting run references
    the key (for this account + entity combination).

    Raises:
        ValueError: if the definition is already archived, or is still in use.
    """
    from apps.contacts.models import CustomAttributeDef

    with transaction.atomic():
        # Lock the definition row first (pk order is trivially satisfied for one row).
        locked = (
            CustomAttributeDef.objects.select_for_update()
            .filter(pk=defn.pk)
            .get()
        )

        if locked.is_archived:
            raise ValueError(f"Attribute {locked.key!r} is already archived.")

        _check_not_referenced(locked)

        locked.archived_at = timezone.now()
        locked.save(update_fields=["archived_at"])

    logger.info(
        "archive_attribute_def: entity=%s key=%s actor=%s",
        locked.entity,
        locked.key,
        actor,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _entity_for(obj) -> str:
    from apps.contacts.models import Contact
    from apps.crm.models import Deal, Lead

    if isinstance(obj, Contact):
        return "contact"
    if isinstance(obj, Lead):
        return "lead"
    if isinstance(obj, Deal):
        return "deal"
    raise TypeError(f"set_attributes: unsupported object type {type(obj).__name__!r}")


def _lock_obj(obj):
    """Lock the target row in its own table; return the fresh locked instance."""
    return obj.__class__.objects.select_for_update().filter(pk=obj.pk).get()


def _lock_defs(account, entity: str, keys: list[str]):
    """Lock all referenced definitions in pk order and return them."""
    from apps.contacts.models import CustomAttributeDef

    return list(
        CustomAttributeDef.objects.select_for_update()
        .filter(account=account, entity=entity, key__in=keys)
        .order_by("pk")
    )


def _coerce(value: Any, defn) -> Any:
    """Coerce ``value`` to the canonical type for ``defn``.

    Canonical forms:
      string  → str
      number  → decimal string, e.g. "1250.00" (2 d.p.)
      boolean → bool
      date    → "YYYY-MM-DD"
      choice  → option key (str), validated against defn.options
    """
    t = defn.type

    if t == "string":
        return str(value)

    if t == "number":
        try:
            return str(Decimal(str(value)).quantize(Decimal("0.01")))
        except InvalidOperation:
            raise ValueError(
                f"Attribute {defn.key!r}: {value!r} cannot be converted to a number"
            )

    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() in ("true", "yes", "1"):
                return True
            if value.lower() in ("false", "no", "0"):
                return False
        if isinstance(value, int):
            return bool(value)
        raise ValueError(
            f"Attribute {defn.key!r}: {value!r} cannot be converted to boolean"
        )

    if t == "date":
        import datetime

        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.strftime("%Y-%m-%d")
        s = str(value).strip()
        # Validate format.
        try:
            datetime.date.fromisoformat(s)
        except ValueError:
            raise ValueError(
                f"Attribute {defn.key!r}: {value!r} is not a valid date (YYYY-MM-DD)"
            )
        return s

    if t == "choice":
        valid_keys = {opt["key"] for opt in (defn.options or []) if "key" in opt}
        s = str(value)
        if valid_keys and s not in valid_keys:
            raise ValueError(
                f"Attribute {defn.key!r}: {s!r} is not a valid option "
                f"(valid: {sorted(valid_keys)})"
            )
        return s

    # Unknown type — store as string (future-proof).
    return str(value)


def _emit_changed(obj, entity: str, diff: dict, *, actor: str, source: str) -> None:
    """Emit one ``attribute.changed`` event per changed key."""
    from apps.conversations.services import emit_event

    account = obj.account
    subject_id = str(getattr(obj, "public_id", obj.pk))
    now = timezone.now()

    for key, (old_value, new_value) in diff.items():
        emit_event(
            account=account,
            type="attribute.changed",
            occurred_at=now,
            source=source,
            payload={
                "entity": entity,
                "entity_id": subject_id,
                "key": key,
                "old_value": old_value,
                "new_value": new_value,
                "actor": actor,
            },
            actor=actor,
            subject_type=entity,
            subject_id=subject_id,
        )


def _check_not_referenced(defn) -> None:
    """Raise ValueError if any published workflow or active/waiting run
    references defn.key for defn.entity."""
    from apps.automation.models import WorkflowRun
    from apps.automation.models import Workflow

    key = defn.key
    entity = defn.entity
    account = defn.account

    # Check published workflows.
    for wf in Workflow.objects.filter(
        account=account, status=Workflow.Status.PUBLISHED
    ):
        if _definition_references_key(wf.definition, key, entity):
            raise ValueError(
                f"Cannot archive {key!r}: published workflow {wf.name!r} "
                f"(id={wf.pk}) references it."
            )

    # Check active or waiting runs.
    active_refs = (
        WorkflowRun.objects.filter(
            workflow__account=account,
            status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        )
        .select_related("workflow")
    )
    for run in active_refs:
        if _definition_references_key(run.workflow.definition, key, entity):
            raise ValueError(
                f"Cannot archive {key!r}: run {run.pk} (workflow {run.workflow.name!r}) "
                f"is currently active/waiting and references it."
            )


def verify_defs_for_publish(account, definition: dict) -> None:
    """Lock all attribute defs referenced by a workflow definition and verify none are archived.

    Must be called **inside** a ``transaction.atomic()`` block. The lock is held
    until the transaction commits, which serialises this publish with any concurrent
    ``archive_attribute_def`` call on the same definitions.

    Raises:
        ValueError: if any referenced definition is archived or does not exist.
    """
    refs = _collect_attribute_refs(definition)
    required = refs.get("required", {})
    optional = refs.get("optional", {})
    if not required and not optional:
        return

    from apps.contacts.models import CustomAttributeDef

    # Collect all entities and keys to lock in one pass, in pk order.
    all_entities = sorted(set(list(required.keys()) + list(optional.keys())))
    for entity in all_entities:
        req_keys = required.get(entity, [])
        opt_keys = optional.get(entity, [])
        all_keys = list({*req_keys, *opt_keys})
        if not all_keys:
            continue

        # Lock ALL matching defs (including archived) to serialise with archive.
        locked = list(
            CustomAttributeDef.objects.select_for_update()
            .filter(account=account, entity=entity, key__in=all_keys)
            .order_by("pk")
        )
        found_keys = {d.key for d in locked}

        # Required keys must have a live def.
        for key in req_keys:
            if key not in found_keys:
                raise ValueError(
                    f"Cannot publish: no attribute definition for key {key!r} "
                    f"(entity={entity!r}). Create it before publishing."
                )

        # All found defs (required or optional) must not be archived.
        for defn in locked:
            if defn.is_archived:
                raise ValueError(
                    f"Cannot publish: attribute {defn.key!r} ({entity!r}) is archived. "
                    "Remove or update the step that references it."
                )


def _collect_attribute_refs(definition: dict) -> dict:
    """Extract {entity: [keys]} that MUST have a live CustomAttributeDef at publish time.

    Rules (matching runtime behaviour):
    - ``ask_question``: always requires a def (for any entity).
    - ``set_attribute`` on lead/deal: always requires a def.
    - ``set_attribute`` on contact: compatibility fallback — a missing def is allowed
      at runtime (legacy direct write), so we do NOT require one here. We still check
      that if a def EXISTS for that key it is not archived.
    """
    # Keys that must exist: ask_question (all entities) + set_attribute (lead/deal only)
    required: dict = {}
    # Keys that are optional but must not be archived if they do exist
    optional: dict = {}

    for step in (definition or {}).get("steps", []):
        stype = step.get("type")
        if stype == "set_attribute":
            key = step.get("key", "")
            entity = step.get("target", "contact")
            if not key:
                continue
            if entity in ("lead", "deal"):
                required.setdefault(entity, [])
                if key not in required[entity]:
                    required[entity].append(key)
            else:
                # contact: optional — only block if archived, not if missing
                optional.setdefault(entity, [])
                if key not in optional[entity]:
                    optional[entity].append(key)
        elif stype == "ask_question":
            key = step.get("attribute", "")
            entity = step.get("target", "contact")
            if not key:
                continue
            required.setdefault(entity, [])
            if key not in required[entity]:
                required[entity].append(key)

    return {"required": required, "optional": optional}


def _definition_references_key(definition: dict, key: str, entity: str) -> bool:
    """Return True if any attribute-writing step in this definition targets key+entity."""
    steps = (definition or {}).get("steps", [])
    for step in steps:
        stype = step.get("type")
        if stype == "set_attribute":
            if step.get("key") == key and step.get("target", "contact") == entity:
                return True
        elif stype == "ask_question":
            if step.get("attribute") == key and step.get("target", "contact") == entity:
                return True
    return False
