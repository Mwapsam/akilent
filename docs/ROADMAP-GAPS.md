# Akilent roadmap gaps and consolidated inventory

Status: planning document, 2026-10-16. Adding an item here does not approve building it. Each item has a tier, and the tier sets when it may be built.

Governing rules, from the existing plans:
- **Pilot feature freeze** (memory: `akilent-pilot-freeze`): no new features without pilot evidence. Bug fixes, pilot-reported friction and media/voice are allowed.
- **Evidence-first** (memory: `akilent-evidence-first-roadmap`): observed problem, smallest build, instrument, measure, decide.
- **Messaging truth rule** (`docs/marketing/messaging.md`): copy claims only what the product does today on the plan shown.

Existing roadmaps stay authoritative for their own scope. This file adds what they omit and records what is verified.

## Classifications

| Classification | Meaning |
|---|---|
| Implemented (static) | Code and a matching test name were found. Tests were not run in this audit. |
| Implemented but incomplete | Works in part; a stub, missing surface or missing path exists. |
| Missing | A named search came back empty. |
| Unverified | Could not confirm either way from code alone. |
| Blocked | Waiting on an external dependency or approval. |
| Deferred | Postponed on purpose until a named gate. |
| Tech debt | Cleanup, security, reliability or maintainability. |

Evidence for "Implemented (static)" means a file and test name were read. The audit ran no tests and made no changes.

## 1. Feature inventory

### 1.1 Conversations, contacts, support, CRM, commerce, billing

| Capability | Status | Evidence / note |
|---|---|---|
| Inbox filters (view, channel, overdue) | Implemented (static) | `apps/conversations/views.py:71-119` |
| Conversation search | Missing | No `q` parameter in the inbox; contact search exists (`apps/contacts/views.py:73`) |
| Contact import (CSV) | Implemented but incomplete | `apps/contacts/services.py:208`; callable from API only, no UI route |
| Contact dedupe on import | Implemented (static) | `apps/contacts/importer.py:100-104` |
| Contact merge | Missing | Migration `0003` refuses auto-merge of duplicates |
| SLA enforcement, support tickets | Implemented (static) | `apps/support/tasks.py:9`, `sla.py:68-84`, tests `test_mark_breached_is_idempotent` |
| SLA / overdue alerts, conversations | Implemented but incomplete | `apps/conversations/tasks.py:22-70` writes an Event and re-routes; no notification sent |
| Assignment and routing | Implemented (static) | `apps/conversations/routing.py`, `test_routing.py` |
| Saved replies | Implemented (static) | `apps/conversations/models.py:720`, `test_saved_replies.py` |
| Follow-ups and reminders | Implemented (static) | `apps/conversations/followups.py`, `tasks.py:11`, `test_followups.py` |
| Conversation notes (UI) | Implemented but incomplete | Model exists; only the internal-note action creates one, and no view does |
| Support internal notes | Implemented (static) | `apps/support/models/message.py:59` |
| Deals and leads pipeline | Implemented (static) | `apps/crm/services.py`, `test_crm.py` |
| Order to payment (Flutterwave) | Implemented (static) | `apps/commerce/services.py:82,121`; `billing/views.py:611-660` checks amount and status |
| Payment link in chat | Implemented but incomplete | `apps/commerce/actions.py:40-51`; Flutterwave only |
| Refunds | Missing | `Order.Status.REFUNDED` exists but nothing sets it |
| Invoices and receipts | Missing | Stripe invoice webhooks only |
| Usage metering | Implemented (static) | `apps/billing/metering.py:300-432`, `test_metering.py` |
| Plan upgrade and checkout | Implemented but incomplete | `billing/views.py:279`; no in-app proration or downgrade flow |
| Entitlement limits | Implemented but incomplete | Applied to contact creation, automation, WhatsApp reserve. Inbound paths have no limit check, which looks right per the rule, but no test asserts it. |
| Staff audit log | Implemented but incomplete | `apps/core/audit.py` used in billing only, not in contacts, conversations, CRM, commerce or support |
| Contact opt-out (STOP) | Implemented (static) | `apps/whatsapp/tasks.py:153-175`, `contacts/models.py:138` |
| Customer data export and erasure | Missing | Only conversation delete exists |

### 1.2 Channels, automation, AI, publishing

| Capability | Status | Evidence / note |
|---|---|---|
| WhatsApp inbound media download | Implemented (static) | `apps/whatsapp/tasks.py:1101-1172` |
| WhatsApp inbound voice, ogg/opus for older Safari | Missing | Stored as-is; no ffmpeg on the inbound path. Matches pilot media gap. |
| WhatsApp outbound audio transcoding | Implemented (static) | `apps/conversations/outbound_media.py:7-157`, `test_outbound_media.py` |
| WhatsApp template gating and status sync | Implemented (static) | `apps/whatsapp/tasks.py:645-660, 1251-1293, 1438` |
| WhatsApp broadcasts with opt-out | Implemented (static) | `apps/whatsapp/campaigns.py:190`, `tasks.py:630-640` |
| Instagram DM triggers | Implemented (static) | Via `conversation.message_received`, `apps/automation/workflow_engine.py:114-135` |
| Instagram comment triggers and moderation | Implemented (static) | `apps/instagram/rules_views.py`, `test_phase3_acceptance.py` |
| Instagram App Review gate | Implemented but incomplete | Manual; no code checks review or live-mode status. Scopes rejected 2026-10-07, resubmission pending (Blocked). |
| Workflow triggers | Implemented (static) | Not WhatsApp-only: conversation, contact, lead, deal, order, email, business event |
| Interactive steps (buttons, lists) | Implemented (static) | `workflow_engine.py:84-85, 410-451`; list rejected on Instagram at publish time |
| Email campaigns and suppression | Implemented (static) | `apps/email/tasks.py:224-226, 613-644` |
| Email bounce and complaint handling | Implemented (static) | `apps/email/ses_webhooks.py:325-491` |
| Email inbound | Deferred | No inbound mail handling (`ChannelAdapter` seam not built) |
| Outbound customer webhooks | Implemented (static) | HMAC-signed `deliver_webhook`, retries and auto-disable: `apps/email/tasks.py:791-835` |
| Customer webhook catalogue | Implemented but incomplete | Event catalogue and test endpoint only in `apps/api/event_views.py:143-158` |
| Scheduled publishing and retries | Implemented (static) | `apps/publishing/tasks.py:35-134, 220, 443`; `apps/scheduler/drainer.py` |
| Facebook page publishing | Implemented (static), live API unverified | `apps/publishing/providers/facebook.py:29-64` |
| AI autonomy (default suggest-only) | Implemented (static) | `apps/ai/models.py:35-36`; kill switch `apps/ai/autonomy.py:185-192` |
| AI topic gating and sensitive-term hold | Implemented (static) | `apps/ai/autonomy.py:35-53`; default `auto_topics=[]` |
| AI fallback to a human | Implemented (static) | `apps/ai/autonomy.py:398-435`, `apps/ai/tasks.py:173-230` |
| Knowledge import | Implemented but incomplete | Text and CSV, plus website crawl with SSRF guard (`apps/ai/knowledge.py`). No PDF or file import. |
| Insights generation | Implemented (static) | `apps/insights/rules.py`, `engine.py:53`, `tasks.py:21`; beat schedule not verified |
| `apps/payments` | Implemented but incomplete (stub) | `apps/payments/providers/__init__.py`; real payments run through `apps/billing/gateways` |

### 1.3 Platform, security, operations

| Capability | Status | Evidence / note |
|---|---|---|
| 2FA (TOTP) | Implemented but incomplete | `apps/accounts/views.py:971-1086`; no login-time or staff-required enforcement |
| Login rate limiting and lockout | Implemented (static) | `apps/accounts/views.py:33-75`, `test_phase4_security.py` |
| Password reset | Implemented, unverified | Django views with custom templates |
| Session management | Unverified | 8h cookie only; no list or revoke sessions |
| SSO (SAML, OIDC, social) | Missing | No hits |
| API keys and scopes | Implemented but incomplete | `HasScope` enforced on template views only (`apps/api/views.py:164-284`) |
| API rate limiting | Implemented but incomplete | Per-key throttling exists; global default is empty, so it applies only where a view opts in |
| OpenAPI docs | Implemented (static) | `apps/api/urls.py:19-27` |
| SDKs (Python, Node, Go, PHP, CLI) | Implemented but incomplete | Test and retry coverage is uneven: Go 2 tests, CLI 3 tests with no retry logic, Python 3 retry hits |
| Error monitoring (Sentry) | Implemented, off by default | `settings.py:955-968` gated on `SENTRY_DSN`; silently off if the secret is empty |
| Health check | Implemented (static) | `/healthz` at `apps/core/views.py:108` |
| Backups and restore drill | Implemented, unverified | `docs/ops/backups.md`; drill outcome not evidenced |
| CI gate on deploy | Implemented, with a hole | `workflow_dispatch` bypasses CI success; the deploy job's environment is named `.env`, which looks misnamed |
| Migration safety in CI | Unverified | No `makemigrations --check` step |
| Secrets handling | Implemented, risk present | Empty GitHub secrets write blank values into `.env`; the encryption key falls back to one derived from the secret key |
| Accessibility and responsive | Implemented but incomplete | Viewport meta and about 225 aria-labels; contrast not checked |
| Onboarding tour | Implemented (static) | `templates/components/welcome_tour.html`, `apps/accounts/onboarding.py` |

## 2. Corrections to the first-pass seeds

- `apps/whatsapp/models/account.py` is **not dead**. It is imported by `apps/whatsapp/models/__init__.py` and holds the encryption helper. Do not delete it. Moving it is a valid cleanup.
- Inbound-message gating: the audit found no limit check on inbound paths and no test. Whether that is correct is a product decision. Customer inbound must never be dropped by a limit, so the right move is a test that asserts it, not a code change.
- Root clutter: `db.sqlite3` and `celerybeat-schedule` are git-ignored and local only. `_verify_out.txt` is tracked and empty and should be removed with `git rm`. `services/` (five tracked legacy files) is unverified and treated as legacy until its callers are checked.

## 3. Newly identified gaps (missing and valuable)

- Conversation search in the inbox.
- Contact merge, and a contact import UI.
- Refunds and invoices or receipts.
- Customer data export and erasure (a compliance need before Wave 2).
- Staff audit log coverage outside billing.
- Staff MFA enforcement and login-time MFA.
- Session list and revoke.
- Customer webhook delivery documentation and webhook management UI.
- Alerts for unassigned or SLA-breached conversations (a notification, not just an Event).
- Knowledge import from PDF or file.
- Downgrade and proration flow.
- Deploy gate fix: close the `workflow_dispatch` bypass and rename the environment.

## 4. Delivery policy

Tiers: **T0** current commitments and pilot-critical; **T1** evidence-gated (named pilot need, baseline, metric, decision threshold); **T2** deferred (released by a named gate); **T3** future opportunities needing validation first.

### Tier 0: current commitments, reliability, security, debt

| ID | Item | Owner | Gate or dependency |
|---|---|---|---|
| T0-1 | Publishing staging validation, then Steps 4.7D and 4.7E | Eng | Current priority; blocks release |
| T0-2 | Pilot readiness gate (`docs/ops/pilot-readiness.md`) | Ops | Before inviting business 3 |
| T0-3 | Implement `notify_owner` (Samantha stub) | Eng | Pilot Samantha; reuse `accounts/notifications.py` |
| T0-4 | Owner alerts for unassigned or SLA-breached conversations | Eng | Reuses the T0-3 channel |
| T0-5 | Login-time 2FA enforcement for staff | Eng | Freeze exception: security, before Wave 2 |
| T0-6 | Deploy gate: close `workflow_dispatch` bypass, fix environment name | Eng | None |
| T0-7 | Verify and record secrets: empty-secret detection for `SENTRY_DSN` and the rest | Eng | None |
| T0-8 | Inbound voice ogg/opus transcoding for older Safari | Eng | Pilot media requirement (in scope) |
| T0-9 | Test asserting inbound messages are never dropped by entitlement limits | Eng | None |
| T0-10 | Remove `_verify_out.txt`; decide `services/` legacy | Eng | Verify callers first |
| T0-11 | Resolve `whatsapp/tasks.py:60` TODO (ModuleSubscription check) | Eng | Superseded by feature catalogue; check and close |
| T0-12 | Migration check (`makemigrations --check`) in CI | Eng | None |
| T0-13 | Workstream B release (`ask_question`) | Product | Held on C gaps and IG review |
| T0-14 | Workstream E (HubSpot) release gate | Product | Pilot-need gate still open |
| T0-15 | Instagram App Review resubmission | Ops | Blocked on Meta |

### Tier 1: evidence-gated (build only when a named pilot needs it)

| ID | Item | Metric to justify | Gate |
|---|---|---|---|
| T1-1 | Payment link closing conversion to attributed revenue | Share of conversations ending in a paid order | Pilot names revenue as the blocker |
| T1-2 | Conversation search | Time to find a conversation (pilot report) | Pilot reports search friction |
| T1-3 | Contact merge and import UI | Duplicate contact count per pilot | Pilot reports duplicates |
| T1-4 | Notes UI on conversations | Notes used per conversation | Pilot asks for handover notes |
| T1-5 | Customer data export and erasure | Requests received | Before any Wave 2 business with EU or data-rights demand |
| T1-6 | Refunds and receipts | Refund requests per pilot | Pilot takes card payments at volume |
| T1-7 | Weekly report delivery by WhatsApp or email | Owner opens per week | Pilot retention evidence |
| T1-8 | Staff audit log across all apps | Support questions about "who changed X" | Pilot or security review asks |
| T1-9 | Session list and revoke | Requests from shared-device pilots | Security review |
| T1-10 | Vertical starter packs (`apps/verticals`) | Time to first live conversation | Two pilots in the same vertical |

### Tier 2: deferred, released by a named gate

| ID | Item | Released by |
|---|---|---|
| T2-1 | Email inbound (`ChannelAdapter` seam) | Pilot evidence that email conversations are needed |
| T2-2 | Messenger and other channels (Telegram, SMS) | Validation gate; SMS out of scope per UI spec §16 |
| T2-3 | Widened AI autonomy topics | Trust in suggest-only; gradual unlock per memory rule |
| T2-4 | Recipe builder, more autopilot | Pilot evidence (freeze) |
| T2-5 | Revenue intelligence | Growth-engine gate after attribution proof |
| T2-6 | Catalogue sync | Meta catalogue access researched |
| T2-7 | Embeddings and fine-tuning for AI knowledge | Retrieval quality measured as insufficient |
| T2-8 | P10 AI Email Copilot | Scheduled by product decision |
| T2-9 | Interactive steps and `whatsapp.received` on other channels | Instagram review passes or a pilot needs it |
| T2-10 | Phase 6 action loop (Insight to order attribution) | Insight-to-DM measurement in pilot data |
| T2-11 | Knowledge import from PDF or file | Pilot knowledge sources need it |
| T2-12 | Downgrade and proration flow | First paid downgrade request |
| T2-13 | SSO (SAML, OIDC) | Enterprise customer requirement |
| T2-14 | Mobile or PWA inbox | Pilot reports phone-first use |

### Tier 3: future opportunities (need validation first)

- Business intelligence OS framing (platform hardening roadmap) beyond the current loop.
- Public outbound webhook marketplace and a richer event catalogue.
- Vertical-specific automation templates beyond starter packs.
- Multi-language AI replies and knowledge.

## 5. Execution sequence (respects the freeze)

1. Finish in-flight gates: T0-1, T0-2, T0-13, T0-14.
2. Security and compliance floor before Wave 2: T0-5, T0-6, T0-9, T0-12.
3. Pilot-reported reliability: T0-3, T0-4, T0-8, T0-7, T0-10, T0-11.
4. Wait for Wave 1 validation gate. Pick up Tier 1 items only with a named pilot need.
5. Revisit Tier 2 when its named gate fires. Tier 3 stays in the parking lot until validation.

## 6. Conflicts and duplicates

- Entitlements memory and the freeze both say inbound is never gated. The code agrees, but no test proves it (T0-9).
- The pilot freeze allows media and voice, which makes T0-8 the one build inside the freeze that is not a bug fix. Confirm it is in scope.
- `notify_owner` (Samantha stub) and T0-4 overlap. One channel should serve both.
- Instagram App Review is listed under both platform hardening and publishing. Track it once, as T0-15.
- The whatsapp TODO refers to ModuleSubscription, which was superseded by the feature catalogue. Close the TODO rather than building the check.

## 7. Open questions for the user

1. Is inbound voice transcoding (T0-8) a pilot blocker, or a Wave 2 item?
2. Should staff MFA (T0-5) be mandatory for all staff, or only for owners and admins?
3. Who owns the Samantha `notify_owner` decision (T0-3), and which channel does the alert use?
4. Is customer data erasure (T1-5) needed before the first EU or UK business?
5. Confirm the deploy environment name before renaming (T0-6).
6. Should `services/` (legacy) be deleted, or kept until the callers are confirmed gone?
7. Are refunds (T1-6) handled outside the platform for now?

## 8. Verification notes

- Every "Missing" row came from a named search that returned nothing. Every "Implemented" row cites a file, and most cite a test name. No tests were run in this audit.
- Next step before relying on this list: run the test suite and spot-check ten rows, especially the "Implemented (static)" entries in billing and inbound gating.

## 9. Production Readiness: Real Client Onboarding

Status: workstream in progress (2026-10-16). Hardening only; no feature work. SSO stays documentation-only until a named customer needs it. Plan: `C:\Users\hp\.claude\plans\revise-the-plans-to-velvety-plum.md`.

Rule: the second factor must apply to every authentication path (password login, email links, invitations, password reset, session restore), not only the login form.

| ID | Gap | Current state and evidence | Launch-blocking | Completion criteria | Status |
|---|---|---|---|---|---|
| PR-1 | Email-link MFA bypass | `apps/accounts/views.py` `verify_email`: the `already_verified` shortcut skipped the token check and logged in; enrolled TOTP users got a full session from a link. Regression tests in `apps/accounts/tests/test_mfa_bypass.py`. | Yes | Token always checked; enrolled users routed to the 2FA step; tests pass | Fixed in code; tests pass; needs review |
| PR-2 | TOTP hardening | Done in code: single-use recovery codes stored as HMAC digests (`apps/accounts/recovery.py`, `RecoveryCode` model, migration 0014); verify path locks the device row and shares the django_otp lockout (`OTP_TOTP_THROTTLE_FACTOR = 5`); disable needs password plus a current factor; `django_otp.login()` called after `auth_login`. Tests: `apps/accounts/tests/test_2fa_hardening.py`. Open part: TOTP secret is still plaintext in `TOTPDevice.key`. In-place encryption is unsafe with the stock model (`CharField(max_length=80)`, read by `unhexlify`); it needs a separate migration design. | Yes | Encryption design approved and migrated with rollback rehearsal, or accepted risk recorded | Mostly done; encryption design open |
| PR-3 | MFA enforcement by role | Built, off by default: `apps/accounts/mfa.py` `MfaEnforcementMiddleware` covers every authenticated request, so password login, email links, invitations, reset and open sessions are all checked. Switches: `MFA_ENFORCEMENT_ENABLED` (env, default off), `MFA_ENFORCED_ROLES` (empty), `MFA_ENFORCE_STAFF` (False). Tests: `apps/accounts/tests/test_mfa_enforcement.py`. Email codes never count. | Yes (staff, owners, admins) | Enable per role only after operator reset is built and rehearsed, and recovery is proven on the target environment | Built; enforcement off; operator reset built (`manage.py reset_user_mfa`), rehearsal open |
| PR-4 | Passkeys / security keys | Not built | No (after PR-2) | Library comparison approved first; WebAuthn as a second factor | Open |
| PR-5 | Login rate limiting | Works for login form; key uses `REMOTE_ADDR` (proxy IP risk); read-then-set race | Yes | Atomic counter; production IP verified; covers reset, 2FA verify, invite, signup | Open |
| PR-6 | Password reset | Django defaults (3-day timeout); no throttle; error path leaks send exception (`apps/accounts/forms.py`) | Yes | 1-hour timeout; throttle; generic errors; replay/expiry/enumeration tests | Open |
| PR-7 | Sessions | 8h absolute only; no idle timeout; no list/revoke; SAMESITE implicit | Yes | Idle timeout; explicit SAMESITE; active-sessions list with revoke | Open |
| PR-8 | Tenant isolation | Scoping by hand per view; no cross-tenant tests for billing, commerce, hubspot, events; publishing variant lookups unscoped (`apps/publishing/views.py`) | Yes | Cross-tenant tests for those apps; scoped queries | Open |
| PR-9 | Deploy gate | `workflow_dispatch` deploys without a CI check; environment named `.env`; empty secrets written blank; no pre-migration dump (`.github/workflows/deploy.yml`) | Yes | Deploy only approved commits with successful CI; fail on empty required secrets; verified pre-migration dump; environment renamed | Open |
| PR-10 | Webhook secrets | Stripe, Flutterwave, Instagram verify token not fail-closed (`automator/settings.py`) | Yes | Refuse webhooks when secret is empty | Open |
| PR-11 | Backups and restore | Nightly dump and weekly restore job exist; no recorded restore drill; key backup undocumented; deploy reuses main AWS keys | Yes (operator evidence) | One real restore drill recorded; key backup documented; dedicated `BACKUP_*` credentials | Open (operator) |
| PR-12 | Accessibility | Good base; gray-400/500 text fails contrast; dark-theme focus ring 2.27:1; signup error semantics; `crm/lead_detail.html` labels; confirm dialog focus | Conditional | Fixes above; automated axe smoke at 375px and 1280px | Open |
| PR-13 | SSO | Not built | No (conditional) | Built only after a named customer requirement | Deferred |

Operator evidence to record separately from code tests: restore drill result, real client IP behind the proxy, failing-commit deploy test, `SENTRY_DSN` confirmed in production.


### Verification log (2026-10-16)
- PostgreSQL 16 (`automator-pg-verify`, port 55440, verification-only container): `test_2fa_pg_lock.py` passes with the row lock and fails with it removed (8 verifications instead of 1). Recovery-code and session tests pass on PostgreSQL too.
- CI: correctness step now runs `-m "not performance"` and blocks deploy; the timing test runs in a separate serial, non-blocking step.
- Open before enforcement is turned on: an operator reset command, rehearsed recovery on the target environment, and a decision on TOTP encryption (migration design).

- Accepted methods for mandatory MFA: a confirmed TOTP device only. Passkeys and security keys are **not yet accepted** for the staff or owner requirement; they remain Open (PR-4) and need dependency approval first. Email codes never count.
- Operator reset: `python manage.py reset_user_mfa --email <user> --ticket <ref> --approved-by <other staff email> [--confirm]`. Server-side only; needs a ticket, a second platform-staff approver (not the target), dry run unless `--confirm`; removes TOTP devices and recovery codes, ends the user's sessions, writes `auth.mfa_operator_reset` to the audit log, and emails the user. Tests: `apps/accounts/tests/test_reset_user_mfa.py`. Still open: the rehearsal on the target environment.

### Risk register: TOTP secrets stored in plaintext
- Risk: anyone who reads the database can generate valid codes for enrolled users.
- Decision owner: the user (Mwapsam), approved 2026-10-16. Implementation owner: the user (solo project), recorded 2026-10-16. Rehearsal owner: the user.
- Decision deadline: **decide before the lost-device rehearsal is signed off** (set by the user, 2026-10-16). Enforcement stays off until the decision is recorded.
- Constraint: in-place encryption is unsafe with the stock `django_otp` model (`max_length=80`, `unhexlify`). A compatible encrypted-device design with a tested backup, migration and rollback is required.
- Interim control: not verified. Database access restriction and backup encryption at rest must be confirmed before this risk is accepted.
- Runbook for enforcement rehearsal: `docs/ops/mfa-recovery-rehearsal.md`.

### Gate status (2026-10-16)
- Locally verified on the working tree based on `a2f4863`: correctness gate 3,338 passed, 21 skipped, 0 failed (`-m "not performance"`). Evidence: `scratchpad/gate.txt`.
- **Not yet verified in GitHub Actions.** The changes are uncommitted. After commit, record the CI run number and the tested revision here.
- PostgreSQL: 18 of 18 pass against PostgreSQL 16; evidence `scratchpad/postgres_evidence.txt`. Verification container removed.
- Performance: `test_import_5000_contacts` misses 60s in a serial full run (60.08s); clean-database benchmark is 27.4–27.8s across three runs (see below). It stays in the separate, non-blocking performance step.
- TOTP secret protection: deadline set (decide before the rehearsal sign-off). Decision and implementation owner recorded: the user (solo project).
- Performance benchmark (clean database, test alone, 3 runs): 27.58s, 27.75s, 27.44s against a 60s budget. Machine: Intel i5-1335U, 12 logical CPUs, 8 GB RAM with about 950 MB free at the time. Evidence: `scratchpad/import_benchmark.txt`. Hypothesis, not confirmed: the 60.08s serial-run miss may reflect memory pressure (about 950 MB free) or accumulated state in a long full run. No diagnosis yet. The budget is not changed.

### TOTP secret protection: revised scope (2026-10-16)
- The owner reports that no TOTP devices exist in any environment. Local dev database: 0 devices (verified). Production and target: **owner claim, not yet verified**. Confirm with a read-only count before relying on it.
- With no existing rows, the data migration is not needed. The work becomes: an encrypted device model (or swapped `TOTP_DEVICE` model) that stores secrets encrypted from first enrolment, with key provisioning, rotation and backup, and a rollback that is a schema revert while the table is empty.
- Enforcement must still wait for this design and the rehearsal, because enforcement will create devices.

- Draft design (no production changes): `docs/design/totp-encryption-migration.md`. Production has 1 device, so the design is an expand, switch, contract migration with a sidecar table.
- **Blocking finding:** `.env` `DJANGO_SECRET_KEY` contains `$`, which Compose interpolates, so Django may receive a different key. Production `deploy.yml` writes the same unquoted format; owner to confirm. Fix as a planned change, because it logs sessions out.
- `DJANGO_SECRET_KEY` `$` interpolation warning: **owner decision, 2026-10-16: ignore for now.** Not fixed. Risk remains open (Compose may pass a different key to Django); revisit if session or token behaviour looks wrong.
- TOTP sealed storage implemented in development (`apps/accounts/totp_storage.py`, default mode `plaintext`). Production unchanged. Details: `docs/design/totp-encryption-migration.md` section 9.
- Review fixes (2026-10-17): spaced TOTP codes verify again (regression from `.strip()`; tests in `test_2fa_hardening.py`); threaded tests carry the `serial` marker and run in a blocking serial CI step, outside the xdist run; the 2FA challenge state is written in one place (`begin_second_factor` in `apps/accounts/views.py`), used by login, email verify and the enforcement middleware.
- Not re-run on PostgreSQL after these fixes: the verification container was removed. Re-run the serial and PostgreSQL tests before relying on them.
- PostgreSQL re-run after review fixes: MFA/TOTP files 64 passed, 0 skipped. Full accounts suite: same six baseline failures as `a2f4863`. Details: design doc section 11.

### Deployment state (verified 2026-10-17)
- Production runs `98b647b`. Migrations 0014 and 0015 applied. Enforcement inert: flags False, roles empty, staff off. TOTP storage plaintext.
- Earlier entries in this file that say the work is uncommitted or not deployed are superseded by this line.
- CI result for `98b647b`: **passed** (reported by the owner 2026-10-17; run number not yet recorded). Deploy was triggered by the CI success gate, not by manual dispatch.
