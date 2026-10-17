"""Single-use recovery codes for TOTP sign-in.

Codes are shown once when generated and stored only as HMAC-SHA256 digests
keyed with SECRET_KEY. Redemption is one conditional UPDATE, so two concurrent
requests cannot both spend the same code.

Rotating SECRET_KEY invalidates every outstanding code; users would then need
a new set (see the backup and key-rotation runbook).
"""

import hashlib
import hmac
import secrets

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import RecoveryCode

CODE_COUNT = 10
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid misreads
_CODE_LENGTH = 16  # 16 chars from a 32-char alphabet = 80 bits


def _normalise(raw):
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def looks_like_code(raw):
    """True when raw has the shape of a recovery code rather than a TOTP code."""
    return len(_normalise(raw)) == _CODE_LENGTH


def _digest(normalised):
    secret = settings.SECRET_KEY
    if not secret:
        raise ImproperlyConfigured("SECRET_KEY is required for recovery codes.")
    return hmac.new(secret.encode(), normalised.encode(), hashlib.sha256).hexdigest()


def generate_codes(user):
    """Replace the user's codes and return the plaintext list, for one-time display."""
    displayed, digests = [], []
    for _ in range(CODE_COUNT):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        displayed.append("-".join(raw[i : i + 4] for i in range(0, _CODE_LENGTH, 4)))
        digests.append(_digest(raw))
    with transaction.atomic():
        # Lock the user row so two regenerations run one after the other. Without
        # this, both could delete the old set and each add ten, leaving twenty valid.
        User.objects.select_for_update().filter(pk=user.pk).first()
        RecoveryCode.objects.filter(user=user).delete()
        RecoveryCode.objects.bulk_create(
            [RecoveryCode(user=user, code_hash=d) for d in digests]
        )
    return displayed


def redeem(user, raw):
    """Spend a recovery code. Returns True only for an unused code that matches."""
    normalised = _normalise(raw)
    if len(normalised) != _CODE_LENGTH:
        return False
    updated = RecoveryCode.objects.filter(
        user=user, code_hash=_digest(normalised), used_at__isnull=True
    ).update(used_at=timezone.now())
    return updated == 1


def remaining(user):
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
