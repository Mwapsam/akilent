from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models


def _fernet() -> MultiFernet:
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None)
    if not keys:
        single = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
        if not single:
            raise ImproperlyConfigured(
                "Set FIELD_ENCRYPTION_KEYS (list, newest first) in settings."
            )
        keys = [single]
    return MultiFernet([Fernet(k) for k in keys])


class EncryptedTextField(models.TextField):
    """A ``TextField`` that transparently encrypts/decrypts via Fernet."""

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        return _fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except Exception:
            raise ValidationError(
                "Could not decrypt field value - check FIELD_ENCRYPTION_KEYS "
                "(is the original key still present in the list?)."
            )

    def to_python(self, value):
        if value is None or value == "":
            return value
        return str(value)
