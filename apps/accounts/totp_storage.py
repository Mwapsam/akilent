"""The only code that reads or writes a TOTP device's secret.

Storage modes (settings.TOTP_SECRET_STORAGE):

- ``plaintext``: the stock django-otp behaviour. Default; no change.
- ``migrating``: new devices are sealed (encrypted sidecar, placeholder in the
  key column). Existing devices keep their plaintext, so rollback to
  ``plaintext`` still works. A missing sidecar falls back to plaintext.
- ``encrypted``: every device must have a sidecar. A missing sidecar fails
  closed; the placeholder is never used as a key.

django-otp's verify_token() calls a full save(), which would write the
decrypted key back into the key column. ``verify_and_seal`` therefore re-seals
the placeholder inside the same transaction, so plaintext is never committed.

See docs/design/totp-encryption-migration.md.
"""

from django.conf import settings
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice, default_key

from apps.accounts.models import TOTPSecretCiphertext

# Not hex, so it can never verify a code. A device left on the placeholder with
# no usable sidecar fails closed instead of matching a real secret.
PLACEHOLDER = "SEALED"


def mode():
    return getattr(settings, "TOTP_SECRET_STORAGE", "plaintext")


def create_device(user, name="default", key_hex=None, confirmed=False):
    """Create a device, storing its secret according to the current mode."""
    key_hex = key_hex or default_key()
    if mode() == "plaintext":
        return TOTPDevice.objects.create(
            user=user, name=name, key=key_hex, confirmed=confirmed
        )
    with transaction.atomic():
        device = TOTPDevice.objects.create(
            user=user, name=name, key=PLACEHOLDER, confirmed=confirmed
        )
        TOTPSecretCiphertext.objects.create(device=device, key_enc=key_hex)
    device.key = key_hex  # in memory only, for config_url and verification
    device._sealed = True
    return device


def load(device):
    """Return the device with its secret in memory, or None if it cannot be used.

    Never saves the decrypted key. Records whether the stored column was the
    placeholder, so ``reseal`` knows whether to restore it.
    """
    device._sealed = device.key == PLACEHOLDER
    try:
        # Decryption happens when the row is fetched, so the guard covers the query too.
        sidecar = TOTPSecretCiphertext.objects.filter(device=device).first()
        key = sidecar.key_enc if sidecar is not None else None
    except Exception:
        _audit_decrypt_failure(device)
        return None
    if key is not None:
        device.key = key
        return device
    if mode() == "plaintext":
        return device
    if mode() == "migrating" and not device._sealed:
        return device
    return None  # encrypted mode, or a sealed device with no usable sidecar


def reseal(device):
    """Put the placeholder back in the key column if the row was sealed on load."""
    if getattr(device, "_sealed", False):
        TOTPDevice.objects.filter(pk=device.pk).update(key=PLACEHOLDER)


def verify_and_seal(device, token):
    """Verify a code under a transaction and re-seal. Returns the device if valid, else None."""
    with transaction.atomic():
        loaded = load(device)
        if loaded is None:
            return None
        try:
            verified = loaded.verify_token(token)
        finally:
            reseal(loaded)
        return loaded if verified else None


def _audit_decrypt_failure(device):
    from apps.core.audit import platform_record

    platform_record(
        action="auth.2fa_decrypt_failed",
        success=False,
        metadata={"device_pk": device.pk},
    )


def sidecar_matches(device):
    """True when the sidecar decrypts to the plaintext key. Returns a boolean only."""
    try:
        sidecar = TOTPSecretCiphertext.objects.filter(device=device).first()
        return sidecar is not None and sidecar.key_enc == device.key
    except Exception:
        return False
