"""Management command to start the SMTP relay server."""

import asyncio
import base64
import hashlib
import logging
from email.parser import BytesParser

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class RelayHandler:
    """Handles incoming SMTP messages and delivers via configured send provider."""

    def _extract_text_and_html(self, msg):
        """Extract text and HTML bodies from email message (handles multipart)."""
        text_body = ""
        html_body = ""

        if msg.is_multipart():
            for part in msg.iter_parts():
                content_type = part.get_content_type()
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        payload_str = payload.decode("utf-8", errors="replace")
                        if content_type == "text/plain" and not text_body:
                            text_body = payload_str
                        elif content_type == "text/html" and not html_body:
                            html_body = payload_str
                except Exception as e:
                    logger.debug(f"SMTP: failed to decode {content_type}: {e}")
        else:
            # Non-multipart: treat as text body
            text_body = msg.get_payload()

        return text_body, html_body

    async def handle_DATA(self, server, session, envelope):
        """Accept email and queue for delivery."""
        if not session.authenticated:
            return "530 Authentication required"

        from apps.api.services import create_and_queue_message
        from apps.email.models import SmtpCredential

        username = session.authenticated
        try:
            cred = SmtpCredential.objects.select_related("account", "domain").get(
                username=username
            )
        except SmtpCredential.DoesNotExist:
            logger.warning(f"SMTP: credential not found after auth: {username}")
            return "500 Authentication state lost"

        # Parse the email
        parser = BytesParser()
        msg = parser.parsebytes(envelope.content)

        # Extract headers
        from_email = envelope.mail_from or msg.get("From", "")
        to_emails = envelope.rcpt_tos or []

        if not from_email or not to_emails:
            return "550 Missing From or To header"

        subject = msg.get("Subject", "")
        text_body, html_body = self._extract_text_and_html(msg)

        # Queue a message for each recipient
        try:
            for to_email in to_emails:
                create_and_queue_message(
                    account=cred.account,
                    from_email=from_email,
                    to_email=to_email,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                )
            logger.info(f"SMTP: queued {len(to_emails)} message(s) from {username}")
            return "250 Message accepted"
        except Exception as exc:
            logger.error(f"SMTP: failed to queue message: {exc}")
            return f"550 Failed to queue message: {str(exc)[:100]}"


class Command(BaseCommand):
    """Start SMTP relay server."""

    help = "Start SMTP relay server on port 2587"

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=2587)

    def handle(self, *args, **options):
        host = options["host"]
        port = options["port"]

        self.stdout.write(f"Starting SMTP relay on {host}:{port}")

        def auth_callback(server, session, envelope, mechanism, auth_data):
            """Verify SMTP credentials against database."""
            from apps.email.models import SmtpCredential

            # Decode credentials based on mechanism
            if mechanism == "LOGIN":
                # auth_data is list: [username, password]
                if len(auth_data) < 2:
                    return AuthResult(success=False)
                username, password = auth_data[0], auth_data[1]
            elif mechanism == "PLAIN":
                # auth_data is bytes or str: \0username\0password (base64 encoded by client)
                try:
                    if isinstance(auth_data, bytes):
                        decoded = base64.b64decode(auth_data).decode()
                    else:
                        decoded = base64.b64decode(auth_data).decode()
                    parts = decoded.split("\0")
                    if len(parts) == 3:
                        # authzid, authcid, passwd
                        username, password = parts[1], parts[2]
                    elif len(parts) == 2:
                        # authcid, passwd
                        username, password = parts[0], parts[1]
                    else:
                        return AuthResult(success=False)
                except (ValueError, UnicodeDecodeError):
                    logger.warning("Failed to decode AUTH PLAIN credentials")
                    return AuthResult(success=False)
            else:
                return AuthResult(success=False)

            # Look up and verify credential
            try:
                cred = SmtpCredential.objects.get(
                    username=username.lower(), is_active=True
                )
                password_hash = hashlib.sha256(password.encode()).hexdigest()
                if password_hash == cred.secret_hash:
                    logger.info(
                        f"SMTP auth successful: {username} (account={cred.account_id})"
                    )
                    return AuthResult(success=True, identity=username)
            except SmtpCredential.DoesNotExist:
                logger.warning(f"SMTP auth failed: credential not found for {username}")
                return AuthResult(success=False)

            logger.warning(f"SMTP auth failed: invalid password for {username}")
            return AuthResult(success=False)

        # Read TLS requirement from MailProviderSettings
        require_tls = True
        try:
            from apps.core.models import MailProviderSettings

            settings_obj = MailProviderSettings.load()
            require_tls = settings_obj.smtp_require_tls
        except Exception as e:
            logger.warning(
                f"Failed to load SMTP TLS setting; defaulting to required: {e}"
            )

        # Create and run the controller
        controller = Controller(
            RelayHandler(),
            hostname=host,
            port=port,
            auth_required=True,
            auth_require_tls=require_tls,
            auth_callback=auth_callback,
        )

        with controller:
            self.stdout.write("Press Ctrl+C to stop")
            try:
                asyncio.run(asyncio.sleep(float("inf")))
            except KeyboardInterrupt:
                self.stdout.write("Stopping SMTP relay server")
