"""Filling the blanks of a WhatsApp template: per customer, per business, or fixed text.

A blank is described in a step's ``variable_mapping`` as one of:

* ``"contact.first_name"`` / ``"contact.full_name"`` / ``"contact.phone"`` / ``"contact.email"``:
  read from the customer being messaged, so each person gets their own value;
* ``"contact.<attr>"``: one of the customer's custom fields;
* ``"account.company_name"``: the business's own name (same for everyone, and correct);
* ``"context.<key>"``: something the run recorded (for example what the customer tapped);
* a step's ``variable_fallbacks`` maps a blank to the text to use when a customer has no value
  for it, since WhatsApp rejects an empty blank;
* anything else: fixed text, **identical for every customer**. Kept so existing automations keep
  working, but the setup screen only offers it as an explicit, warned choice.
"""
from __future__ import annotations

# What the setup screen offers, in order: (choice key, source, label, default fallback).
CHOICES = (
    ("first_name", "contact.first_name", "Customer's first name", "there"),
    ("full_name", "contact.full_name", "Customer's full name", "there"),
    ("phone", "contact.phone", "Customer's phone number", ""),
    ("business", "account.company_name", "Your business name", ""),
)
_BY_KEY = {key: (source, label, fallback) for key, source, label, fallback in CHOICES}

_CONTACT_FIELDS = ("first_name", "last_name", "full_name", "phone", "email")
_NAME_LIKE = ("name", "first", "customer", "client", "person")
_BUSINESS_LIKE = ("company", "business", "shop", "store", "brand", "organisation", "organization")


def merge_first_name(text: str, first_name: str) -> str:
    """``{first_name}`` becomes the customer's first name, or "there" when we don't have one."""
    return (text or "").replace("{first_name}", (first_name or "").strip() or "there")


def default_choice(variable: str) -> str:
    """The safest choice for a blank called ``variable``: names are per customer, never fixed."""
    lowered = variable.lower()
    if any(word in lowered for word in _BUSINESS_LIKE):
        return "business"
    if "full" in lowered and "name" in lowered:
        return "full_name"
    if any(word in lowered for word in _NAME_LIKE):
        return "first_name"
    if "phone" in lowered or "number" in lowered:
        return "phone"
    return "literal"


def looks_like_name(variable: str) -> bool:
    """A blank that should never be one fixed value (a fixed name reaches the wrong customer)."""
    return default_choice(variable) in ("first_name", "full_name")


def entry_from_choice(choice: str, literal: str = "", fallback: str = ""):
    """``(source, fallback)`` for what the owner picked, or ``(None, "")`` if it is incomplete."""
    if choice in _BY_KEY:
        source, _label, default_fallback = _BY_KEY[choice]
        return source, (fallback or "").strip() or default_fallback
    # "literal" means the owner typed fixed text; any other unknown value is the old form's raw text.
    text = (literal if choice == "literal" else choice) or ""
    text = text.strip()
    return (text or None), ""


def is_fixed_text(source) -> bool:
    """True when ``source`` sends the same text to everyone."""
    return not (isinstance(source, str) and source.startswith(("contact.", "account.", "context.")))


def read_source(source: str, *, contact, account, context: dict):
    """The value of ``source`` for this customer, or None if they have none."""
    if source.startswith("contact."):
        attr = source[len("contact."):]
        if attr in _CONTACT_FIELDS:
            value = getattr(contact, attr, None)
            if value:
                return str(value)
        value = (contact.attributes or {}).get(attr)
        return None if value in (None, "") else str(value)
    if source.startswith("account."):
        attr = source[len("account."):]
        value = getattr(account, attr, None) if attr in ("company_name",) else None
        return str(value) if value else None
    if source.startswith("context."):
        value = context.get(source[len("context."):])
        return None if value in (None, "") else str(value)
    return source


def resolve(variables: list, mapping: dict, *, contact, account, context: dict,
            fallbacks: dict | None = None) -> dict:
    """The template params for one customer. Raises ``MissingValue`` naming the blank if one is empty."""
    params: dict = {}
    for var in variables or []:
        if var not in mapping:
            continue
        source = mapping[var]
        value = read_source(source, contact=contact, account=account, context=context) if isinstance(source, str) else source
        if value in (None, ""):
            value = (fallbacks or {}).get(var)
        if value in (None, ""):
            raise MissingValue(var)
        params[var] = value
    return params


class MissingValue(ValueError):
    """A blank has no value for this customer and no fallback, so the message must not be sent."""

    def __init__(self, variable: str):
        super().__init__(f"this customer has no value for '{variable}' and no fallback is set")
        self.variable = variable
