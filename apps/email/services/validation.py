"""Pre-send email recipient validation (syntax + MX record checks).

Validates email addresses before sending to avoid wasting SES quota on addresses
that are guaranteed to bounce. Syntax validation is instant; MX record checks are
cached per domain to avoid repeated DNS lookups on high-volume sends.

Designed to mirror the error-handling and testability patterns of apps.email.dnscheck:
broad exception handling (treat DNS timeouts as "no MX"), debug-level logging only,
monkeypatched network seam for test coverage.
"""

import logging

import dns.resolver
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

logger = logging.getLogger(__name__)

_MX_LOOKUP_LIFETIME = 5.0


def _resolve_mx(domain: str) -> list:
    """Resolve MX records for a domain. Returns empty list on any error (no MX, timeout, etc).

    Single network seam, monkeypatched in tests via apps.email.services.validation._resolve_mx.
    """
    try:
        return list(dns.resolver.resolve(domain, "MX", lifetime=_MX_LOOKUP_LIFETIME))
    except Exception as e:
        logger.debug("MX lookup failed for domain %s: %s", domain, type(e).__name__)
        return []


def is_valid_syntax(email: str) -> bool:
    """Check if email address matches RFC syntax. Fast, no network calls."""
    try:
        validate_email(email)
        return True
    except ValidationError:
        return False


def has_mx_record(domain: str) -> bool:
    """Check if domain has at least one MX record. Cached per domain."""
    if not domain:
        return False

    cache_key = f"mx_check:{domain}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Load cache TTL from settings singleton
    from apps.core.models import MailProviderSettings

    settings = MailProviderSettings.load()
    ttl = settings.mx_validation_cache_ttl_seconds

    # Perform lookup
    has_mx = bool(_resolve_mx(domain))
    cache.set(cache_key, has_mx, ttl)
    return has_mx


def validate_recipient(email: str) -> bool:
    """Validate a recipient email address for delivery.

    Returns True if the address is valid and should be sent to.
    Returns False if it fails validation (syntax or no MX record on domain).

    Short-circuits to True if validation is disabled globally.
    """
    from apps.core.models import MailProviderSettings

    settings = MailProviderSettings.load()

    if not settings.enable_recipient_validation:
        return True

    # Syntax check first (no network I/O)
    if not is_valid_syntax(email):
        logger.debug("Recipient validation failed (syntax): %s", email)
        return False

    # MX record check (cached)
    domain = email.split("@", 1)[1] if "@" in email else ""
    if not has_mx_record(domain):
        logger.debug("Recipient validation failed (no MX): %s", email)
        return False

    return True
