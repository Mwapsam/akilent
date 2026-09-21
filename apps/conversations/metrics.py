"""Engineering counters for the conversation spine (not customer-facing analytics).

There is no metrics backend in this project, so counters are structured log lines on
the ``akilent.metrics`` logger: ``metric name=<name> value=<n> tag=...``. They exist so
we can tell whether the spine is trustworthy (projection created/duplicate/failed,
status updates applied/failed, status lag) and are cheap to aggregate from logs.
"""
import logging

logger = logging.getLogger("akilent.metrics")


def incr(name: str, value: float = 1, **tags) -> None:
    extra = " ".join(f"{k}={v}" for k, v in sorted(tags.items()))
    logger.info("metric name=%s value=%s %s", name, value, extra)
