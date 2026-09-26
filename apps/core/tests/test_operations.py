"""Operational guards: the health endpoint, and every Celery queue has a worker in production."""
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)


@pytest.mark.django_db
def test_healthz_is_public_and_reports_the_database_and_cache(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "db": True, "cache": True}


@pytest.mark.django_db
def test_healthz_fails_when_the_database_is_down(client):
    with patch("django.db.backends.utils.CursorWrapper.execute", side_effect=Exception("db down")):
        resp = client.get("/healthz")
    assert resp.status_code == 503 and resp.json()["db"] is False


def _routed_queues() -> set[str]:
    """Every queue a task can be sent to: settings routes (including WhatsApp-only ones) and
    ``queue="..."`` in task decorators and ``apply_async`` calls."""
    queues = set(re.findall(r'"queue":\s*"(\w+)"', (ROOT / "automator" / "settings.py").read_text()))
    for path in (ROOT / "apps").rglob("*.py"):
        if "tests" in path.parts:
            continue
        queues |= set(re.findall(r'queue\s*=\s*"(\w+)"', path.read_text(encoding="utf-8")))
    return queues | {"celery"}  # Celery's default queue for unrouted tasks


def _consumed_queues() -> set[str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    consumed = set()
    for service in compose["services"].values():
        command = service.get("command") or ""
        if "celery" in command and " worker" in command:
            match = re.search(r"-Q\s+(\S+)", command)
            consumed |= set(match.group(1).split(",")) if match else {"celery"}
    return consumed


def test_every_routed_queue_has_a_worker():
    missing = _routed_queues() - _consumed_queues()
    assert not missing, f"No worker in docker-compose.yml consumes: {sorted(missing)}"


def test_ai_has_its_own_worker_so_it_cannot_delay_whatsapp():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    workers = [s["command"] for s in compose["services"].values() if " worker" in (s.get("command") or "")]
    ai = [c for c in workers if re.search(r"-Q\s+\S*\bai\b", c)]
    assert len(ai) == 1 and "outbound" not in ai[0] and "whatsapp" not in ai[0]


def test_internal_services_are_not_published_to_the_internet():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name in ("db", "redis", "rabbitmq", "web"):
        for port in compose["services"][name].get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} on every interface"
