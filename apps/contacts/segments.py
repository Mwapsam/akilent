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
  tag                                                     -> value is a tag name ("VIP" == "vip");
                                                             operators eq ne in exists not_exists
  opened_in_last_90d                                      -> bool
  phone, lifecycle_stage                                  -> Contact columns
  first_seen_days                                         -> whole days since first seen
  has_phone                                               -> bool
  has_order                                               -> bool (any order, ever)
  whatsapp                                                -> opted_in | updates | opted_out | none
                                                             (apps.contacts.reachability's states);
                                                             operators eq ne in
  whatsapp_reachable                                      -> bool (opted in or updates only)

Operators: eq ne gt gte lt lte contains in exists not_exists
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

MAX_CONDITIONS = 40
MAX_DEPTH = 5

_DIRECT_FIELDS = {
    "email",
    "first_name",
    "last_name",
    "status",
    "locale",
    "source",
    "phone",
    "lifecycle_stage",
}
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


def _tag_q(op: str, value) -> Q:
    from django.utils.text import slugify

    if op == "exists":
        return Q(tags__isnull=False)
    if op == "not_exists":
        return Q(tags__isnull=True)
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise SegmentError("tag 'in' needs a list of tag names")
        return Q(tags__slug__in=[slugify(str(v))[:60] for v in value])
    if op not in ("eq", "ne") or not isinstance(value, str) or not value.strip():
        raise SegmentError("tag supports eq/ne/in/exists/not_exists with a tag name")
    has = Q(tags__slug=slugify(value)[:60])
    return ~has if op == "ne" else has


def _whatsapp_state_q(state: str) -> Q:
    """Customers in one ``reachability`` state, as a Q (same precedence: opted out wins)."""
    from apps.contacts import reachability
    from apps.whatsapp.models import WhatsAppContact

    _Opt = WhatsAppContact.OptInStatus
    linked = WhatsAppContact.objects.filter(contact_id__isnull=False)
    opted_out = Q(
        pk__in=linked.filter(opt_in_status=_Opt.OPTED_OUT).values("contact_id")
    )
    opted_in = Q(pk__in=linked.filter(opt_in_status=_Opt.OPTED_IN).values("contact_id"))
    reachable = Q(pk__in=linked.values("contact_id")) | (
        Q(phone__isnull=False) & ~Q(phone="")
    )
    if state == reachability.OPTED_OUT:
        return opted_out
    if state == reachability.OPTED_IN:
        return opted_in & ~opted_out
    if state == reachability.UPDATES:
        return reachable & ~opted_in & ~opted_out
    if state == reachability.NONE:
        return ~reachable
    raise SegmentError(f"unknown WhatsApp status {state!r}")


def _whatsapp_q(op: str, value) -> Q:
    if op == "in":
        if not isinstance(value, (list, tuple)) or not value:
            raise SegmentError("whatsapp 'in' needs a list of statuses")
        combined = Q(pk__in=[])
        for v in value:
            combined |= _whatsapp_state_q(str(v))
        return combined
    if op not in ("eq", "ne") or not isinstance(value, str):
        raise SegmentError("whatsapp supports eq/ne/in with a status")
    q = _whatsapp_state_q(value)
    return ~q if op == "ne" else q


def _days_q(column: str, op: str, value) -> Q:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise SegmentError(f"{column} needs a number of days")
    cutoff = timezone.now() - timedelta(days=float(value))
    if op in ("gt", "gte"):  # longer ago than N days
        return Q(**{f"{column}__lt": cutoff}) | Q(**{f"{column}__isnull": True})
    if op in ("lt", "lte"):  # within the last N days
        return Q(**{f"{column}__gte": cutoff})
    raise SegmentError("day rules support lt/lte/gt/gte only")


def _bool_q(q: Q, op: str, value) -> Q:
    if op not in ("eq", "ne"):
        raise SegmentError("yes/no rules support eq/ne only")
    want = bool(value) if op == "eq" else not bool(value)
    return q if want else ~q


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

    if field == "tag":
        return _tag_q(op, value)

    if field == "whatsapp":
        return _whatsapp_q(op, value)

    if field == "whatsapp_reachable":
        from apps.contacts import reachability

        q = _whatsapp_state_q(reachability.OPTED_IN) | _whatsapp_state_q(
            reachability.UPDATES
        )
        return _bool_q(q, op, value)

    if field == "has_phone":
        return _bool_q(Q(phone__isnull=False) & ~Q(phone=""), op, value)

    if field == "has_order":
        return _bool_q(Q(orders__isnull=False), op, value)

    if field == "first_seen_days":
        return _days_q("first_seen", op, value)

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
        return (
            ~Q(**{f"{orm_field}__isnull": True})
            if field in _DIRECT_FIELDS
            else Q(**{"attributes__has_key": field.split(".", 1)[1]})
        )
    if op == "not_exists":
        return (
            Q(**{f"{orm_field}__isnull": True})
            if field in _DIRECT_FIELDS
            else ~Q(**{"attributes__has_key": field.split(".", 1)[1]})
        )

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


def builder_fields(account) -> list[dict]:
    """What the segment builder offers, with the choices this business has.

    ``type``: choice | number | bool | text. ``operators`` are the AST operators
    the field accepts, each with the owner-facing wording.
    """
    from apps.contacts import reachability
    from apps.contacts.models import Contact, ContactList, CustomAttributeDef, Tag

    is_isnt = [["eq", "is"], ["ne", "isn't"]]
    text_ops = [
        ["contains", "contains"],
        ["eq", "is exactly"],
        ["exists", "is filled in"],
        ["not_exists", "is empty"],
    ]
    days_ops = [["lt", "in the last"], ["gt", "more than … ago"]]
    yes = [["eq", "is"]]
    fields = [
        {
            "key": "whatsapp",
            "label": "WhatsApp status",
            "type": "choice",
            "operators": is_isnt,
            "choices": [[s, reachability.LABELS[s]] for s in reachability.STATES],
        },
        {
            "key": "whatsapp_reachable",
            "label": "Can get WhatsApp messages",
            "type": "bool",
            "operators": yes,
        },
        {
            "key": "tag",
            "label": "Tag",
            "type": "choice",
            "operators": [["eq", "has"], ["ne", "doesn't have"]],
            "choices": [
                [t.name, t.name]
                for t in Tag.objects.filter(account=account).order_by("name")
            ],
        },
        {
            "key": "in_list",
            "label": "List",
            "type": "choice",
            "operators": [["eq", "is on"], ["ne", "isn't on"]],
            "choices": [
                [cl.slug, cl.name]
                for cl in ContactList.objects.filter(account=account).order_by("name")
            ],
        },
        {
            "key": "last_engaged_days",
            "label": "Last active",
            "type": "number",
            "unit": "days",
            "operators": days_ops,
        },
        {
            "key": "first_seen_days",
            "label": "First seen",
            "type": "number",
            "unit": "days",
            "operators": days_ops,
        },
        {"key": "has_order", "label": "Has ordered", "type": "bool", "operators": yes},
        {
            "key": "has_phone",
            "label": "Has a phone number",
            "type": "bool",
            "operators": yes,
        },
        {
            "key": "lifecycle_stage",
            "label": "Stage",
            "type": "choice",
            "operators": is_isnt,
            "choices": [[v, label] for v, label in Contact.LifecycleStage.choices],
        },
        {"key": "source", "label": "Came from", "type": "text", "operators": text_ops},
        {
            "key": "first_name",
            "label": "First name",
            "type": "text",
            "operators": text_ops,
        },
        {"key": "email", "label": "Email", "type": "text", "operators": text_ops},
    ]
    for d in CustomAttributeDef.objects.filter(
        account=account,
        entity=CustomAttributeDef.Entity.CONTACT,
        archived_at__isnull=True,
    ).order_by("label"):
        fields.append(
            {
                "key": f"attributes.{d.key}",
                "label": d.label or d.key,
                "type": "text",
                "operators": text_ops,
            }
        )
    return fields


def from_contact_filters(filters: dict, account) -> dict:
    """A definition matching the Customers page's filter (search, WhatsApp, tag, list)."""
    from apps.contacts.models import ContactList, Tag

    conditions: list = []
    q = (filters.get("q") or "").strip()
    if q:
        conditions.append(
            {
                "op": "or",
                "conditions": [
                    {"field": f, "operator": "contains", "value": q}
                    for f in ("first_name", "last_name", "email", "phone")
                ],
            }
        )
    if filters.get("wa"):
        conditions.append(
            {"field": "whatsapp", "operator": "eq", "value": filters["wa"]}
        )
    if filters.get("tag"):
        tag = Tag.objects.filter(account=account, slug=filters["tag"]).first()
        if tag is not None:
            conditions.append({"field": "tag", "operator": "eq", "value": tag.name})
    if filters.get("list"):
        cl = ContactList.objects.filter(account=account, pk=filters["list"]).first()
        if cl is not None:
            conditions.append({"field": "in_list", "operator": "eq", "value": cl.slug})
    return {"op": "and", "conditions": conditions}
