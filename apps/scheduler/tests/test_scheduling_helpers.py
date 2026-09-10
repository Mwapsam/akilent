"""apps.core.scheduling — timezone + recurrence helpers (no DB)."""
from datetime import datetime, timezone as _tz

import pytest

from apps.core.scheduling import (
    InvalidTimezone,
    next_occurrence,
    to_utc,
    validate_iana,
    validate_rrule,
)


def test_validate_iana_accepts_known_zone():
    assert validate_iana("America/New_York") == "America/New_York"


def test_validate_iana_rejects_junk():
    with pytest.raises(InvalidTimezone):
        validate_iana("Mars/Olympus")


def test_validate_iana_recipient_sentinel():
    with pytest.raises(InvalidTimezone):
        validate_iana("recipient")
    assert validate_iana("recipient", allow_recipient=True) == "recipient"


@pytest.mark.parametrize(
    "naive,zone,expected_utc_hour",
    [
        # 09:00 in New York in September = EDT (UTC-4) => 13:00 UTC
        (datetime(2026, 9, 15, 9, 0), "America/New_York", 13),
        # 09:00 in New York in December = EST (UTC-5) => 14:00 UTC
        (datetime(2026, 12, 15, 9, 0), "America/New_York", 14),
        # 09:00 India (UTC+5:30) => 03:30 UTC
        (datetime(2026, 6, 1, 9, 0), "Asia/Kolkata", 3),
    ],
)
def test_to_utc_wall_clock(naive, zone, expected_utc_hour):
    got = to_utc(naive, zone)
    assert got.tzinfo == _tz.utc
    assert got.hour == expected_utc_hour


def test_to_utc_aware_input_ignores_tz_arg():
    aware = datetime(2026, 9, 15, 9, 0, tzinfo=_tz.utc)
    assert to_utc(aware, "America/New_York").hour == 9


def test_next_occurrence_weekly_is_dst_safe():
    # Weekly Monday 09:00 New York, starting just before a fall-back DST change.
    after = to_utc(datetime(2026, 10, 30, 12, 0), "America/New_York")
    nxt = next_occurrence("FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
                          after=after, tz="America/New_York")
    # The next Monday is Nov 2 2026 — after the Nov 1 fall-back, so 09:00 EST = 14:00 UTC.
    assert nxt.hour == 14
    from zoneinfo import ZoneInfo
    local = nxt.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (9, 0)


def test_validate_rrule_rejects_sub_hour():
    with pytest.raises(ValueError):
        validate_rrule("FREQ=MINUTELY;INTERVAL=5")


def test_validate_rrule_accepts_daily():
    assert validate_rrule("FREQ=DAILY;BYHOUR=9") == "FREQ=DAILY;BYHOUR=9"
