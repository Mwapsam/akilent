"""Value normalisation for echo-detection and conflict resolution.

All values are normalised to a canonical string before hashing
(shared with C's set_attributes coercion).
"""

from __future__ import annotations

import re

_E164 = re.compile(r"^\+\d{7,15}$")


def normalise_value(field: str, value: str) -> str:
    """Canonicalise a field value for hash comparison.

    - email: lowercase, stripped
    - phone: E.164 digits only
    - everything else: stripped
    """
    if value is None:
        return ""
    v = str(value).strip()
    if field == "email":
        return v.lower()
    if field == "phone":
        digits = re.sub(r"[^\d+]", "", v)
        return digits
    return v
