"""Resolve the fixed "Remind me" choices (R2.2) into a due datetime.

Deliberately only three options, per the plan's UX guardrail against turning
this into a general task system: 1h from now, tomorrow morning, or a
caller-picked time.
"""
from __future__ import annotations

import datetime

from django.utils import timezone
from django.utils.dateparse import parse_datetime

_TOMORROW_HOUR = 9


def resolve_due_at(choice: str, custom_value: str = "") -> datetime.datetime:
    now = timezone.now()
    if choice == "1h":
        return now + datetime.timedelta(hours=1)
    if choice == "tomorrow":
        tomorrow = (now + datetime.timedelta(days=1)).replace(
            hour=_TOMORROW_HOUR, minute=0, second=0, microsecond=0
        )
        return tomorrow
    if choice == "custom":
        parsed = parse_datetime(custom_value) if custom_value else None
        if parsed is None:
            raise ValueError("Pick a valid date and time.")
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed
    raise ValueError("Choose when to be reminded.")
