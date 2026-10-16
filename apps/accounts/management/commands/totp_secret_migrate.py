"""Phased TOTP secret migration: expand, verify, contract, restore.

Phases (run in order; see docs/design/totp-encryption-migration.md):

- expand:   copy each device's key into an encrypted sidecar. Plaintext stays.
- verify:   confirm each sidecar decrypts to its plaintext key. Prints counts only.
- contract: refuse unless every device verifies, then replace the plaintext
            with the placeholder. Needs the encryption key to undo.
- restore:  decrypt each sidecar back into the key column (undoes contract).

Secrets are never printed. Output is counts and booleans.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import totp_storage
from apps.accounts.models import TOTPSecretCiphertext


class Command(BaseCommand):
    help = "Expand, verify, contract or restore TOTP secret encryption."

    def add_arguments(self, parser):
        parser.add_argument(
            "phase", choices=["expand", "verify", "contract", "restore"]
        )
        parser.add_argument(
            "--confirm", action="store_true", help="Required for contract and restore."
        )

    def handle(self, *args, **options):
        phase = options["phase"]
        handler = getattr(self, f"_{phase}")
        handler(options)

    def _expand(self, options):
        created = already = 0
        for device in TOTPDevice.objects.exclude(key=totp_storage.PLACEHOLDER):
            if TOTPSecretCiphertext.objects.filter(device=device).exists():
                already += 1
                continue
            TOTPSecretCiphertext.objects.create(device=device, key_enc=device.key)
            created += 1
        self.stdout.write(f"expand: created={created} already_present={already}")

    def _verify(self, options):
        results = [
            totp_storage.sidecar_matches(device)
            for device in TOTPDevice.objects.exclude(key=totp_storage.PLACEHOLDER)
        ]
        self.stdout.write(
            f"verify: devices={len(results)} all_match={all(results) and bool(results)}"
        )
        return results

    def _contract(self, options):
        if not options["confirm"]:
            raise CommandError(
                "contract changes stored secrets. Re-run with --confirm."
            )
        results = self._verify(options)
        if not results or not all(results):
            raise CommandError(
                "Refusing to contract: not every device's sidecar verifies."
            )
        with transaction.atomic():
            updated = TOTPDevice.objects.exclude(key=totp_storage.PLACEHOLDER).update(
                key=totp_storage.PLACEHOLDER
            )
        self.stdout.write(f"contract: sealed={updated}")

    def _restore(self, options):
        if not options["confirm"]:
            raise CommandError(
                "restore rewrites stored secrets. Re-run with --confirm."
            )
        restored = 0
        with transaction.atomic():
            for device in TOTPDevice.objects.filter(key=totp_storage.PLACEHOLDER):
                sidecar = TOTPSecretCiphertext.objects.filter(device=device).first()
                if sidecar is None:
                    raise CommandError(
                        "A sealed device has no sidecar; restore aborted."
                    )
                TOTPDevice.objects.filter(pk=device.pk).update(key=sidecar.key_enc)
                restored += 1
        self.stdout.write(f"restore: restored={restored}")
