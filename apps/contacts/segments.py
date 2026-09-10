"""Compile a Segment ``definition`` AST into a Django ``Q`` and run it.

Definition shape (recursive)::

    {"op": "and" | "or",
     "conditions": [
        {"field": "<name>", "operator": "<op>", "value": <json>},
        { ...nested group... }
     ]}

Fields:
  email, first_name, last_name, status, locale, source   -> Contact columns
  attributes.<key>                                        -> JSON attribute
  last_engaged_days                                       -> whole days since last_engaged_at
  in_list                                                 -> value is a ContactList slug
  opened_in_last_90d                                      -> bool

Operators: eq ne gt gte lt lte contains in exists not_exists
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

MAX_CONDITIONS = 40
MAX_DEPTH = 5

_DIRECT_FIELDS = {"email", "first_name", "last_name", "status", "locale", "source"}
_OPERATOR_SUFFIX = {
    "eq": "",
    "ne": "",
    "gt": "__gt",
    "gte": "__gte",
    "lt": "__lt",
    "lte": "__lte",
    "contains": "__icontains",
    "in": "__in",
}


class SegmentError(ValueError):
    pass


def _leaf_q(cond: dict) -> Q:
    field = cond.get("field")
    op = cond.get("operator", "eq")
    value = cond.get("value")
    if not field or not isinstance(field, str):
        raise SegmentError("condition missing 'field'")
    if op not in {*_OPERATOR_SUFFIX, "exists", "not_exists"}:
        raise SegmentError(f"unknown operator {op!r}")

    # Computed facts ---------------------------------------------------------
    if field == "last_engaged_days":
        if not isinstance(value, (int, float)):
            raise SegmentError("last_engaged_days needs a numeric value")
        cutoff = timezone.now() - timedelta(days=float(value))
        if op in ("gt", "gte"):  # engaged longer ago than N days
            return Q(last_engaged_at__lt=cutoff) | Q(last_engaged_at__isnull=True)
        if op in ("lt", "lte"):  # engaged within the last N days
            return Q(last_engaged_at__gte=cutoff)
        raise SegmentError("last_engaged_days supports lt/lte/gt/gte only")

    if field == "opened_in_last_90d":
        cutoff = timezone.now() - timedelta(days=90)
        recent = Q(events__type__endswith="opened", events__occurred_at__gte=cutoff)
        return recent if value else ~recent

    if field == "in_list":
        member = Q(lists__slug=value)
        return member if op not in ("ne", "not_exists") else ~member

    # Attribute or direct column -----------------------------------------------
    if field.startswith("attributes."):
        key = field.split(".", 1)[1]
        if not key:
            raise SegmentError("empty attribute key")
        orm_field = f"attributes__{key}"
    elif field in _DIRECT_FIELDS:
        orm_field = field
    else:
        raise SegmentError(f"field {field!r} is not queryable")

    if op == "exists":
        return ~Q(**{f"{orm_field}__isnull": True}) if field in _DIRECT_FIELDS else Q(**{f"attributes__has_key": field.split('.', 1)[1]})
    if op == "not_exists":
        return Q(**{f"{orm_field}__isnull": True}) if field in _DIRECT_FIELDS else ~Q(**{f"attributes__has_key": field.split('.', 1)[1]})

    lookup = f"{orm_field}{_OPERATOR_SUFFIX[op]}"
    q = Q(**{lookup: value})
    return ~q if op == "ne" else q


def build_q(node: dict, *, _depth: int = 0, _counter: list | None = None) -> Q:
    if _counter is None:
        _counter = [0]
    if _depth > MAX_DEPTH:
        raise SegmentError("segment nested too deep")
    if not isinstance(node, dict):
        raise SegmentError("segment node must be an object")

    if "conditions" in node:
        op = node.get("op", "and").lower()
        if op not in ("and", "or"):
            raise SegmentError(f"group op must be and/or, got {op!r}")
        parts = []
        for child in node["conditions"]:
            parts.append(build_q(child, _depth=_depth + 1, _counter=_counter))
        if not parts:
            return Q()
        combined = parts[0]
        for p in parts[1:]:
            combined = (combined & p) if op == "and" else (combined | p)
        return combined

    _counter[0] += 1
    if _counter[0] > MAX_CONDITIONS:
        raise SegmentError("too many conditions")
    return _leaf_q(node)


def contacts_for(definition: dict, account):
    """Return the Contact queryset matching ``definition`` for ``account``."""
    from apps.contacts.models import Contact

    qs = Contact.objects.filter(account=account)
    if definition:
        qs = qs.filter(build_q(definition)).distinct()
    return qs


def count_for(definition: dict, account) -> int:
    return contacts_for(definition, account).count()
