# TOTP secret encryption: migration design (revision 2, for owner approval)

Status: **code deployed in commit 98b647b; production in plaintext mode.** Migrations 0014 and 0015 applied; sidecar table empty; production device still plaintext. Remaining phases (expand, switch, contract) not started.

## 1. Current state
- Production has **one** `TOTPDevice` (owner report, 2026-10-16). It must keep working throughout the migration.
- `django-otp`'s `TOTPDevice.key` is `CharField(max_length=80)`, hex-encoded. `bin_key` decodes it with `unhexlify`, and `config_url` reads it. Fernet ciphertext does not fit and is not hex.
- Existing project support: `apps/accounts/fields.py` (`EncryptedTextField`, MultiFernet keys from `FIELD_ENCRYPTION_KEYS`).
- Replay (`last_t`), throttling and session integration live in `django-otp` and must keep their current behaviour.

## 2. Approach: a storage service, not global model hooks
Keep `django_otp`'s model unchanged. Do **not** use global `post_init`/`pre_save` handlers, because they intercept every model load and save, including migrations and admin code.

**Sidecar table** `TOTPSecretCiphertext`: `device` (one-to-one to `TOTPDevice`), `key_enc` (`EncryptedTextField`), `created_at`.

**Storage service** `apps/accounts/totp_storage.py`, the only code that reads or writes the secret:
- `enrol(user, key_hex)`: inside one `transaction.atomic()`, create the `TOTPDevice` with the placeholder `key` and create the sidecar with the ciphertext. The plaintext is never committed, and if either write fails both roll back.
- `load_for_verify(device)`: returns the device with the decrypted key set **in memory only**, for `verify_token` and `config_url`. It never saves it back.
- `load_for_verify` behaviour depends on a storage mode setting:
  - `plaintext`: current behaviour (before the migration).
  - `migrating`: use the sidecar if present, otherwise fall back to plaintext.
  - `encrypted`: sidecar required. **A missing sidecar fails closed**; the placeholder is never treated as a secret.

**Write paths.** Every path that creates or changes a device must go through the service. Add a test that fails if `TOTPDevice` is created or saved with a real key outside the service (grep test over `apps/`, plus a unit test of the service).

Rejected: swapping the `django_otp` model (not supported); a multi-table subclass (more moving parts); ciphertext in the 80-character field (does not fit); global signal handlers (see above).

## 3. Phased migration (expand, switch, contract)

**Phase 0: inspect, read-only (owner).** Record `user_id`, `confirmed`, `name`, `step`, `digits`, `t0`, `tolerance`, `drift`, `last_t`, and the length of `key`. Do not print or export `key`.

**Phase 1: backup and key custody.** Take a database dump and prove it restores (PR-11 drill). Keep the current `FIELD_ENCRYPTION_KEYS` value in an offline password-manager copy.

**Phase 2: expand (code deploy, behaviour unchanged, storage mode `plaintext`).**
- Migration creates `TOTPSecretCiphertext`. Reversible by dropping the table.
- Data migration encrypts each existing device's key into the sidecar. The plaintext `key` stays in place.
- **Verification, without printing the secret:**
  1. In-process, decrypt the sidecar and compare it with the plaintext `key`. Output only `True` or `False`.
  2. Generate a valid token from the decrypted key and verify it through the storage service's verify path, using a dry-run check that does not change `last_t` or throttling.
- Rollback: drop the table. Plaintext is untouched.

**Phase 3: switch reads (code deploy, storage mode `migrating`).**
- Reads use the sidecar, falling back to plaintext if it is missing.
- Owner signs in with the existing device on production: password plus code. Expected: success, `auth.2fa_success` in the audit log, and replay of the same code is refused.
- Rollback: revert code to `plaintext`. Plaintext is still present.

**Phase 4: contract (irreversible without the key).**
- Migration overwrites the `key` column with the placeholder.
- Reverse migration decrypts the sidecar and restores plaintext. It **requires** the encryption key.
- Only run after Phase 3 is verified on production and a fresh dump has been taken.
- Then set storage mode `encrypted`. Missing sidecars now fail closed.

**Rollback rule:** at every phase, the device has at least one working way to authenticate. Phase 4 must not run unless the key is in the password manager and a pre-contract dump restore has been rehearsed.

## 4. Key management
- MultiFernet via `FIELD_ENCRYPTION_KEYS` (newest first).
- Rotation: prepend the new key, re-encrypt the sidecar, verify every row, then retire the old key. Never retire a key that any row still depends on.
- Missing or invalid key: a device whose sidecar cannot be decrypted **fails closed**, writes `auth.2fa_decrypt_failed`, and recovers through the two-person reset command. No silent plaintext fallback outside `migrating`.
- The key is required to read the sidecar, so the password-manager copy is part of the backup.

## 5. Tests (required before production)
Lifecycle:
- Enrolment is atomic: a failure after the device insert leaves neither a device nor a sidecar, and no plaintext is committed.
- A device loaded before the sidecar table exists (historical models in migrations) does not crash migration runs.
- `encrypted` mode: a device with no sidecar fails closed; the placeholder is never used as a key.
- `migrating` mode: falls back to plaintext only when the sidecar is missing.
- Every write path goes through the service (grep test plus unit test).

Verification:
- Decrypted key equals the plaintext key (boolean only, no output of the secret).
- A token from the original secret verifies on both the plaintext and sidecar paths.
- Replay is refused (`last_t`); throttling and lockout still apply (PR-2 tests).
- `config_url` still produces the same provisioning URI.

Keys:
- Wrong key: decryption fails, device fails closed, no plaintext fallback.
- Rotation: rows re-encrypt, still verify, old key retired only after verification.

Environment:
- Migration forward and reverse on an anonymised copy of production data. Test fixtures never contain a real secret.
- PostgreSQL run for the device path, including the row lock.

## 6. Compose `$` interpolation warning (out of scope; owner decision)
The `DJANGO_SECRET_KEY` interpolation warning in `.env` is **not being pursued**, by the owner's decision on 2026-10-16. It stays recorded as an open risk in `docs/ROADMAP-GAPS.md`. It is independent of this migration. Should the owner revisit it, the constraints are: never print the secret, compare fingerprints only, and plan the correction separately because changing the effective key invalidates sessions and password-reset tokens.

## 7. Approval status
- **Retain:** expand, switch, contract sequencing; MultiFernet key management; backup requirement; explicit rollback; fail-closed behaviour.
- **Revised in this draft:** storage service replaces global hooks; atomic enrolment; storage modes; verification by key equality and token check; lifecycle tests.
- **Blocked:** production changes and mandatory MFA enforcement, until the owner approves this revision, the implementation passes the section 5 tests, and the lost-device rehearsal passes.

## 8. Owner decisions needed
1. Run the Phase 0 read-only inspection and send the non-secret fields.
2. Approve the storage-service approach (section 2) and storage modes (section 3).
3. Approve the phase order and whether a maintenance window is wanted for the Phase 3 sign-in check.

## 9. Implementation status (development)
- Model: `TOTPSecretCiphertext` (migration `0015_totp_secret_ciphertext`, additive).
- Service: `apps/accounts/totp_storage.py` (`create_device`, `load`, `verify_and_seal`, `reseal`, `sidecar_matches`). Login, enrolment and enable routed through it.
- Command: `manage.py totp_secret_migrate {expand|verify|contract|restore} [--confirm]`. Prints counts and booleans only.
- Default `TOTP_SECRET_STORAGE=plaintext`: no behaviour change until the mode is switched.
- Tests: `apps/accounts/tests/test_totp_storage.py` (22 tests incl. atomic enrolment, fail-closed, wrong key, replay on the sealed path, phased command, sealed sign-in). Accounts suite on SQLite: 226 passed, 1 skipped.
- Bug found and fixed during testing: decryption occurs when the sidecar row is fetched, so the fail-closed guard must cover the query, not just the attribute read.
- Still required before production: PostgreSQL run, Phase 0 inspection, dump and restore proof, maintenance window, lost-device rehearsal.

## 10. PostgreSQL verification (development, 2026-10-17)
- Container: `automator-pg-verify2` (postgres:16.15, port 55441), verification only.
- Full accounts suite on PostgreSQL: 221 passed, 6 failed. The six failures (`test_auth` signup tests, `test_business_hours`, `test_default_modules` x3, `test_signup_billing`) **also fail on the untouched commit `a2f4863` against the same PostgreSQL**, so they predate this work. Evidence: `scratchpad/postgres_evidence_v3.txt` and `scratchpad/baseline_out.txt`.
- MFA and TOTP files on PostgreSQL (`test_2fa*`, `test_totp_storage`, `test_mfa_*`, `test_reset_user_mfa`): **62 passed, 0 skipped**. Includes the row-lock test. Evidence: `scratchpad/pg_mfa_files.txt`.
- An earlier PostgreSQL run took 3 hours and failed 16 tests because Celery tried to reach `redis:6379`. Fixed by using the eager Celery test settings for verification. Not a code result.
- Still open: the six pre-existing PostgreSQL failures need their own investigation before any PostgreSQL CI gate is relied on.

## 11. PostgreSQL re-run after review fixes (2026-10-17)
- Container `automator-pg-verify3` (postgres:16, port 55442), verification only.
- MFA and TOTP files on PostgreSQL: **64 passed, 0 skipped** (includes the spaced-code regression tests). Evidence: `scratchpad/pg_mfa_files_v2.txt`.
- Full accounts suite on PostgreSQL: 223 passed, 6 failed. The same six baseline failures as `a2f4863`. Evidence: `scratchpad/pg_full_accounts_v4.txt`.

## 12. Phase 0 result: production inspection (read-only, 2026-10-17)
- Device count: 1. Confirmed: yes. Name: default. Step 30s, digits 6, t0 0, tolerance 1, drift 0.
- `last_t` unchanged from the earlier inspection, so no successful TOTP sign-in since then.
- Stored key length: 40 characters (hex). Key value not printed or recorded.
- Identifiers (device and user IDs) are kept out of this repository, per the review.
- Sidecar table check: **not yet run**. Expected to fail with a missing-table error, because migration 0015 is not deployed.

## 13. Deployment state (verified 2026-10-17)
- Production runs `98b647b` (feat(mfa): role-based MFA enforcement and recovery codes). Migrations `0014_recovery_code` and `0015_totp_secret_ciphertext` applied.
- Production flags: `MFA_ENFORCEMENT_ENABLED=False`, `MFA_ENFORCED_ROLES=[]`, `MFA_ENFORCE_STAFF=False`, `TOTP_SECRET_STORAGE=plaintext`. Enforcement is inert.
- Live behaviour changes: disable 2FA needs password plus code; enrolment shows recovery codes once; email-verify links do not sign in enrolled users; spaced codes verify.
- CI result for `98b647b`: **passed** (reported by the owner 2026-10-17; run number not yet recorded). Deploy was triggered by the CI success gate, not by manual dispatch.
