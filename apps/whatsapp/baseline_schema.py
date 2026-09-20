"""Contract for the Step 0a "before" baseline snapshot (``baseline_conversations``).

The snapshot is a measurement contract: the command validates every snapshot
against :func:`validate_snapshot` before writing it, and the tests import this
same module, so the contract cannot drift from the implementation. Any change
to a field, its meaning, or a bucket boundary requires bumping
``SCHEMA_VERSION``.

Validation is dependency-free on purpose (no pydantic in the project).
"""

SCHEMA_VERSION = "1.0"

BUSINESS_HOURS_ADJUSTMENT = "none"
SOURCE = "MessageLog"

# Exclusive, non-overlapping first-response buckets (seconds).
#   lt_5m      0s <= r < 5m
#   5m_to_1h   5m <= r < 1h
#   1h_to_24h  1h <= r < 24h
#   gte_24h    r >= 24h
BUCKET_NAMES = ("lt_5m", "5m_to_1h", "1h_to_24h", "gte_24h")
BUCKET_UPPER_BOUNDS_SECONDS = (5 * 60, 60 * 60, 24 * 60 * 60)  # gte_24h is unbounded
PERCENTILE_MIN_SAMPLES = 5

INDETERMINATE_REASONS = (
    "no_outbound_logged_for_account",
    "outbound_not_delivered",
    "outbound_created_before_enquiry",
    "within_grace_period",
)

LIMITATIONS = (
    "Business-hours and timezone adjustments are not applied in this baseline "
    "(calendar time only).",
    "MessageLog has no author field: automated replies (auto-reply, workflow "
    "send_whatsapp, templates) cannot be told apart from human replies, so "
    "'answered' may overstate human responsiveness.",
    "Delivery is not seen: sent, delivered and read all count as answered. A "
    "delivered-but-unseen message, or a template sent into a conversation that "
    "WhatsApp later closed, still counts as answered.",
    "Enquiry counts depend on the enquiry gap (see the per-gap results).",
    "Exchanges are delimited only by silence (no message in either direction "
    "for the enquiry gap), not by WhatsApp conversation boundaries. Exchanges "
    "that the business started (first message outbound) and STOP/START "
    "keyword messages are not counted as enquiries.",
    "conversion_cohort is descriptive: enquiries whose contact later acquired a "
    "lead, deal or order after the enquiry within the window. It is not "
    "attribution and not a causal conversion rate.",
    "Source is MessageLog only; conversations.Message is deliberately not read.",
)

ENQUIRY_KEYS = {
    "inbound", "answered", "unanswered", "never_answered", "indeterminate",
    "indeterminate_reasons",
}
RESPONSE_TIME_KEYS = set(BUCKET_NAMES) | {"median_seconds", "p90_seconds"}
CONVERSION_KEYS = {"enquiries_with_lead", "enquiries_with_deal", "enquiries_with_order"}
DATA_QUALITY_KEYS = {"inbound_rows", "outbound_rows", "outbound_failed", "outbound_template"}
RESULT_KEYS = {
    "enquiry_gap_hours", "enquiries", "response_time", "conversion_cohort", "data_quality",
}
METADATA_KEYS = {
    "generated_at", "git_commit", "since", "until", "grace_hours", "timezone",
    "business_hours_adjustment", "source",
}
TOP_LEVEL_KEYS = {"schema_version", "metadata", "accounts", "limitations"}


class SnapshotSchemaError(ValueError):
    """The snapshot does not satisfy the v1.0 contract."""


def _fail(path: str, message: str):
    raise SnapshotSchemaError(f"{path}: {message}")


def _keys(path: str, obj, expected: set):
    if not isinstance(obj, dict):
        _fail(path, "must be an object")
    missing, extra = expected - obj.keys(), obj.keys() - expected
    if missing or extra:
        _fail(path, f"missing={sorted(missing)} unexpected={sorted(extra)}")


def _count(path: str, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(path, f"must be a non-negative integer, got {value!r}")


def _nullable_number(path: str, value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        _fail(path, f"must be a number or null, got {value!r}")


def validate_result(path: str, result: dict) -> None:
    _keys(path, result, RESULT_KEYS)
    _count(f"{path}.enquiry_gap_hours", result["enquiry_gap_hours"])

    enq = result["enquiries"]
    _keys(f"{path}.enquiries", enq, ENQUIRY_KEYS)
    for key in ENQUIRY_KEYS - {"indeterminate_reasons"}:
        _count(f"{path}.enquiries.{key}", enq[key])
    reasons = enq["indeterminate_reasons"]
    if not isinstance(reasons, dict):
        _fail(f"{path}.enquiries.indeterminate_reasons", "must be an object")
    for reason, n in reasons.items():
        if reason not in INDETERMINATE_REASONS:
            _fail(f"{path}.enquiries.indeterminate_reasons", f"unknown reason {reason!r}")
        _count(f"{path}.enquiries.indeterminate_reasons.{reason}", n)

    if enq["answered"] + enq["unanswered"] + enq["indeterminate"] != enq["inbound"]:
        _fail(f"{path}.enquiries", "answered + unanswered + indeterminate must equal inbound")
    if enq["never_answered"] > enq["unanswered"]:
        _fail(f"{path}.enquiries", "never_answered must be a subset of unanswered")
    if sum(reasons.values()) != enq["indeterminate"]:
        _fail(f"{path}.enquiries", "indeterminate_reasons must sum to indeterminate")

    rt = result["response_time"]
    _keys(f"{path}.response_time", rt, RESPONSE_TIME_KEYS)
    for name in BUCKET_NAMES:
        _count(f"{path}.response_time.{name}", rt[name])
    if sum(rt[name] for name in BUCKET_NAMES) != enq["answered"]:
        _fail(f"{path}.response_time", "buckets must sum to answered")
    for key in ("median_seconds", "p90_seconds"):
        _nullable_number(f"{path}.response_time.{key}", rt[key])
        if enq["answered"] < PERCENTILE_MIN_SAMPLES and rt[key] is not None:
            _fail(f"{path}.response_time.{key}", f"must be null when answered < {PERCENTILE_MIN_SAMPLES}")

    conv = result["conversion_cohort"]
    _keys(f"{path}.conversion_cohort", conv, CONVERSION_KEYS)
    for key in CONVERSION_KEYS:
        _count(f"{path}.conversion_cohort.{key}", conv[key])
        if conv[key] > enq["inbound"]:
            _fail(f"{path}.conversion_cohort.{key}", "cannot exceed enquiries")

    dq = result["data_quality"]
    _keys(f"{path}.data_quality", dq, DATA_QUALITY_KEYS)
    for key in DATA_QUALITY_KEYS:
        _count(f"{path}.data_quality.{key}", dq[key])


def validate_snapshot(snapshot: dict) -> None:
    """Raise :class:`SnapshotSchemaError` unless ``snapshot`` satisfies the contract."""
    _keys("snapshot", snapshot, TOP_LEVEL_KEYS | ({"total"} & snapshot.keys()))
    if snapshot["schema_version"] != SCHEMA_VERSION:
        _fail("schema_version", f"expected {SCHEMA_VERSION!r}")

    meta = snapshot["metadata"]
    _keys("metadata", meta, METADATA_KEYS)
    if meta["business_hours_adjustment"] != BUSINESS_HOURS_ADJUSTMENT:
        _fail("metadata.business_hours_adjustment", f"must be {BUSINESS_HOURS_ADJUSTMENT!r}")
    if meta["source"] != SOURCE:
        _fail("metadata.source", f"must be {SOURCE!r}")

    if not isinstance(snapshot["accounts"], list):
        _fail("accounts", "must be a list")
    for i, entry in enumerate(snapshot["accounts"]):
        path = f"accounts[{i}]"
        _keys(path, entry, {"account", "results"})
        _keys(f"{path}.account", entry["account"], {"id", "slug"})
        if not isinstance(entry["results"], list) or not entry["results"]:
            _fail(f"{path}.results", "must be a non-empty list")
        for j, result in enumerate(entry["results"]):
            validate_result(f"{path}.results[{j}]", result)

    if "total" in snapshot:
        _keys("total", snapshot["total"], {"results"})
        for j, result in enumerate(snapshot["total"]["results"]):
            validate_result(f"total.results[{j}]", result)

    if list(snapshot["limitations"]) != list(LIMITATIONS):
        _fail("limitations", "must be the mandatory limitations for this schema version")
