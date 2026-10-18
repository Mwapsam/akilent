"""Passkeys and security keys (WebAuthn) as a second factor.

Each ceremony stores its challenge in the session, once, with a timestamp.
A challenge is consumed on first use and refused after five minutes, so a
captured response cannot be replayed. The relying-party ID and origin come
from the request host unless WEBAUTHN_RP_ID overrides the ID.

A sign counter that does not move forward is treated as a cloned key and
refused. Verification failures are not locked out here; the browser's own
user-verification prompt and the sign counter are the controls.
"""

import base64
import json
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
)

from apps.accounts.models import PasskeyCredential, PasskeyRegistrationChallenge

CHALLENGE_TTL = timedelta(minutes=5)
REG_KEY = "webauthn_registration"
AUTH_KEY = "webauthn_authentication"


def _timeout_ms():
    """How long the browser waits for a phone to scan, approve and pair (both ceremonies)."""
    return getattr(settings, "WEBAUTHN_REGISTRATION_TIMEOUT_MS", 120_000)


def has_passkeys(user):
    return PasskeyCredential.objects.filter(user=user, confirmed=True).exists()


def _relying_party(request):
    host = request.get_host()
    rp_id = getattr(settings, "WEBAUTHN_RP_ID", "") or host.split(":")[0]
    return rp_id, f"{request.scheme}://{host}"


def _store_challenge(request, key, challenge, **extra):
    request.session[key] = {
        "challenge": base64.b64encode(challenge).decode(),
        "at": timezone.now().isoformat(),
        **extra,
    }


def _take_challenge(request, key):
    """Pop the pending challenge. Single use: it is removed whether or not it verifies."""
    pending = request.session.pop(key, None)
    if not pending:
        return None
    issued = datetime.fromisoformat(pending["at"])
    if timezone.now() - issued > CHALLENGE_TTL:
        return None
    return pending


def _descriptors(credentials):
    return [
        PublicKeyCredentialDescriptor(id=bytes(c.credential_id)) for c in credentials
    ]


def _session_digest(request):
    """SHA-256 of the current session key, saving the session first if it has none yet.

    The digest binds a registration to the session that started it. Rotating the session
    key therefore invalidates the attempt, which is the intended outcome.
    """
    import hashlib

    if not request.session.session_key:
        request.session.save()
    return hashlib.sha256(request.session.session_key.encode()).hexdigest()


def begin_registration(request, user):
    """Return registration options as JSON, and store the ceremony they belong to.

    The browser runs the standard ceremony. Any phone handoff is the browser's own
    cross-device flow, so no Akilent page is opened on the phone.
    """
    rp_id, origin = _relying_party(request)
    existing = PasskeyCredential.objects.filter(user=user)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=getattr(settings, "WEBAUTHN_RP_NAME", "Akilent"),
        user_name=user.email or user.get_username(),
        user_id=str(user.pk).encode(),
        exclude_credentials=_descriptors(existing),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        # Ask the browser to offer a phone (cross-device) alongside this device.
        hints=[PublicKeyCredentialHint.HYBRID, PublicKeyCredentialHint.CLIENT_DEVICE],
        timeout=_timeout_ms(),
    )
    ceremony = PasskeyRegistrationChallenge.objects.create(
        user=user,
        challenge=base64.b64encode(options.challenge).decode(),
        rp_id=rp_id,
        origin=origin,
        session_digest=_session_digest(request),
        expires_at=timezone.now() + CHALLENGE_TTL,
    )
    request.session[REG_KEY] = {"id": ceremony.pk}
    return options_to_json(options)


def _consume_ceremony(user, ceremony_id, session_digest):
    """Mark the ceremony used if it is this user's, this session's, unused and unexpired.

    Returns the ceremony, or None. Atomic: the conditional UPDATE matches a row once, so
    parallel completions cannot both succeed.
    """
    now = timezone.now()
    claimed = PasskeyRegistrationChallenge.objects.filter(
        pk=ceremony_id,
        user=user,
        session_digest=session_digest,
        used_at__isnull=True,
        expires_at__gt=now,
    ).update(used_at=now)
    if claimed != 1:
        return None
    return PasskeyRegistrationChallenge.objects.get(pk=ceremony_id)


def finish_registration(request, user, credential_json, name=""):
    """Verify the new credential and store it. Returns the credential, or None.

    The ceremony is consumed before verification, so it is single use whether or not the
    credential verifies. Verification uses the challenge, origin and relying-party ID
    stored with the ceremony, not whatever the completing request claims.
    """
    pending = request.session.pop(REG_KEY, None)
    if not pending or not isinstance(pending, dict) or "id" not in pending:
        return None
    ceremony = _consume_ceremony(user, pending["id"], _session_digest(request))
    if ceremony is None:
        return None
    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=ceremony.challenge_bytes(),
            expected_rp_id=ceremony.rp_id,
            expected_origin=ceremony.origin,
        )
    except Exception:
        return None
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            return PasskeyCredential.objects.create(
                user=user,
                name=(name or "Passkey").strip()[:60],
                confirmed=True,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
            )
    except IntegrityError:
        # The authenticator is already registered (for example in another tab).
        return None


def begin_authentication(request, user):
    """Return sign-in options as JSON, for the user already past their password."""
    rp_id, _ = _relying_party(request)
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=_descriptors(PasskeyCredential.objects.filter(user=user)),
        timeout=_timeout_ms(),
    )
    _store_challenge(request, AUTH_KEY, options.challenge, user_pk=user.pk)
    return options_to_json(options)


def finish_authentication(request, user, credential_json):
    """Verify a sign-in assertion. Returns the passkey, or None if it does not verify."""
    pending = _take_challenge(request, AUTH_KEY)
    if pending is None or pending.get("user_pk") != user.pk:
        return None
    try:
        body = (
            json.loads(credential_json)
            if isinstance(credential_json, str)
            else credential_json
        )
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    try:
        credential_id = base64url_to_bytes(body["rawId"])
    except (KeyError, ValueError, TypeError):
        return None
    passkey = PasskeyCredential.objects.filter(
        user=user, credential_id=credential_id
    ).first()
    if passkey is None:
        return None
    rp_id, origin = _relying_party(request)
    try:
        verified = verify_authentication_response(
            credential=credential_json,
            expected_challenge=base64.b64decode(pending["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=bytes(passkey.public_key),
            credential_current_sign_count=passkey.sign_count,
        )
    except Exception:
        return None
    # The library refuses a sign counter that does not advance (a possibly copied key).
    passkey.sign_count = verified.new_sign_count
    passkey.last_used_at = timezone.now()
    passkey.save(update_fields=["sign_count", "last_used_at"])
    return passkey
