import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core ---

DEBUG = os.getenv("DEBUG", "False").lower() == "true"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY and not DEBUG:
    raise ValueError("DJANGO_SECRET_KEY is required in production")

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

_extra_origins = os.getenv("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _extra_origins.split(",") if o.strip()]

# --- Applications ---

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.core",
    "apps.accounts",
    "apps.whatsapp",
    "apps.email",
    "apps.logs",
    "apps.contacts",
    "apps.events",
    "drf_spectacular",
    "apps.automation",
    "apps.billing",
    "apps.api",
    "apps.internal_debug",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serve built static assets (CSS/JS/fonts) compressed + cache-busted.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.RequestIdMiddleware",
]

ROOT_URLCONF = "automator.urls"
WSGI_APPLICATION = "automator.wsgi.application"

# --- Templates ---

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site_context",
                "apps.accounts.context_processors.onboarding_status",
            ],
        },
    },
]

# --- Database ---

if os.getenv("USE_SQLITE", "0") == "1":
    # Local dev convenience: run without Postgres/docker.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "automator"),
            "USER": os.getenv("POSTGRES_USER", "automator"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD"),
            "HOST": os.getenv("POSTGRES_HOST", "db"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }

    if not DEBUG and not DATABASES["default"]["PASSWORD"]:
        raise ValueError("POSTGRES_PASSWORD is required")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/auth/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/auth/login/"

# --- Auth ---

# Email is the login credential; ModelBackend stays as a fallback (Django
# admin and any username-based auth keep working unchanged).
AUTHENTICATION_BACKENDS = [
    "apps.accounts.backends.EmailBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalization ---

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files ---

AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "")
AWS_CLOUDFRONT_DOMAIN = os.environ.get("AWS_CLOUDFRONT_DOMAIN", "")
CLOUDFRONT_ID = os.environ.get("AWS_CLOUDFRONT_ID", "")

USE_S3_STORAGE = bool(
    (not DEBUG)
    and AWS_ACCESS_KEY_ID
    and AWS_SECRET_ACCESS_KEY
    and AWS_STORAGE_BUCKET_NAME
)
SERVE_STATIC_MEDIA_LOCALLY = not USE_S3_STORAGE
_enforce_https_env = os.environ.get("ENFORCE_HTTPS")
if _enforce_https_env is None:
    ENFORCE_HTTPS = not SERVE_STATIC_MEDIA_LOCALLY
else:
    ENFORCE_HTTPS = _enforce_https_env.lower() == "true"

if USE_S3_STORAGE:
    STATICFILES_DIRS = [BASE_DIR / "static"]

    # Buckets created since April 2023 default to Object Ownership "Bucket
    # owner enforced", which disables ACLs outright — sending one on upload
    # (the old public-read default) makes every PutObject fail with
    # AccessControlListNotSupported. Public read comes from the bucket policy
    # instead; don't set an ACL at all.
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = False

    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": "max-age=31536000, public",
    }

    AWS_S3_CUSTOM_DOMAIN = (
        AWS_CLOUDFRONT_DOMAIN or f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com"
    )

    STATICFILES_LOCATION = "static"
    STATIC_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/{STATICFILES_LOCATION}/"

    MEDIAFILES_LOCATION = "media"
    MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/{MEDIAFILES_LOCATION}/"

    STORAGES = {
        "default": {"BACKEND": "automator.storage_backends.mediaRootS3Boto3Storage"},
        "staticfiles": {"BACKEND": "automator.storage_backends.StaticToS3Storage"},
    }
else:
    STATIC_URL = "/static/"
    STATICFILES_DIRS = [BASE_DIR / "static"]
    STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

    MEDIA_URL = "/media/"
    MEDIA_ROOT = os.path.join(BASE_DIR, "media")

    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }


BASE_DOMAIN = os.getenv("BASE_DOMAIN", "localhost:8000")


# --- Encryption ---

FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY")
if not FIELD_ENCRYPTION_KEY:
    raise ValueError(
        "FIELD_ENCRYPTION_KEY must be set. "
        "Generate one using Fernet.generate_key()."
    )
FIELD_ENCRYPTION_KEYS = [FIELD_ENCRYPTION_KEY]

# --- Feature flags ---
# Soft-disable the non-email verticals. Apps stay in INSTALLED_APPS (so models,
# migrations and signals remain intact); these flags gate their URLs, nav,
# Celery schedule and startup secret validation. Flip to True to re-enable.

WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "False").lower() == "true"

# --- WhatsApp ---

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")

# Embedded Signup (Tech Provider onboarding). app_id + config_id come from the
# onboarding link Meta gives you in the App dashboard.
WHATSAPP_APP_ID = os.getenv("WHATSAPP_APP_ID", "")
WHATSAPP_CONFIG_ID = os.getenv("WHATSAPP_CONFIG_ID", "")
WHATSAPP_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")

# Inbound keyword handling for messaging consent. A single-word inbound text
# matching (case-insensitively) one of these opts the contact out / back in.
WHATSAPP_STOP_KEYWORDS = [
    k.strip().upper()
    for k in os.getenv(
        "WHATSAPP_STOP_KEYWORDS", "STOP,UNSUBSCRIBE,CANCEL,END,QUIT"
    ).split(",")
    if k.strip()
]
WHATSAPP_START_KEYWORDS = [
    k.strip().upper()
    for k in os.getenv(
        "WHATSAPP_START_KEYWORDS", "START,UNSTOP,SUBSCRIBE"
    ).split(",")
    if k.strip()
]
WHATSAPP_OPT_OUT_CONFIRMATION = os.getenv(
    "WHATSAPP_OPT_OUT_CONFIRMATION",
    "You've been unsubscribed and won't receive further messages. "
    "Reply START to opt back in.",
)

# Largest inbound media file we will pull from Meta and store (bytes).
# Meta's own ceiling is 100 MB for documents; smaller by default.
WHATSAPP_MAX_MEDIA_BYTES = int(os.getenv("WHATSAPP_MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))

# Send read receipts (blue ticks) for inbound messages.
WHATSAPP_MARK_READ_ENABLED = os.getenv("WHATSAPP_MARK_READ_ENABLED", "True").lower() == "true"

if not DEBUG and WHATSAPP_ENABLED:
    if not WHATSAPP_VERIFY_TOKEN:
        raise ValueError("WHATSAPP_VERIFY_TOKEN is missing")
    if not WHATSAPP_APP_SECRET:
        raise ValueError("WHATSAPP_APP_SECRET is missing")


# --- Slack ---
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

STALWART_API_BASE = os.getenv("STALWART_API_BASE", "")
STALWART_API_KEY = os.getenv("STALWART_API_KEY", "")

# --- Internal debug API (apps.internal_debug) ---
# Off by default even when the token is set — both must be true. Intended to
# be flipped on only for the duration of an active debugging session, driven
# by an internal MCP tool, never a customer-facing surface.
INTERNAL_DEBUG_ENABLED = os.getenv("INTERNAL_DEBUG_ENABLED", "False").lower() == "true"
INTERNAL_DEBUG_TOKEN = os.getenv("INTERNAL_DEBUG_TOKEN", "")

# Provider selector — swap the implementation without touching business logic.
# These are the boot-time defaults; the MailProviderSettings singleton (edited by
# superadmins) overrides them at runtime.
MAIL_PROVIDER_BACKEND = os.getenv("MAIL_PROVIDER_BACKEND", "stalwart")
EMAIL_SEND_PROVIDER_BACKEND = os.getenv("EMAIL_SEND_PROVIDER_BACKEND", "smtp")

# AWS SES — credentials come from the standard AWS_* env vars / IAM role; these
# name the region and (optional) tracking resources. Also settable live via
# MailProviderSettings.
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_S3_REGION_NAME", "us-east-1"))
SES_CONFIGURATION_SET = os.getenv("SES_CONFIGURATION_SET", "")
SES_SNS_TOPIC_ARN = os.getenv("SES_SNS_TOPIC_ARN", "")

_USING_SES = "ses" in (MAIL_PROVIDER_BACKEND, EMAIL_SEND_PROVIDER_BACKEND)

# Public domain used to build absolute tracking URLs in outgoing emails.
BASE_DOMAIN = os.getenv("BASE_DOMAIN", (ALLOWED_HOSTS[0] if ALLOWED_HOSTS else "localhost"))

# SMTP submission credentials (Stalwart port 587) used to actually send mail.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "True").lower() == "true"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@localhost")

if not DEBUG:
    if _USING_SES:
        # SES path: require AWS credentials + region. The Stalwart/SMTP vars are
        # not needed (system mail also goes through the SES send provider).
        if not AWS_ACCESS_KEY_ID:
            raise ValueError("AWS_ACCESS_KEY_ID is required when using the SES backend")
        if not AWS_SECRET_ACCESS_KEY:
            raise ValueError("AWS_SECRET_ACCESS_KEY is required when using the SES backend")
        if not AWS_REGION:
            raise ValueError("AWS_REGION is required when using the SES backend")
        if not SES_SNS_TOPIC_ARN:
            import warnings

            warnings.warn(
                "SES_SNS_TOPIC_ARN is not set — bounce/complaint notifications "
                "will not be ingested until it is configured.",
                RuntimeWarning,
            )
    else:
        if not STALWART_API_BASE:
            raise ValueError("STALWART_API_BASE is required")
        if not STALWART_API_KEY:
            raise ValueError("STALWART_API_KEY is required")
        if not EMAIL_HOST:
            raise ValueError("EMAIL_HOST is required")
        if not EMAIL_HOST_USER:
            raise ValueError("EMAIL_HOST_USER is required")
        if not EMAIL_HOST_PASSWORD:
            raise ValueError("EMAIL_HOST_PASSWORD is required")

# The external Stalwart submission endpoint, shown to customers setting up
# per-tenant SMTP relay (apps.api / apps.email SmtpCredential docs).
SMTP_RELAY_HOST = os.getenv("SMTP_RELAY_HOST", EMAIL_HOST)
SMTP_RELAY_PORT = int(os.getenv("SMTP_RELAY_PORT", str(EMAIL_PORT)))

# --- REST API (Developer Platform) ---
# Public, versioned API surface (apps.api). Auth/permission classes are set
# per-view rather than as DRF defaults, so this never touches the
# session-authenticated dashboard views in apps.email/apps.accounts/etc.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_VERSION": "v1",
    "DEFAULT_THROTTLE_CLASSES": [],
    "EXCEPTION_HANDLER": "apps.api.errors.custom_exception_handler",
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Akilent API",
    "DESCRIPTION": "Send transactional and bulk email, manage templates, and observe delivery.",
    "VERSION": "v1",
    "SERVERS": [{"url": "/api/v1"}],
    "SCHEMA_PATH_PREFIX": r"/api",
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    "PREPROCESSING_HOOKS": ["apps.api.schema.only_public_api"],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "apps.api.schema.strip_version_param",
    ],
}

# --- Flutterwave ---

FLUTTERWAVE_SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY")
FLUTTERWAVE_WEBHOOK_HASH = os.getenv("FLUTTERWAVE_WEBHOOK_HASH")
FLUTTERWAVE_CURRENCY = os.getenv("FLUTTERWAVE_CURRENCY", "USD")

# --- Cache ---
# Backs DRF request-rate throttling and the API-key bad-attempt lockout
# counter (apps.api.authentication) — must be shared across gunicorn workers,
# so LocMemCache (Django's default) won't do outside of tests. Redis is
# already a dependency for Celery; a separate DB index keeps the keyspaces apart.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("REDIS_CACHE_URL", "redis://redis:6379/1"),
    }
}

# --- Celery ---

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@rabbitmq:5672//")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

# Shared Redis used for the distributed SES send rate limiter (falls back to the
# cache URL, then the Celery result backend). If none resolve to a redis:// URL
# the limiter degrades to a per-process token bucket.
REDIS_URL = os.getenv(
    "REDIS_URL",
    os.getenv("REDIS_CACHE_URL", CELERY_RESULT_BACKEND),
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE


CELERY_TASK_ROUTES = {
    # Accounts
    "apps.accounts.tasks.send_verification_email": {"queue": "celery"},
    # Email provisioning
    "apps.email.tasks.send_email": {"queue": "outbound"},
    "apps.email.tasks.send_bulk_recipient_email": {"queue": "outbound"},
    "apps.email.tasks.dispatch_campaign": {"queue": "campaigns"},
    "apps.email.tasks.rotate_dkim_async": {"queue": "email"},
    "apps.email.tasks.provision_domain_async": {"queue": "email"},
    "apps.email.tasks.deliver_webhook": {"queue": "webhooks"},
    # Automation (rule evaluation)
    "apps.automation.tasks.evaluate_rules_for_message": {"queue": "automation"},
    # Maintenance
    "apps.email.tasks.prune_email_logs": {"queue": "celery"},
    "apps.email.tasks.prune_tracking_tokens": {"queue": "celery"},
    "apps.email.tasks.prune_provisioning_jobs": {"queue": "celery"},
    "apps.email.tasks.reverify_pending_domains": {"queue": "celery"},
    "apps.email.tasks.alert_on_failure_spike": {"queue": "celery"},
    "apps.email.tasks.snapshot_deliverability": {"queue": "celery"},
    "apps.automation.tasks.run_due_workflows": {"queue": "celery"},
    "apps.logs.tasks.prune_message_events": {"queue": "celery"},
    "apps.logs.tasks.prune_idempotency_records": {"queue": "celery"},
    "apps.logs.tasks.prune_api_requests": {"queue": "celery"},
    "apps.logs.tasks.reconcile_message_stats": {"queue": "celery"},
}

CELERY_BEAT_SCHEDULE = {
    "expire-trials": {
        "task": "apps.billing.tasks.expire_trials",
        "schedule": 3600.0,
    },
    "prune-email-logs": {
        "task": "apps.email.tasks.prune_email_logs",
        "schedule": 86400.0,
    },
    "prune-tracking-tokens": {
        "task": "apps.email.tasks.prune_tracking_tokens",
        "schedule": 86400.0,
    },
    "prune-provisioning-jobs": {
        "task": "apps.email.tasks.prune_provisioning_jobs",
        "schedule": 86400.0,
    },
    "reverify-pending-domains": {
        "task": "apps.email.tasks.reverify_pending_domains",
        "schedule": 900.0,  # every 15 min — pick up customer DNS changes
    },
    "alert-on-failure-spike": {
        "task": "apps.email.tasks.alert_on_failure_spike",
        "schedule": 900.0,  # every 15 min
    },
    "prune-message-events": {
        "task": "apps.logs.tasks.prune_message_events",
        "schedule": 86400.0,
    },
    "prune-idempotency-records": {
        "task": "apps.logs.tasks.prune_idempotency_records",
        "schedule": 3600.0,
    },
    "prune-api-requests": {
        "task": "apps.logs.tasks.prune_api_requests",
        "schedule": 86400.0,
    },
    "reconcile-message-stats": {
        "task": "apps.logs.tasks.reconcile_message_stats",
        "schedule": 86400.0,
    },
    "snapshot-deliverability": {
        "task": "apps.email.tasks.snapshot_deliverability",
        "schedule": 86400.0,
    },
    "run-due-workflows": {
        "task": "apps.automation.tasks.run_due_workflows",
        "schedule": 60.0,  # workflow wait-timers resolve at minute granularity
    },
}

if WHATSAPP_ENABLED:
    CELERY_TASK_ROUTES.update({
        "apps.whatsapp.tasks.process_whatsapp_event": {"queue": "whatsapp"},
        "apps.whatsapp.tasks.drain_outbound_queue": {"queue": "outbound"},
        "apps.whatsapp.tasks.mark_read": {"queue": "whatsapp"},
    })
    CELERY_BEAT_SCHEDULE.update({
        "close-expired-conversations": {
            "task": "apps.whatsapp.tasks.close_expired_conversations",
            "schedule": 3600.0,
        },
        "drain-outbound-queue": {
            "task": "apps.whatsapp.tasks.drain_outbound_queue",
            "schedule": 10.0,
        },
        "download-media": {
            "task": "apps.whatsapp.tasks.download_media",
            "schedule": 60.0,
        },
        "sync-whatsapp-templates": {
            "task": "apps.whatsapp.tasks.sync_templates",
            "schedule": 1800.0,  # every 30 min — pull Meta approval status
        },
        "alert-on-whatsapp-failure-spike": {
            "task": "apps.whatsapp.tasks.alert_on_whatsapp_failure_spike",
            "schedule": 900.0,  # every 15 min
        },
    })

# --- Logging ---

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {
            "()": "apps.core.log_filters.RequestIdFilter",
        },
    },
    "formatters": {
        "standard": {
            "format": "[{levelname}] {asctime} {name}:{lineno} [{request_id}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["request_id"],
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "automator.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "standard",
            "filters": ["request_id"],
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "DEBUG" if DEBUG else "INFO",
    },
}

# --- Production security ---

if not DEBUG:
    # SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_TRUSTED_ORIGINS = [
        "https://akilent.com",
        "https://www.akilent.com",
    ]

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
    )
