#!/bin/sh
set -e

# Serves only apps/core/views_events.py's SSE stream (see docker-compose.yml's `events` service
# and nginx/conf.d's /events/ location) on ASGI, so a long-lived streaming connection per browser
# tab never ties up a WSGI worker from the main `web` service (docker/entrypoint.sh). No
# migrations here — `web` already runs them; this process only reads sessions/memberships and
# relays Redis pub/sub, both already in place by the time it starts.
if [ "$DJANGO_ENV" = "production" ]; then
    echo "Starting events (gunicorn + uvicorn worker)..."
    exec gunicorn automator.asgi:application \
        -k uvicorn_worker.UvicornWorker \
        --bind 0.0.0.0:8001 \
        --workers "${EVENTS_WORKERS:-2}" \
        --timeout "${GUNICORN_TIMEOUT:-120}" \
        --graceful-timeout 30 \
        --log-level info \
        --access-logfile - \
        --error-logfile -
else
    echo "Starting events (uvicorn, dev)..."
    exec python -m uvicorn automator.asgi:application --host 0.0.0.0 --port 8001 --reload
fi
