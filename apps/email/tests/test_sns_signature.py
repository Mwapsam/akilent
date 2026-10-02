"""Real-crypto coverage for SNS signature verification (SigV1 + SigV2).

The other webhook tests mock `_verify_sns_signature`; this one exercises the
actual RSA/PKCS1v15 path with a self-signed cert so a regression in the
canonical-string construction or the verify call is caught.
"""

import base64
import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.core.cache import cache

from apps.email import ses_webhooks

CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123def.pem"


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key, pem


def _canonical(message: dict) -> str:
    fields = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    return "".join(f"{f}\n{message[f]}\n" for f in fields if f in message)


def _signed_message(key, *, version: str) -> dict:
    msg = {
        "Type": "Notification",
        "MessageId": "id-1",
        "TopicArn": "arn:aws:sns:us-east-1:123:topic",
        "Timestamp": "2026-09-03T00:00:00.000Z",
        "Message": '{"eventType":"Delivery"}',
        "SignatureVersion": version,
        "SigningCertURL": CERT_URL,
    }
    algo = hashes.SHA1() if version == "1" else hashes.SHA256()  # nosec B303
    sig = key.sign(_canonical(msg).encode(), padding.PKCS1v15(), algo)
    msg["Signature"] = base64.b64encode(sig).decode()
    return msg


@pytest.fixture(autouse=True)
def _clear_cert_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _serve_cert(keypair, monkeypatch):
    _, pem = keypair

    class _Resp:
        text = pem

        def raise_for_status(self):
            pass

    monkeypatch.setattr(ses_webhooks.requests, "get", lambda *a, **k: _Resp())


@pytest.mark.parametrize("version", ["1", "2"])
def test_valid_signature_passes(keypair, version):
    key, _ = keypair
    assert (
        ses_webhooks._verify_sns_signature(_signed_message(key, version=version))
        is True
    )


@pytest.mark.parametrize("version", ["1", "2"])
def test_tampered_message_body_fails(keypair, version):
    key, _ = keypair
    msg = _signed_message(key, version=version)
    msg["Message"] = '{"eventType":"Bounce"}'  # signature no longer matches
    assert ses_webhooks._verify_sns_signature(msg) is False


def test_wrong_key_fails(keypair):
    key, _ = keypair
    msg = _signed_message(key, version="1")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_sig = other.sign(_canonical(msg).encode(), padding.PKCS1v15(), hashes.SHA1())  # nosec B303
    msg["Signature"] = base64.b64encode(bad_sig).decode()
    assert ses_webhooks._verify_sns_signature(msg) is False


def test_untrusted_cert_url_rejected(keypair):
    key, _ = keypair
    msg = _signed_message(key, version="1")
    msg["SigningCertURL"] = "https://evil.example.com/SimpleNotificationService-x.pem"
    assert ses_webhooks._verify_sns_signature(msg) is False


def test_unsupported_signature_version_rejected(keypair):
    key, _ = keypair
    msg = _signed_message(key, version="1")
    msg["SignatureVersion"] = "3"
    assert ses_webhooks._verify_sns_signature(msg) is False
