"""HubSpot ↔ Akilent field mapping tables.

Lead status is akilent-owned (push only) in the first release.
The LEAD_STATUS_MAP values are the HubSpot allowed values for hs_lead_status.
"""

from __future__ import annotations

# Akilent Contact attribute key → HubSpot contact property name.
# Extend as needed; both sides use canonical normalised strings.
CONTACT_FIELD_MAP: dict[str, str] = {
    "first_name": "firstname",
    "last_name": "lastname",
    "email": "email",
    "phone": "phone",
}

# Akilent Lead.status → HubSpot hs_lead_status value.
# These are the standard HubSpot defaults; custom portals may differ.
LEAD_STATUS_MAP: dict[str, str] = {
    "new": "New",
    "contacted": "Attempted to Contact",
    "qualified": "In Progress",
    "lost": "Unqualified",
    "converted": "Open Deal",
}

HS_LEAD_STATUS_PROPERTY = "hs_lead_status"
