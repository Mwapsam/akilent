#!/bin/sh
set -e

# Production (DJANGO_ENV=production, written to .env by the deploy workflow) serves with gunicorn.
# Migrations and collectstatic run once per deploy in .github/workflows/deploy.yml, not here,
# so a restart never re-uploads static files or races another container on migrations.
if [ "$DJANGO_ENV" = "production" ]; then
    echo "Starting gunicorn..."
    exec gunicorn automator.wsgi:application \
        --bind 0.0.0.0:8000 \
        --workers "${GUNICORN_WORKERS:-4}" \
        --timeout "${GUNICORN_TIMEOUT:-120}" \
        --graceful-timeout 30 \
        --keep-alive 5 \
        --log-level info \
        --access-logfile - \
        --error-logfile -
else
    echo "Applying migrations..."
    python manage.py migrate --noinput

    echo "Starting development server..."
    exec python manage.py runserver 0.0.0.0:8000
fi
