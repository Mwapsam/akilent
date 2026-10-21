# WhatsApp campaigns and contacts — production-readiness audit

Date: 2026-10-19. Scope: WhatsApp campaigns, customers (contacts) and customer lists. Email campaigns are out of scope.

## Verdict

**Before the fixes, this was not production-ready.** The main problem came before sending: an owner could not build an audience that a campaign would actually reach.

- Customers added by hand, imported or synced had no WhatsApp identity, so every campaign skipped them as "no WhatsApp number". Only people who had messaged the business first could receive a campaign.
- Lists could be filled only by tag or one person at a time. There was no paste, CSV, bulk select, rename or delete, and only 50 members were shown.
- Consent could not be recorded or seen. Promotions require opt-in, and the only way to opt in was the customer texting START, so promotional campaigns reached almost no one.
- The send path also lost failures from its stats, could hang forever, and could send twice.

Release 1 (below) fixes all of these. Releases 2 and 3 were built on 2026-10-21 (see below); they still need a Postgres/staging run before production.

## Findings and status

| ID | Finding | Status |
|---|---|---|
| A1 | Customers created outside WhatsApp inbound got no `WhatsAppContact`, so campaigns skipped them | **Fixed.** `ensure_whatsapp_identity` + a post-commit Contact signal, a campaign-time safety net, and the `backfill_whatsapp_identities` command |
| A2 | Lists were filled by tag or one at a time; no bulk, paste, CSV, profile add, rename/delete; 50-member cap | **Fixed.** `apps/contacts/list_building.py`; paste, CSV, bulk select (including "all matching"), "everyone who messaged you", picker, profile add/remove, rename/delete, paginated members |
| A3 | No way to capture or show opt-in; every WhatsApp customer showed a green *email* "Subscribed" badge | **Fixed.** Owner attestation on bulk add, per-customer opt in/out on the profile (STOP is never overridden), and one WhatsApp status vocabulary everywhere (`apps/contacts/reachability.py`) |
| B1 | Messages that failed for good vanished from delivery stats | **Fixed.** Grouped DB aggregate that includes FAILED recipients plus separate held and unconfirmed counts |
| B2 | A media-header template or any chunk error left a campaign "Sending" forever | **Fixed.** Media-header templates refused up front; chunk errors retry, then fail the campaign with a reason; the sweeper catches SENDING with no progress |
| B3 | Promotions to customers who hadn't opted in failed one by one at send time | **Fixed.** Skipped up front as `NOT_OPTED_IN` and shown before sending |
| B4 | Double submit sent the broadcast twice | **Fixed.** `submission_key` plus a partial unique constraint |
| B5 | The Insights "campaign" action re-dispatched a finished campaign and recorded success | **Fixed.** Recorded as a failure with the reason; scoped to the account |
| B6 | Campaign creation wasn't atomic | **Fixed** |
| C1 | Adding a customer with an email another customer owns returned a 500 | **Fixed.** Friendly error naming the owner |
| C8 | Duplicate list or segment name through the API returned a 500 | **Fixed.** 409 `already_exists` |
| D1 | Copy drift ("send a campaign to it" with no path, a "targeted segments" promise, no help guide) | **Fixed.** Lists link to the composer, the objective copy is truthful, and there is a new help guide `whatsapp-campaign-lists` |

## What changed for the owner (Release 1)

- **Customers page:** WhatsApp status column and filter, list filter, checkbox selection with "select all N matching", and a bulk "Add to list" bar. "Add customers" offers one at a time, paste numbers or upload a CSV.
- **Lists:** a "can receive offers" count on the index. The list page has:
  - a readiness bar (opted in / updates only / opted out / no WhatsApp), with each part clickable to filter;
  - "Add people" with five methods;
  - rename, delete (blocked when a campaign used the list), and search and pagination of members;
  - "Send WhatsApp campaign".
- **Paste / CSV:** CSV preview in the browser before upload, a consent attestation, and a result page that shows bad numbers so they can be fixed inline, plus a downloadable CSV of rejected rows.
- **Customer profile:** a WhatsApp card (status, since when, how, and mark opted in/out), a Lists card (add/remove), and "Open conversation".
- **Campaign composer:** a stepped flow (who → message → review).
  - List cards show reachability.
  - Templates show Promotion/Update; templates with a picture or file header are disabled with the reason.
  - Every blank has a fallback.
  - A live WhatsApp-bubble preview uses a real customer.
  - A server-computed "who gets it" breakdown applies the same rules as the send.
  - The button reads "Send to N customers".
- **Campaigns home:** a first-run readiness checklist, and a table with sent, delivered, read, failed and not-sent counts per campaign.
- **Campaign detail:** auto-refreshing progress and stats, a "Not sent" section with plain reasons and a fix link each, a recipients table filterable by outcome, a failure alert with the cause, and an audit log entry on send.

## Release 2 — campaign lifecycle and segments (built 2026-10-21)

- **Drafts:** anyone on the team can save a campaign as a draft; only owners and admins send, schedule, pause or cancel.
- **Scheduling:** a day, time and time zone. Recipients are chosen when it fires (beat task `dispatch_due_campaigns`, every minute, row-locked so it never starts twice), so people added in the meantime are included. A scheduled send that finds an empty list or a plan problem fails with the reason instead of sending.
- **Pause / resume / cancel:** fan-out stops at the next chunk, and the outbound queue holds back a paused campaign's messages, even after fan-out has finished. Cancel withdraws what hasn't gone out ("not sent: cancelled").
- **Duplicate** (new draft) and **Retry failed** (only the failed recipients, minus anyone who has since opted out).
- **Send a test to my WhatsApp:** one message to the owner's number; no campaign or customer record.
- **Detail page:** state-aware actions, history timeline, "who it would reach" for drafts and scheduled campaigns, recipients CSV export (formula-safe).
- **Segments:** a rule builder at Customers → Segments with a live count and sample. New rules include WhatsApp status, can-get-WhatsApp, has phone, has ordered, first seen, stage and custom fields. Segments work on the Customers page, as "Save as segment" from a filter, as a campaign audience (re-evaluated at send), and through "save who matches now as a list".

## Release 3 — contacts, delivery, API, cleanup (built 2026-10-21)

- **Merge** (`apps/contacts/merge.py`): everything that points at a customer moves across, found from the model graph. One-per-customer rules are resolved: a second open lead is closed as lost, a clashing workflow run is cancelled, duplicate list or campaign rows are dropped. Includes duplicate suggestions (profile banner and a "Possible duplicates" page) and "different people" dismissal. Instagram customer creation is now row-locked (no double-create) and copies the Instagram name.
- **Case-insensitive email uniqueness:** migration `contacts 0010` refuses to run while duplicates exist. Run `manage.py check_contact_emails --merge` first.
- **Profile:** one activity timeline (WhatsApp/Instagram messages, orders, campaign sends, notes, events), private notes, custom fields with labels and inline editing, Instagram handle, related counts, and an overflow menu (Merge, Delete).
- **Custom fields settings** (Customers → More → Customer details you keep): add, edit, archive and restore. **The Workstream C release gate is still open; decide before announcing it.**
- **Self-service delete** (`apps/contacts/erasure.py`), shared by the profile, bulk delete, the API and the operator console. It removes WhatsApp and Instagram identities, Instagram comments, DMs and queued replies, and queues a HubSpot archive. Requires typing "delete"; owner/admin only; audit-logged. The /data-deletion/ page describes it.
- **Customers page:** sortable columns, bulk tag/untag, bulk delete, and CSV export of the current filter (owner/admin, formula-safe; the console export is now formula-safe too).
- **Audit log:** customer create, edit, delete, merge, export, opt-in/out, and list create/rename/delete.
- **Delivery** (`apps/whatsapp/delivery.py`):
  - campaign messages go out at low priority, after replies;
  - each drain pass is shared fairly between businesses, with a configurable batch (`WHATSAPP_OUTBOUND_BATCH`);
  - fan-out kicks the drain straight away;
  - campaigns follow Meta's messaging tier, stored from the quality webhook: a campaign over today's room is spread over days;
  - quiet hours, 08:00–20:00 by default, configurable at Campaigns → Sending hours;
  - held messages older than 72h are dropped as "plan limit — not sent late";
  - numbers known to be unreachable are skipped;
  - a campaign stops when Meta pauses or rejects its template;
  - the owners are alerted when a campaign's failure rate is high;
  - only one retry chain waits while WhatsApp is off.

  A 10,000-recipient load test (`performance` marker) passes.
- **API:**
  - phone-only `POST /v1/contacts`;
  - contacts include phone, WhatsApp status, tags and lists;
  - attested opt-in and opt-out through `PATCH`;
  - list detail, rename and delete, and members add/remove (1,000 per request, by id or phone);
  - WhatsApp campaigns: create, schedule or draft, get with stats, and pause/resume/cancel/send now, with `Idempotency-Key` and the new `whatsapp:campaigns` key scope;
  - docs page and changelog entry;
  - a fix so `Idempotency-Key` replays with datetimes no longer break the request.
- **Cleanup:** publishing "Campaigns" are now "Content themes" (with edit, delete and dates). `importer.py` and `quality.py` are removed; the load test uses the live import path. The `ContactPhone`/`ContactEmail` models are kept, because pre-send checks read `whatsapp_status`.

Not done, deliberately:

- Erasure that also blocks the number from being re-created later is a legal question, still open.
- The contacts API is still gated on the plan's email-API feature, which keeps WhatsApp-only plans out. That's a pricing decision.

## Operational notes

- Run `python manage.py backfill_whatsapp_identities --dry-run`, then without `--dry-run`, once per environment after deploy. It is safe to re-run and never changes opt-in.
- Migrations:
  - Release 1: `whatsapp 0023` (skip reason) and `0024` (submission key).
  - Release 2: `whatsapp 0025` (campaign lifecycle).
  - Release 3: `whatsapp 0026` (delivery scale), `contacts 0010`–`0012` (case-insensitive email, notes, sort index) and `hubspot` (contact-delete job).
- Before `contacts 0010`: run `python manage.py check_contact_emails`, then add `--merge` if it reports groups.
- New beat tasks:
  - `dispatch_due_campaigns` (every minute);
  - `cancel_stale_campaign_holds` (hourly);
  - `alert_on_campaign_failure_rate` (every 15 minutes).

  All are on the `campaigns` queue.
- New settings (all optional): `WHATSAPP_CAMPAIGN_QUIET_HOURS_DEFAULT` (1), `WHATSAPP_OUTBOUND_BATCH` (50), `WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS` (72).
