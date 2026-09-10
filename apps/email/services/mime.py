"""Build a raw RFC 5322 message from an OutboundEmail (used for attachment sends)."""
from __future__ import annotations

from email.message import EmailMessage as _MimeMessage
from email.utils import make_msgid

from apps.email.types import OutboundEmail


def build_mime(message: OutboundEmail) -> bytes:
    """Serialize `message` (including attachments) to raw MIME bytes."""
    mime = _MimeMessage()
    mime["From"] = message.from_email
    mime["To"] = message.to_email
    mime["Subject"] = message.subject
    mime["Message-ID"] = make_msgid()
    for name, value in (message.headers or {}).items():
        if name.lower() not in ("from", "to", "subject", "message-id"):
            mime[name] = value

    text = message.text_body or " "
    if message.html_body:
        mime.set_content(text)
        mime.add_alternative(message.html_body, subtype="html")
    else:
        mime.set_content(text)

    for att in message.attachments or ():
        maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
        mime.add_attachment(
            att.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )
    return mime.as_bytes()
