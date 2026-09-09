import logging

from apps.core.request_context import get_request_id


class RequestIdFilter(logging.Filter):
    """Inject the ambient request id into every log record as ``request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id() or "-"
        return True
