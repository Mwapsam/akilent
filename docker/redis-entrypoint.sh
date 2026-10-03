#!/bin/sh
# Write the Redis password to a file so it never appears in the redis-server
# argument list (visible via docker inspect, ps aux, Docker events).
# REDIS_PASSWORD is injected as a container environment variable by docker-compose.
set -e

PASS_FILE="/tmp/.redis-requirepass"

if [ -n "${REDIS_PASSWORD:-}" ]; then
    printf '%s' "$REDIS_PASSWORD" > "$PASS_FILE"
    chmod 600 "$PASS_FILE"
    exec redis-server --requirepass-file "$PASS_FILE"
else
    exec redis-server
fi
