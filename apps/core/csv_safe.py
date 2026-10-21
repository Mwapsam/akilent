"""Spreadsheet-safe CSV cells.

A cell starting with ``=``, ``+``, ``-`` or ``@`` is run as a formula by Excel
and Sheets, so an export of customer-typed text could execute it. Such cells
get a leading apostrophe. Phone numbers (``+260...``) and negative numbers are
left alone: a sign followed by a digit is data, not a formula.
"""

from __future__ import annotations

_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    if text[:1] in _TRIGGERS and not text[1:2].isdigit():
        return "'" + text
    return text
