# Security facts the code can't show

The public Security page (`/security/`, `templates/legal/security.html`) and the landing page's
trust section state only what is true today. Most of it is provable from the repository; this file
records the infrastructure facts that live outside it, as confirmed by the owner, so the claim
register (`docs/marketing/messaging.md` §10) has something to point at.

**When any of these changes, update this file, the Security page and the landing trust section in
the same change.**

| Fact | Status | Confirmed |
|---|---|---|
| Hosting | The application runs on Amazon Web Services (AWS EC2); files and backups are in Amazon S3; static files are served through Amazon CloudFront. Cloudflare sits in front of akilent.com. | Owner, 2026-11-07 (EC2 also stated in the privacy policy; CloudFront and Cloudflare visible in live response headers) |
| Data region | **Not published.** The Security page names AWS without a region until the owner confirms it. | Owner, 2026-11-07 |
| Encryption at rest | EBS encryption is on for the production database volume. Backups use S3 server-side encryption (SSE-S3, `docs/ops/backups.md`). Channel access tokens are also encrypted field by field (`apps/accounts/fields.py`). | Owner, 2026-11-07 |
| Backups | Running in production: nightly at 02:00 UTC, kept 30 days, restore-tested every Sunday (`docker/backup/`, `docs/ops/backups.md`). | Owner, 2026-11-07 |
| GDPR / data processing agreement | **Not offered yet.** Leave GDPR and DPA wording off public pages until a DPA exists. | Owner, 2026-11-07 |
| Meta Tech Provider | Meta Tech Provider (confirmed by owner). Shown as plain text ("Meta Tech Provider"), never with Meta's or WhatsApp's logo: Meta publishes no Tech Provider badge and its brand rules forbid implying endorsement. | 2026-11-07 |
| Status page | Linked only when `STATUS_PAGE_URL` is set (a hosted status page). | 2026-11-07 |
