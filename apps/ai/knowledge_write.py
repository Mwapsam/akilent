"""Single write service for KnowledgeBaseEntry mutations.

Every mutation to ``KnowledgeBaseEntry`` must go through this module.  Each
write increments ``AISettings.knowledge_version`` in the same transaction so
the BM25 index cache key (``kb_bm25:{account_id}:{version}``) is invalidated
before any reader can build a stale index from the new data.

If ``AISettings`` does not exist for the account (AI not yet enabled), the
version bump is a no-op — there is no index to invalidate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models as _m
from django.db import transaction

if TYPE_CHECKING:
    from apps.ai.models import KnowledgeBaseEntry


def bump_version(account_id: int) -> None:
    """Increment ``AISettings.knowledge_version`` for *account_id*.

    Must be called inside an open ``transaction.atomic()``.  Safe when no
    ``AISettings`` row exists (update touches 0 rows).
    """
    from apps.ai.models import AISettings

    AISettings.objects.filter(account_id=account_id).update(
        knowledge_version=_m.F("knowledge_version") + 1
    )


def create_entry(
    account,
    *,
    title: str,
    content: str,
    source_type: str = "faq",
    is_active: bool = True,
    origin: str = "manual",
    source_url: str = "",
) -> KnowledgeBaseEntry:
    from apps.ai.models import KnowledgeBaseEntry

    with transaction.atomic():
        entry = KnowledgeBaseEntry.objects.create(
            account=account,
            title=title,
            content=content,
            source_type=source_type,
            is_active=is_active,
            origin=origin,
            source_url=source_url[:500],
        )
        bump_version(account.pk)
    return entry


def save_entry(entry, *, update_fields: list[str]) -> None:
    """Save an already-mutated entry and bump the version in one transaction."""
    with transaction.atomic():
        entry.save(update_fields=update_fields)
        bump_version(entry.account_id)


def delete_entry(entry) -> None:
    account_id = entry.account_id
    with transaction.atomic():
        entry.delete()
        bump_version(account_id)


def bulk_update_entries(account, queryset, **update_kwargs: object) -> int:
    """``QuerySet.update`` wrapper that bumps the version once for the whole batch."""
    with transaction.atomic():
        n = queryset.update(**update_kwargs)
        if n:
            bump_version(account.pk)
    return n


def bulk_create_for_review(
    account,
    pairs: list[tuple[str, str]],
    *,
    origin: str,
    source_url: str = "",
) -> list:
    """Create inactive entries for review; one version bump for the whole batch.

    Entries whose title already exists in the account's knowledge base are
    skipped (case-insensitive).  Returns the list of created entries.
    """
    from apps.ai.models import KnowledgeBaseEntry

    with transaction.atomic():
        have = {
            t.lower()
            for t in KnowledgeBaseEntry.objects.filter(account=account).values_list(
                "title", flat=True
            )
        }
        created = []
        for question, answer in pairs:
            if question.lower() in have:
                continue
            have.add(question.lower())
            created.append(
                KnowledgeBaseEntry.objects.create(
                    account=account,
                    title=question,
                    content=answer,
                    source_type=KnowledgeBaseEntry.SourceType.FAQ,
                    origin=origin,
                    source_url=source_url[:500],
                    is_active=False,
                )
            )
        if created:
            bump_version(account.pk)
    return created
