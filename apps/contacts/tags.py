"""Tags: the simplest way to say what a customer is about.

Deterministic and channel-neutral, so a keyword rule ("price" -> tag ``pricing-enquiry``), a
human in the inbox and, later, anything else that can name a fact all write the same thing,
and a workflow branch or segment reads it back without caring who set it.

Tags are created on first use. Names are trimmed and case-folded into a slug, so "VIP",
"vip" and " Vip " are one tag. Adding a tag a contact already has, or removing one they do
not, is a quiet no-op: automations run more than once and must not error or duplicate.
"""
from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.contacts.models import Contact, Tag

MAX_NAME_LENGTH = 40
MAX_TAGS_PER_CONTACT = 50
MAX_TAGS_PER_ACCOUNT = 500


class TagError(ValueError):
    """A tag name or limit problem, worded for a business owner."""


def clean_name(raw: str) -> str:
    """Trim and collapse whitespace; refuse empty or over-long names."""
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name:
        raise TagError("Type a tag name.")
    if len(name) > MAX_NAME_LENGTH:
        raise TagError(f"Keep tag names under {MAX_NAME_LENGTH} characters.")
    if not slugify(name):
        raise TagError("A tag needs at least one letter or number.")
    return name


def slug_for(raw: str) -> str:
    return slugify(clean_name(raw))[:60]


def _get_or_create(account, name: str) -> Tag:
    slug = slugify(name)[:60]
    tag = Tag.objects.filter(account=account, slug=slug).first()
    if tag is not None:
        return tag
    if Tag.objects.filter(account=account).count() >= MAX_TAGS_PER_ACCOUNT:
        raise TagError("You've reached the limit of tags. Remove ones you no longer use.")
    try:
        with transaction.atomic():
            return Tag.objects.create(account=account, name=name, slug=slug)
    except IntegrityError:  # a concurrent request created it first
        return Tag.objects.get(account=account, slug=slug)


def add_tag(contact: Contact, raw_name: str) -> bool:
    """Tag ``contact``. Returns True if the tag was newly added."""
    name = clean_name(raw_name)
    tag = _get_or_create(contact.account, name)
    if contact.tags.filter(pk=tag.pk).exists():
        return False
    if contact.tags.count() >= MAX_TAGS_PER_CONTACT:
        raise TagError(f"A customer can have at most {MAX_TAGS_PER_CONTACT} tags.")
    contact.tags.add(tag)
    _record(contact, "tag.added", tag)
    return True


def remove_tag(contact: Contact, raw_name: str) -> bool:
    """Remove a tag from ``contact``. Returns True if they had it."""
    slug = slug_for(raw_name)
    tag = contact.tags.filter(slug=slug).first()
    if tag is None:
        return False
    contact.tags.remove(tag)
    _record(contact, "tag.removed", tag)
    return True


def _record(contact: Contact, event_type: str, tag: Tag) -> None:
    from apps.contacts.services import record_contact_event

    record_contact_event(contact, event_type, data={"tag": tag.name})
