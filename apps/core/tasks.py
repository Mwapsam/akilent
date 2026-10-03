"""Worker heartbeats for the Pilot Command Center.

Beat runs ``send_heartbeats`` every minute; it sends one ``heartbeat`` through every queue in
``settings.WORKER_QUEUES``. A queue whose worker is down, or that no worker consumes, stops
updating its cache key, and the Command Center shows it as late.
"""

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from celery import shared_task

KEY = "ops:heartbeat:{}"
TTL = 60 * 60 * 24

# Written every 2 minutes with a 5-minute TTL. Measures beat+worker liveness: beat must be alive
# to schedule the task AND a worker must be alive to execute it. If either dies the key expires
# and /healthz reports "beat": false, making the failure detectable by uptime monitors.
BEAT_HEARTBEAT_KEY = "ops:beat:heartbeat"
BEAT_HEARTBEAT_TTL = 300  # 5 minutes


@shared_task(queue="celery")
def send_heartbeats() -> None:
    for queue in settings.WORKER_QUEUES:
        heartbeat.apply_async((queue,), queue=queue, expires=300)


@shared_task
def heartbeat(queue: str) -> None:
    cache.set(KEY.format(queue), timezone.now().isoformat(), TTL)


@shared_task(queue="celery")
def beat_heartbeat() -> None:
    """Refresh the beat+worker liveness key. Both beat (schedules) and a worker (executes) must
    be alive for this to run. Key expires after 5 min; /healthz reports "beat": false when gone."""
    cache.set(BEAT_HEARTBEAT_KEY, timezone.now().isoformat(), BEAT_HEARTBEAT_TTL)


@shared_task(queue="celery")
def delete_closed_accounts() -> int:
    """Delete businesses an operator closed more than 30 days ago (see console.data)."""
    from apps.core.console.data import delete_due_accounts

    return delete_due_accounts()


def last_seen(queue: str):
    from django.utils.dateparse import parse_datetime

    raw = cache.get(KEY.format(queue))
    return parse_datetime(raw) if raw else None
