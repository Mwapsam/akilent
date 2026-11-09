"""Putting rules in order, by drag or by Move up / Move down (Stage 5 rule builder).

Rules that run first-match (comment replies, moderation, routing) keep a numeric ``priority``,
lower first. People don't set numbers any more: they drag a row, or press Move up / Move
down (the same forms work by keyboard, on a phone and without scripts), and the list is
renumbered 10, 20, 30… in the order they see it. Renumbering every time keeps gaps for the
next move and never leaves two rules tied.
"""

from __future__ import annotations

STEP = 10


def _rows(qs) -> list:
    return list(qs.order_by("priority", "created_at", "pk"))


def renumber(qs, ordered_pks=None) -> list:
    """Give ``qs``'s rows priorities 10, 20, … in ``ordered_pks`` order (rows it doesn't
    name keep their relative order, after the named ones). Returns the rows in order.
    Only rows in ``qs`` are touched, so another account's pk in the list is ignored."""
    rows = _rows(qs)
    if ordered_pks:
        wanted = []
        for raw in ordered_pks:
            try:
                wanted.append(int(raw))
            except (TypeError, ValueError):
                continue
        rank = {pk: i for i, pk in enumerate(dict.fromkeys(wanted))}
        before = {r.pk: i for i, r in enumerate(rows)}
        rows.sort(key=lambda r: (rank.get(r.pk, len(rank)), before[r.pk]))
    changed = []
    for i, row in enumerate(rows):
        priority = (i + 1) * STEP
        if row.priority != priority:
            row.priority = priority
            changed.append(row)
    if changed:
        type(rows[0]).objects.bulk_update(changed, ["priority"])
    return rows


def move(qs, pk, direction: str) -> bool:
    """Move one row up or down a place. False if it isn't there or can't move further."""
    rows = _rows(qs)
    index = next((i for i, r in enumerate(rows) if r.pk == int(pk)), None)
    if index is None:
        return False
    other = index - 1 if direction == "up" else index + 1
    if direction not in ("up", "down") or not 0 <= other < len(rows):
        return False
    rows[index], rows[other] = rows[other], rows[index]
    renumber(qs, [r.pk for r in rows])
    return True


def next_priority(qs) -> int:
    """The priority for a new rule: after every existing one."""
    last = qs.order_by("-priority").values_list("priority", flat=True).first()
    return (last or 0) + STEP
