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
import hashlib
import json
import secrets
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
    ResidentKeyRequirement,
)

from apps.accounts.models import PasskeyCredential

CHALLENGE_TTL = timedelta(minutes=5)
REG_KEY = "webauthn_registration"
AUTH_KEY = "webauthn_authentication"


def has_passkeys(user):
    return PasskeyCredential.objects.filter(user=user, confirmed=True).exists()


LINK_TTL = timedelta(minutes=5)


def _link_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_link(user):
    """Issue a one-time token for a phone to add a passkey. Returns the raw token."""
    from apps.accounts.models import PasskeyLinkRequest

    token = secrets.token_urlsafe(32)
    PasskeyLinkRequest.objects.create(
        user=user,
        token_digest=_link_digest(token),
        expires_at=timezone.now() + LINK_TTL,
    )
    return token


def find_link(user, token):
    """The pending link for this user and token, or None if it is used, expired or not theirs."""
    from apps.accounts.models import PasskeyLinkRequest

    link = PasskeyLinkRequest.objects.filter(
        token_digest=_link_digest(token), user=user
    ).first()
    if link is None or link.used_at is not None or link.expires_at <= timezone.now():
        return None
    return link


def complete_link(request, user, token, credential_json, name=""):
    """Store a passkey added from a phone and mark the link used, in one transaction.

    The link row is locked, so two phones cannot both use it. If the credential does
    not verify, nothing is stored and the link stays usable for another try.
    """
    from django.db import transaction

    from apps.accounts.models import PasskeyLinkRequest

    now = timezone.now()
    with transaction.atomic():
        link = (
            PasskeyLinkRequest.objects.select_for_update()
            .filter(
                token_digest=_link_digest(token),
                user=user,
                used_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if link is None:
            return None
        passkey = finish_registration(request, user, credential_json, name)
        if passkey is None:
            transaction.set_rollback(True)
            return None
        link.used_at = now
        link.save(update_fields=["used_at"])
        return passkey


def link_is_done(user, token):
    """True once a passkey has been added through this link."""
    from apps.accounts.models import PasskeyLinkRequest

    return PasskeyLinkRequest.objects.filter(
        token_digest=_link_digest(token), user=user, used_at__isnull=False
    ).exists()


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


def begin_registration(request, user):
    """Return registration options as JSON for the browser."""
    rp_id, _ = _relying_party(request)
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
    )
    _store_challenge(request, REG_KEY, options.challenge)
    return options_to_json(options)


def finish_registration(request, user, credential_json, name=""):
    """Verify the new credential and store it. Returns the credential, or None."""
    pending = _take_challenge(request, REG_KEY)
    if pending is None:
        return None
    rp_id, origin = _relying_party(request)
    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=base64.b64decode(pending["challenge"]),
            expected_rp_id=rp_id,
            expected_origin=origin,
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
