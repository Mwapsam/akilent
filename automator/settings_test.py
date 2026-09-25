
import os

from cryptography.fernet import Fernet

os.environ["USE_SQLITE"] = "1"
os.environ["DEBUG"] = "true"
os.environ["DJANGO_SECRET_KEY"] = "test-secret-key"
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["FLUTTERWAVE_SECRET_KEY"] = "FLWSECK_TEST-testkey"
os.environ["FLUTTERWAVE_WEBHOOK_HASH"] = "test-hash"
os.environ["WHATSAPP_ENABLED"] = "false"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test_verify_token"
os.environ["WHATSAPP_APP_SECRET"] = "test_app_secret"

from automator.settings import *  # noqa: F401,F403,E402

# Capture mail in django.core.mail.outbox instead of hitting SMTP.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Run Celery tasks inline (no broker needed) so `.delay()` calls in views —
# e.g. verification emails, transactional sends — execute synchronously and
# stay observable via mail.outbox in tests.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# No real Redis available in tests — LocMemCache is fine since each test
# process/run doesn't need throttling state shared across workers.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}

# Force the SES rate limiter onto its in-process fallback (no Redis probe).
REDIS_URL = ""

# Don't fire the (network) WhatsApp read-receipt task from inbound-message tests.
WHATSAPP_MARK_READ_ENABLED = False

# Tests never talk to a real AI service or see a real key from .env.
AI_PROVIDER_BACKEND = "none"
OLLAMA_API_KEY = ""
OLLAMA_BASE_URL = "http://ollama.invalid"
