"""Worker heartbeats for the Pilot Command Center.

Beat runs ``send_heartbeats`` every minute; it sends one ``heartbeat`` through every queue in
``settings.WORKER_QUEUES``. A queue whose worker is down, or that no worker consumes, stops
updating its cache key, and the Command Center shows it as late.
"""
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

KEY = "ops:heartbeat:{}"
TTL = 60 * 60 * 24


@shared_task(queue="celery")
def send_heartbeats() -> None:
    for queue in settings.WORKER_QUEUES:
        heartbeat.apply_async((queue,), queue=queue, expires=300)


@shared_task
def heartbeat(queue: str) -> None:
    cache.set(KEY.format(queue), timezone.now().isoformat(), TTL)


def last_seen(queue: str):
    from django.utils.dateparse import parse_datetime

    raw = cache.get(KEY.format(queue))
    return parse_datetime(raw) if raw else None
