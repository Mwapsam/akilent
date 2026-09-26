# WhatsApp operations runbook

The WhatsApp vertical talks directly to the **Meta WhatsApp Cloud API** (no BSP).
It is gated by `WHATSAPP_ENABLED`; when false, URLs, nav, the Celery schedule and
startup secret validation are all skipped.

## Meta Tech Provider Program alignment

This platform operates as a **Tech Provider** (ISV) under Meta's Tech Provider
Program, not as a Solution Partner / BSP. What that means for the implementation:

- **Onboarding is Embedded Signup.** End businesses connect their own WhatsApp
  Business Account (WABA) through the popup hosted at `/whatsapp/numbers/`
  (`WHATSAPP_APP_ID` + `WHATSAPP_CONFIG_ID`). `connect_complete`
  (`apps/whatsapp/numbers.py`) exchanges the OAuth `code` for a
  business-integration token, subscribes our app to the WABA, and **registers
  the phone number** on the Cloud API (`POST /{phone_number_id}/register`,
  setting a stored two-step-verification PIN). Manual `phone_number_id` + token
  entry stays available as a fallback.
- **Billing = Path 2 ("Tech Provider Only").** The end client's WABA is billed
  by **Meta directly** for conversation charges (they attach their own payment
  method during Embedded Signup). This platform bills **only for the
  value-added SaaS solution** via `Plan` tiers (the feature catalog, `apps/billing/features.py`). As a Tech
  Provider we **must not** extend a line of credit to businesses or add any
  markup on conversation fees — `apps/billing` has no per-conversation resale
  pricing and none should be added. `Plan.max_conversations_per_month` is a
  usage *quota* for the SaaS tier, not a billed rate.
- **App Review scopes:** `whatsapp_business_messaging` (send/receive) and
  `whatsapp_business_management` (templates, phone assets, subscribe). Both are
  required before Embedded Signup works for real clients.
- **Per-client credentials.** Each `WhatsAppBusinessNumber` stores its own
  encrypted `access_token` (+ `verification_pin`); the provider factory resolves
  the token per account. There is no shared platform sending token.
- **Registration & PIN lifecycle.** `connect_complete` calls
  `register_phone_number` once, with a freshly generated 6-digit PIN it then
  stores (`verification_pin`). Registration state and the PIN are properties of
  the *number*, not the token:
  - **Token rotation** (error 190 — see below) swaps `access_token` only; the
    number stays registered and the stored PIN is unchanged. Re-running
    registration after a rotation is *not* required; if you do, pass the
    **stored** PIN (`register_phone_number` with a different PIN fails once
    two-step verification is on).
  - **A number moving to another account**, or a number whose two-step PIN was
    changed out-of-band in Meta Business Manager, needs re-registration with the
    correct PIN — update `verification_pin` to match.
  - Manual-entry numbers (`numbers_create`) are assumed already registered by
    whoever supplied the token; `verification_pin` stays null.
- **Account sharing / joint BSP solutions** (Tech Provider manages templates
  while a BSP handles sending + billing) is a future Meta capability; the data
  model (`waba_id`, `business_id` per number) already accommodates it, but no
  code path depends on it today.

## Enabling it

0. **Meta-side prerequisites** (one-time, per environment — none of these are in
   this codebase):
   - **Business Verification** complete in Meta Business Manager. This is the KYB
     check and is *separate from App Review*; until it passes, Embedded Signup
     (step 3) fails for real clients even though steps 1–2 look fine.
   - **App Review** approved for `whatsapp_business_messaging` and
     `whatsapp_business_management`.
   - Tech Provider agreement accepted in the Meta Developer Portal.
1. Set the env vars (see `.env.example` → WhatsApp section):
   - `WHATSAPP_ENABLED=True`
   - `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` — app-level webhook secrets
     (required when `WHATSAPP_ENABLED` and not `DEBUG`).
   - `WHATSAPP_APP_ID`, `WHATSAPP_CONFIG_ID` — Embedded Signup onboarding link.
   - `FIELD_ENCRYPTION_KEY` — encrypts per-number access tokens at rest.
2. Point the Meta app's webhook at `POST /whatsapp/webhook/`; the GET handshake
   answers `hub.challenge` when `hub.verify_token` matches `WHATSAPP_VERIFY_TOKEN`.
3. Onboard a number: dashboard → WhatsApp numbers → Embedded Signup, or register
   `phone_number_id` + system-user token manually. A number needs `waba_id` set
   for template sync to work.
4. Nothing to enable: WhatsApp (connection, inbox, templates) is a core feature on every
   plan. Campaigns and verification codes come with the plan, set in /manage/plans/features/.

## Workers & queues

Run Celery workers covering these queues:

| Queue      | Tasks |
|------------|-------|
| `whatsapp` | `process_whatsapp_event`, `mark_read` |
| `outbound` | `drain_outbound_queue` |
| default    | `close_expired_conversations`, `download_media`, `sync_templates`, `alert_on_whatsapp_failure_spike` |

Beat schedule (added only when `WHATSAPP_ENABLED`):

| Task | Interval |
|------|----------|
| `drain_outbound_queue` | 10 s |
| `download_media` | 60 s |
| `close_expired_conversations` | 1 h |
| `sync_templates` | 30 min |
| `alert_on_whatsapp_failure_spike` | 15 min |

`process_whatsapp_event` and `drain_outbound_queue` run `acks_late` /
`reject_on_worker_lost`, so a killed worker re-delivers the job.

## Outbound send path

`apps.whatsapp.api.send_message(account, contact, text)` (or automation's
`send_whatsapp_message` action) creates a `QUEUED` `OutboundMessage` carrying a
`payload` dict and an `idempotency_key`, then triggers a drain.

`drain_outbound_queue` for each due message:

1. recovers rows stuck in `SENDING` > 10 min;
2. `_authorize_send` — policy gate (see below);
3. mirrors the message into a `MessageLog(direction=out)` and links it;
4. flips to `SENDING`, acquires a per-number rate-limit token, calls the provider;
5. on success stamps the provider `message_id` onto the `MessageLog` so status
   webhooks (`sent`/`delivered`/`read`/`failed`) reconcile via
   `MessageLog.apply_status_update`;
6. on failure `mark_failed` — exponential backoff (1,2,4,8 min, max 5 attempts),
   or straight to `FAILED` when the error is terminal.

### Policy gate (`_authorize_send`)

| Situation | Result |
|-----------|--------|
| `payload._consent_ack` (STOP confirmation) | always allowed |
| contact `opt_in_status == opted_out` | `CONTACT_OPTED_OUT` (terminal) |
| free-text / media, 24h window closed | `OUTSIDE_WINDOW_NO_TEMPLATE` (terminal) |
| template, not linked / not `APPROVED` | `TEMPLATE_NOT_APPROVED` (terminal) |
| marketing template, contact not `opted_in` | `MARKETING_REQUIRES_OPT_IN` (terminal) |

Terminal failures set `OutboundMessage.status = FAILED` without burning retries;
`error_code` holds the Meta code when present.

## Consent

Inbound single-word `STOP` / `UNSUBSCRIBE` / … (`WHATSAPP_STOP_KEYWORDS`) opts the
contact out, closes the conversation and queues one confirmation reply.
`START` / `SUBSCRIBE` / … opts back in. State lives on `WhatsAppContact`
(`opt_in_status`, `opt_in_at`, `opt_out_at`, …) and is visible/editable in admin.

## Media

Inbound webhooks carry only a `media_id`. `download_media` fetches a short-lived
URL via the provider, downloads the bytes (capped at `WHATSAPP_MAX_MEDIA_BYTES`)
and stores them through the default Django storage backend, setting
`MessageLog.media_file` / `media_size`. A row that fails 5× stops being retried
(`media_attempts`, `media_error`).

Outbound media: put the file in storage and send a payload with `media_path` +
`mime_type`; `_send_outbound` uploads it (`provider.upload_media`) then sends.

## Templates

`sync_templates` pulls every active WABA's templates from Meta every 30 min and
upserts `approval_status` / `category` onto `MessageTemplate` (keyed by
`whatsapp_template_name` + `language_code`). Meta's
`message_template_status_update` webhook updates a row in real time. Only
`APPROVED` templates can be sent.

## Rate limiting

One token bucket per `phone_number_id`, rate = `WhatsAppBusinessNumber.
send_rate_limit` (default 20/s; raise toward Meta's 80/1000-per-second tier as the
number's quality rating allows). Redis-backed when `REDIS_URL` is set, else a
per-process fallback (safe only for a single worker).

## Meta error codes

| Code | Meaning | Handling |
|------|---------|----------|
| 130429 / 131056 / HTTP 429 | throughput / pair rate limit | retryable, backoff |
| 131047 | re-engagement required (outside window) | terminal |
| 131026 | undeliverable | terminal |
| 131051 | unsupported message type | terminal |
| 132xxx | template errors | terminal |
| 133004–133016 | account / registration errors | terminal |
| 133010 | phone number not registered | terminal — but see the note in `_NON_RETRYABLE_CODES`: a burst right after onboarding may just be Meta-side propagation lag, which the failure-spike alert will surface |
| 190 | access token expired/invalid | terminal — rotate the number's token (registration/PIN survive; see "Registration & PIN lifecycle") |

Any Meta code **not** in `_NON_RETRYABLE_CODES` (`apps/whatsapp/providers/meta.py`)
is treated as retryable with backoff; HTTP 400/401/403 are terminal regardless.

## Failure-spike alert

`alert_on_whatsapp_failure_spike` pages Slack (`apps.billing.slack.post_message`)
when ≥ 20% of terminal `OutboundMessage`s in the last 60 min are `FAILED`
(min volume 30), with a 1 h cooldown. First check: access-token validity and
[Meta platform status].

## Health checks

- Outbound backlog: `apps.whatsapp.api.outbound_queue_depth()`.
- Failed events: `WebhookEventLog.objects.filter(processed=False, attempts__gte=3)`.
- Stuck media: `MessageLog.objects.filter(media_id__isnull=False, media_file="",
  media_attempts__gte=5)`.
