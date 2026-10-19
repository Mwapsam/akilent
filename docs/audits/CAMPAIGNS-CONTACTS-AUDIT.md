# WhatsApp campaigns and contacts — production-readiness audit

Date: 2026-10-19. Scope: WhatsApp campaigns, customers (contacts) and customer lists. Email campaigns are out of scope.

## Verdict

**Before the fixes, this was not production-ready.** The main problem came before sending: an owner could not build an audience that a campaign would actually reach.

- Customers added by hand, imported or synced had no WhatsApp identity, so every campaign skipped them as "no WhatsApp number". Only people who had messaged the business first could receive a campaign.
- Lists could be filled only by tag or one person at a time. There was no paste, CSV, bulk select, rename or delete, and only 50 members were shown.
- Consent could not be recorded or seen. Promotions require opt-in, and the only way to opt in was the customer texting START, so promotional campaigns reached almost no one.
- The send path also lost failures from its stats, could hang forever, and could send twice.

Release 1 (below) fixes all of these. Releases 2–3 are planned, not built.

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

## Planned (not built)

- **Release 2:** draft, schedule, pause, cancel, duplicate and retry-failed for campaigns; "send test to me"; segment targeting with a builder UI.
- **Release 3:**
  - merging WhatsApp and Instagram duplicates;
  - self-service delete and export (with formula escaping);
  - notes and custom-field editing;
  - delivery priority, per-account fairness, Meta tier awareness, quiet hours and stale-hold cancel;
  - phone-first contacts API and a WhatsApp campaign API;
  - renaming publishing "Campaigns";
  - removing the unreachable `importer.py`/`quality.py`.

The full plan is in the session plan file. Each release stops for review.

## Operational notes

- Run `python manage.py backfill_whatsapp_identities --dry-run`, then without `--dry-run`, once per environment after deploy. It is safe to re-run and never changes opt-in.
- Migrations: `whatsapp 0023` (skip reason) and `0024` (submission key).
