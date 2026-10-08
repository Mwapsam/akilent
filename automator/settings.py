import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core ---

DEBUG = os.getenv("DEBUG", "False").lower() == "true"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ValueError("DJANGO_SECRET_KEY is required in production")
    # Stable fallback so dev can boot without .env. Never use in production.
    SECRET_KEY = "dev-only-insecure-secret-key-change-me"

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]
if not DEBUG and set(ALLOWED_HOSTS) <= {"localhost", "127.0.0.1"}:
    raise ValueError("ALLOWED_HOSTS must be configured for production")

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
    "apps.conversations",
    "apps.crm",
    "apps.commerce",
    "apps.verticals",
    "drf_spectacular",
    "apps.automation",
    "apps.scheduler",
    "apps.billing",
    "apps.api",
    "apps.internal_debug",
    "apps.ai",
    "apps.insights",
    "apps.chatbot",
    "apps.support",
    "apps.instagram",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django.contrib.sitemaps",
    "apps.seo",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
    # Serve built static assets (CSS/JS/fonts) compressed + cache-busted.
    # WhiteNoise is only useful when static files are served locally.
    # When USE_S3_STORAGE is True it is omitted (see below).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.RequestIdMiddleware",
    "apps.core.middleware.ChannelGateMiddleware",
    "apps.core.middleware.BrowserSessionMiddleware",
    "apps.core.middleware.SuspendedAccountMiddleware",
    "apps.core.middleware.ViewAsReadOnlyMiddleware",
    # In-place navigation: falls back to a full page load whenever a fragment won't do.
    "apps.core.htmx.ShellMiddleware",
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
                "apps.core.context_processors.operator_context",
                "apps.accounts.context_processors.onboarding_status",
                "apps.accounts.context_processors.plan_features",
                "apps.accounts.context_processors.workspace_context",
                "apps.seo.context_processors.resolve_seo_context",
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
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "OPTIONS": {
                # Uncomment if your Postgres requires SSL
                # "sslmode": "require",
            },
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

# Sessions expire when the browser closes, with an absolute ceiling of 8 hours.
# This prevents tokens left in unattended browsers from staying valid indefinitely.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_AGE = 8 * 60 * 60  # 8 hours

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
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
    # Remove WhiteNoise when static assets live on S3/CloudFront
    MIDDLEWARE = [
        m for m in MIDDLEWARE if m != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]

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

# Single source of truth for the public domain
BASE_DOMAIN = os.getenv(
    "BASE_DOMAIN", (ALLOWED_HOSTS[0] if ALLOWED_HOSTS else "localhost")
)

# --- SEO ---

SITE_URL = os.getenv("SITE_URL", "https://akilent.com")

# Origins where system (Akilent-owned) chatbots may be deployed.
# SITE_URL is always implicitly included; add additional Akilent-controlled
# origins here (e.g. staging, marketing site, help centre).
PLATFORM_ALLOWED_ORIGINS: list[str] = [
    o.strip().rstrip("/")
    for o in os.getenv("PLATFORM_ALLOWED_ORIGINS", SITE_URL).split(",")
    if o.strip()
]

SEO_DEFAULTS = {
    "site_name": "Akilent",
    "default_title": "Customer Conversations, CRM & Business Automation",
    "default_description": "Akilent helps businesses manage customer conversations, contacts, sales, automation and insights from one workspace.",
    "title_suffix": "— Akilent",
    "og_image": "img/seo/og-default.png",
}

# Set to True only in production. Staging/dev emit noindex globally and robots.txt Disallow: /
SEO_ALLOW_INDEXING = os.getenv("SEO_ALLOW_INDEXING", "False").lower() == "true"

GOOGLE_ANALYTICS_ID = os.getenv("GOOGLE_ANALYTICS_ID", "")

# --- AI (optional) ---
# "none" switches every AI feature off; Akilent works fully without it.
#   ollama:    OLLAMA_BASE_URL (the hosted API by default) + OLLAMA_API_KEY; model default gpt-oss:120b
#   anthropic: ANTHROPIC_API_KEY; model default claude-sonnet-5
#   openai:    OPENAI_API_KEY (+ OPENAI_BASE_URL for compatible servers); AI_MODEL is required
# AI_MODEL overrides the provider's default. AI_MODEL_FAST, when set, answers simple messages
# (see apps.ai.router); leave it empty to use one model for everything.
AI_PROVIDER_BACKEND = os.getenv("AI_PROVIDER_BACKEND", "none")
AI_MODEL = os.getenv("AI_MODEL", "")
AI_MODEL_FAST = os.getenv("AI_MODEL_FAST", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")
# Emergency stop for AI sending on its own, for every business. Suggestions keep working.
AI_AUTONOMY_ENABLED = os.getenv("AI_AUTONOMY_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "45"))
AI_DAILY_CALL_LIMIT = int(os.getenv("AI_DAILY_CALL_LIMIT", "500"))

# Site-wide emergency brakes on WhatsApp sends across every business (a buggy loop, a compromised
# account, a runaway automation). Not plan limits and never shown to businesses: sends over the
# brake wait and go out once it lifts. 0 turns a window off.
WHATSAPP_PLATFORM_MAX_PER_MINUTE = int(
    os.getenv("WHATSAPP_PLATFORM_MAX_PER_MINUTE", "1200")
)
WHATSAPP_PLATFORM_MAX_PER_HOUR = int(
    os.getenv("WHATSAPP_PLATFORM_MAX_PER_HOUR", "30000")
)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")


# --- Encryption ---

FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY")
if not FIELD_ENCRYPTION_KEY:
    if DEBUG:
        # Derive a stable key from SECRET_KEY so encrypted-field data survives
        # server restarts in local development. Fernet.generate_key() at import
        # time would produce a new key on every restart, making any previously
        # encrypted rows unreadable (InvalidToken) and requiring a DB wipe.
        import base64
        import hashlib

        _raw = hashlib.sha256(SECRET_KEY.encode()).digest()  # type: ignore[name-defined]
        FIELD_ENCRYPTION_KEY = base64.urlsafe_b64encode(_raw).decode()
    else:
        raise ValueError(
            "FIELD_ENCRYPTION_KEY must be set. Generate one using Fernet.generate_key()."
        )
FIELD_ENCRYPTION_KEYS = [FIELD_ENCRYPTION_KEY]

# --- Feature flags ---
# Soft-disable the non-email verticals. Apps stay in INSTALLED_APPS (so models,
# migrations and signals remain intact); these flags gate their URLs, nav,
# Celery schedule and startup secret validation. Flip to True to re-enable.

WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "False").lower() == "true"
INSTAGRAM_ENABLED = os.getenv("INSTAGRAM_ENABLED", "True").lower() == "true"
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "True").lower() == "true"

# --- WhatsApp ---

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")

# Embedded Signup (Tech Provider onboarding). app_id + config_id come from the
# onboarding link Meta gives you in the App dashboard.
WHATSAPP_APP_ID = os.getenv("WHATSAPP_APP_ID", "")
WHATSAPP_CONFIG_ID = os.getenv("WHATSAPP_CONFIG_ID", "")
WHATSAPP_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")

# Instagram Business Login (OAuth connect flow)
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET")
INSTAGRAM_GRAPH_VERSION = os.getenv("INSTAGRAM_GRAPH_VERSION", "v21.0")
INSTAGRAM_VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN", "")

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
    for k in os.getenv("WHATSAPP_START_KEYWORDS", "START,UNSTOP,SUBSCRIBE").split(",")
    if k.strip()
]
WHATSAPP_OPT_OUT_CONFIRMATION = os.getenv(
    "WHATSAPP_OPT_OUT_CONFIRMATION",
    "You've been unsubscribed and won't receive further messages. "
    "Reply START to opt back in.",
)

# Largest inbound media file we will pull from Meta and store (bytes).
# Meta's own ceiling is 100 MB for documents; smaller by default.
WHATSAPP_MAX_MEDIA_BYTES = int(
    os.getenv("WHATSAPP_MAX_MEDIA_BYTES", str(25 * 1024 * 1024))
)

# Send read receipts (blue ticks) for inbound messages.
WHATSAPP_MARK_READ_ENABLED = (
    os.getenv("WHATSAPP_MARK_READ_ENABLED", "True").lower() == "true"
)

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

# Domain DNS checks ask each zone's own nameservers first, so records a
# tenant has just added are seen immediately rather than hidden by a
# cached "doesn't exist". Set to "1" to enable; defaults off to avoid
# cold-cache SOA-walk latency on every domain after a fresh deploy.
DNSCHECK_AUTHORITATIVE = os.getenv("DNSCHECK_AUTHORITATIVE", "0") == "1"

_USING_SES = "ses" in (MAIL_PROVIDER_BACKEND, EMAIL_SEND_PROVIDER_BACKEND)

# SMTP submission credentials (Stalwart port 587) used to actually send mail.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() == "true"
# Django raises ImproperlyConfigured when both TLS and SSL are True.
# An existing .env with EMAIL_USE_SSL=True + EMAIL_USE_TLS=True (the default)
# would crash on startup, so force SSL off when TLS is on.
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    EMAIL_USE_SSL = False
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@localhost")

if not DEBUG:
    if _USING_SES:
        # SES path: require AWS credentials + region. The Stalwart/SMTP vars are
        # not needed (system mail also goes through the SES send provider).
        if not AWS_ACCESS_KEY_ID:
            raise ValueError("AWS_ACCESS_KEY_ID is required when using the SES backend")
        if not AWS_SECRET_ACCESS_KEY:
            raise ValueError(
                "AWS_SECRET_ACCESS_KEY is required when using the SES backend"
            )
        if not AWS_REGION:
            raise ValueError("AWS_REGION is required when using the SES backend")
        # Bounce/complaint feedback is not optional. Without a configuration
        # set publishing to an SNS topic we send blind: no suppression of bad
        # addresses, no visibility of our own reputation. Refuse to boot rather
        # than start a worker that can only discover problems from AWS.
        if not SES_CONFIGURATION_SET:
            raise ValueError(
                "SES_CONFIGURATION_SET is required when using the SES backend — "
                "without it no bounce/complaint events are emitted"
            )
        if not SES_SNS_TOPIC_ARN:
            raise ValueError(
                "SES_SNS_TOPIC_ARN is required when using the SES backend — "
                "without it bounce/complaint notifications are never ingested"
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

# --- Stripe ---

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "USD")


# --- Cache ---
# Backs DRF request-rate throttling and the API-key bad-attempt lockout
# counter (apps.api.authentication) — must be shared across gunicorn workers,
# so LocMemCache (Django's default) won't do outside of tests. Redis is
# already a dependency for Celery; a separate DB index keeps the keyspaces apart.
def _redis_url(raw: str | None, default: str) -> str:
    url = raw or default
    if url and not url.startswith(("redis://", "rediss://", "unix://")):
        url = f"redis://{url}"
    return url


CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _redis_url(os.getenv("REDIS_CACHE_URL"), "redis://redis:6379/1"),
    }
}

# --- Celery ---

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@rabbitmq:5672//")
CELERY_RESULT_BACKEND = _redis_url(
    os.getenv("CELERY_RESULT_BACKEND"), "redis://redis:6379/0"
)

# Shared Redis used for the distributed SES send rate limiter (falls back to the
# cache URL, then the Celery result backend). If none resolve to a redis:// URL
# the limiter degrades to a per-process token bucket.
REDIS_URL = _redis_url(
    os.getenv("REDIS_URL", os.getenv("REDIS_CACHE_URL")),
    CELERY_RESULT_BACKEND,
)

# Off by default: the SSE endpoint (apps/core/views_events.py) is meant to run behind a
# *separate* ASGI process (docker-compose.yml's `events` service, routed by nginx at /events/),
# not the main WSGI web tier — a WSGI worker holding a long-lived streaming response open per
# browser tab would quietly shrink the pool available for ordinary requests. Flip this on only
# once that service is actually deployed and reachable; until then static/js templates never
# open the connection, and everything already works via polling regardless.
REALTIME_SSE_ENABLED = os.getenv("REALTIME_SSE_ENABLED", "False").lower() == "true"
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
    "apps.email.tasks.retry_held_email_recipients": {"queue": "outbound"},
    "apps.email.tasks.rotate_dkim_async": {"queue": "email"},
    "apps.email.tasks.provision_domain_async": {"queue": "email"},
    "apps.email.tasks.deliver_webhook": {"queue": "webhooks"},
    # Instagram
    "apps.instagram.tasks.drain_instagram_outbox": {"queue": "outbound"},
    # Automation (rule evaluation)
    "apps.automation.tasks.evaluate_rules_for_message": {"queue": "automation"},
    # Maintenance
    "apps.email.tasks.prune_email_logs": {"queue": "celery"},
    "apps.email.tasks.prune_tracking_tokens": {"queue": "celery"},
    "apps.email.tasks.prune_provisioning_jobs": {"queue": "celery"},
    "apps.email.tasks.reverify_pending_domains": {"queue": "celery"},
    "apps.email.tasks.recheck_verified_domains": {"queue": "celery"},
    "apps.email.tasks.alert_on_failure_spike": {"queue": "celery"},
    "apps.email.tasks.snapshot_deliverability": {"queue": "celery"},
    "apps.automation.tasks.run_due_workflows": {"queue": "celery"},
    "apps.automation.tasks.find_repeated_replies": {"queue": "celery"},
    "apps.logs.tasks.prune_message_events": {"queue": "celery"},
    "apps.logs.tasks.prune_idempotency_records": {"queue": "celery"},
    "apps.logs.tasks.prune_api_requests": {"queue": "celery"},
    "apps.logs.tasks.reconcile_message_stats": {"queue": "celery"},
    "apps.scheduler.tasks.run_due_jobs": {"queue": "scheduler"},
    "apps.scheduler.tasks.prune_scheduled_jobs": {"queue": "celery"},
}

CELERY_BEAT_SCHEDULE = {
    "generate-insights": {
        "task": "apps.insights.tasks.generate_insights",
        "schedule": 86400.0,  # nightly — insight rules query potentially large datasets
    },
    "evaluate-policies": {
        "task": "apps.insights.tasks.evaluate_policies",
        "schedule": 900.0,  # every 15 min — matches the escalation / campaign-sweep tier
    },
    "expire-trials": {
        "task": "apps.billing.tasks.expire_trials",
        "schedule": 3600.0,
    },
    "commit-stale-usage-reservations": {
        "task": "apps.billing.tasks.commit_stale_reservations",
        "schedule": 3600.0,
    },
    "retry-held-email-recipients": {
        "task": "apps.email.tasks.retry_held_email_recipients",
        "schedule": 900.0,  # every 15 min — a held recipient isn't lost, just waiting for room
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
    "recheck-verified-domains": {
        "task": "apps.email.tasks.recheck_verified_domains",
        # Hourly, but each domain is only re-checked once it's 20h stale, in
        # batches of 200 -- so every verified domain is seen about daily.
        "schedule": 3600.0,
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
    "find-repeated-replies": {
        "task": "apps.automation.tasks.find_repeated_replies",
        "schedule": 86400.0,  # "You've answered this N times": daily is fresh enough
    },
    "remind-missed-conversations": {
        "task": "apps.conversations.tasks.remind_missed_conversations",
        "schedule": 3600.0,  # a conversation becomes "missed" at 24h; hourly is prompt enough
    },
    "escalate-overdue-conversations": {
        "task": "apps.conversations.tasks.escalate_overdue_conversations",
        "schedule": 900.0,  # OVERDUE_WAITING is 2h; 15-minutely matches the campaign sweeper's cadence
    },
    "run-due-scheduled-jobs": {
        "task": "apps.scheduler.tasks.run_due_jobs",
        "schedule": 60.0,  # send-later / scheduled campaigns resolve at minute granularity
    },
    "prune-scheduled-jobs": {
        "task": "apps.scheduler.tasks.prune_scheduled_jobs",
        "schedule": 86400.0,
    },
    "capture-benchmarks": {
        "task": "apps.conversations.tasks.capture_benchmarks",
        "schedule": 21600.0,  # starting-week and day-30 numbers; captured once each, 6-hourly is plenty
    },
    "capture-weekly-snapshots": {
        "task": "apps.conversations.tasks.capture_weekly_snapshots",
        "schedule": 21600.0,  # a week only closes and settles once; 6-hourly is plenty here too
    },
    "send-weekly-reports": {
        "task": "apps.conversations.tasks.send_weekly_reports",
        "schedule": 86400.0,  # daily; the Event guard below makes each account's send once-a-week
    },
    "delete-closed-accounts": {
        "task": "apps.core.tasks.delete_closed_accounts",
        "schedule": 86400.0,  # 30 days after an operator closes an account
    },
    "send-heartbeats": {
        "task": "apps.core.tasks.send_heartbeats",
        "schedule": 60.0,  # the Pilot Command Center shows each queue's last heartbeat
    },
    "beat-heartbeat": {
        "task": "apps.core.tasks.beat_heartbeat",
        "schedule": 120.0,  # every 2 min; TTL is 5 min — /healthz reports "beat": false when expired
    },
    "evaluate-support-sla": {
        "task": "apps.support.tasks.evaluate_sla",
        "schedule": 300.0,  # every 5 min — SLA breach detection + at-risk escalation
    },
}

if INSTAGRAM_ENABLED:
    CELERY_BEAT_SCHEDULE.update(
        {
            "drain-instagram-outbox": {
                "task": "apps.instagram.tasks.drain_instagram_outbox",
                "schedule": 30.0,  # retries transiently-failed Instagram sends after backoff
            },
            "download-instagram-media": {
                "task": "apps.instagram.tasks.download_instagram_media",
                "schedule": 60.0,  # sweep attachments the on-arrival download missed
            },
            "refresh-instagram-tokens": {
                "task": "apps.instagram.tasks.refresh_instagram_tokens",
                "schedule": 86400.0,  # daily — long-lived tokens last 60 days
            },
        }
    )
    CELERY_TASK_ROUTES.update(
        {
            "apps.instagram.tasks.download_instagram_media": {"queue": "celery"},
            "apps.instagram.tasks.refresh_instagram_tokens": {"queue": "celery"},
        }
    )

# Every queue a production worker consumes (docker-compose.yml). The Pilot Command Center sends a
# heartbeat through each one, so a queue without a worker shows up as late.
WORKER_QUEUES = [
    "celery",
    "email",
    "outbound",
    "campaigns",
    "webhooks",
    "whatsapp",
    "automation",
    "scheduler",
    "ai",
]

# Private bucket for nightly database dumps (docs/ops/backups.md); read by the Pilot Command Center.
BACKUP_S3_BUCKET = os.getenv("BACKUP_S3_BUCKET", "")

# Scheduler kill-switch for dark-launch / backout. When False, new schedule
# requests are rejected with a 4xx (apps.scheduler.api.should_schedule) instead
# of deferring; existing jobs still drain.
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "1") != "0"

if WHATSAPP_ENABLED:
    CELERY_TASK_ROUTES.update(
        {
            "apps.whatsapp.tasks.process_whatsapp_event": {"queue": "whatsapp"},
            "apps.whatsapp.tasks.drain_outbound_queue": {"queue": "outbound"},
            "apps.whatsapp.tasks.mark_read": {"queue": "whatsapp"},
            "apps.whatsapp.tasks.sweep_stuck_whatsapp_campaigns": {
                "queue": "campaigns"
            },
        }
    )
    CELERY_BEAT_SCHEDULE.update(
        {
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
            "sweep-stuck-whatsapp-campaigns": {
                "task": "apps.whatsapp.tasks.sweep_stuck_whatsapp_campaigns",
                "schedule": 900.0,  # every 15 min, mirrors retry-held-email-recipients
            },
        }
    )

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

CSRF_COOKIE_HTTPONLY = True

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    # Health probes hit web:8000 directly (bypassing nginx), so they carry no
    # X-Forwarded-Proto header. Without this exemption Django redirects them to
    # HTTPS and the probe loop-fails forever.
    SECURE_REDIRECT_EXEMPT = [r"^/healthz/?$"]

    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    SECURE_PROXY_SSL_HEADER = (
        "HTTP_X_FORWARDED_PROTO",
        "https",
    )

    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

    # Extend (do not overwrite) any origins supplied via the environment
    CSRF_TRUSTED_ORIGINS = list(
        {
            *CSRF_TRUSTED_ORIGINS,
            "https://akilent.com",
            "https://www.akilent.com",
        }
    )


RELEASE_VERSION = os.getenv("RELEASE_VERSION", "")

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    from apps.core.browser_session import get_browser_session_id
    from apps.core.request_context import get_request_id

    def _sentry_before_send(event, hint):
        event.setdefault("tags", {})["browser_session_id"] = get_browser_session_id()
        event.setdefault("tags", {})["request_id"] = get_request_id()
        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release=RELEASE_VERSION,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE") or "0.0"),
        send_default_pii=False,
        before_send=_sentry_before_send,
    )
