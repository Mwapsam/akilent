# Content Studio competitive capability roadmap (Facebook and Instagram, in phases)

## Context
An audit on 2026-11-01 compared Content Studio with Buffer, Later, Hootsuite, Sprout, Metricool, Planable and Meta Business Suite. Content Studio has three capabilities that are **tested, not yet shown in production**:
- recovery when publishing fails;
- honest metric states;
- crediting a post with the WhatsApp chat, lead and order it started.

It is behind on formats, media, scheduling productivity, analytics, AI, collaboration and mobile. It also has **Facebook format bugs** (below) that weaken any reliability claim until they're fixed.

User decisions:
- Go deep on Facebook and Instagram, with no new networks.
- The freeze is lifted for Content Studio. Each phase gets its own go-ahead and a release flag.
- **Close most gaps with competitors.**

**Feature filter:** does it help a business turn social activity into measurable commercial outcomes? Phases follow it. The exceptions are the stopgap and Phase 0a, which are **exempt as correctness fixes**: the composer offers formats that publish the wrong thing on Facebook.

**Meta App Review (App Dashboard plus MCP, 2026-11-01):**
- Submission `1086006971089663` is **"Review in progress", not approved**. Meta says most submissions are reviewed within 20 days.
- All 14 Facebook and Instagram scopes are **first-time "New requests"**: `pages_show_list`, `pages_manage_metadata`, `pages_messaging`, `business_management`, `pages_read_engagement`, `pages_read_user_content`, `pages_manage_engagement`, `read_insights`, `instagram_business_manage_insights`, `pages_manage_posts`, `instagram_business_content_publish`, `instagram_business_basic`, `instagram_business_manage_comments`, `instagram_business_manage_messages`.
- The only earlier submission (4 Oct 2026, WhatsApp) was approved, and the WhatsApp scopes are up for renewal in this one.
- The MCP's "REJECTED" `grant_status` on these scopes only means "not granted yet". It isn't a past rejection.
- **No new scopes are needed for Facebook Reels or Stories, provided this submission is approved** (`pages_show_list`, `pages_read_engagement`, `pages_manage_posts`).
- Meta also re-reviews the existing WhatsApp access, app icon and privacy policy URL in this submission, so a problem there could delay everything.
- **Scope freeze during review:**
  - No phase adds a Meta scope, use case or app setting until this submission is approved.
  - Any feature found to need a new scope (most likely benchmarking through `business_discovery`) is recorded as **not closing**, not changed mid-review.
- **Timeline:** 20 days is Meta's usual figure, not a promise, and `business_management` and `pages_messaging` are the scopes most likely to draw questions. **The pilot on real Facebook and Instagram accounts starts after approval, not on a date.**
- Until approval, live verification of every phase runs only on the **dedicated Akilent test Page plus an Instagram professional account with a role on app `1080201821670178`**. The user is setting these up.

### Can users post Reels today? (checked in code)
| Format | Instagram | Facebook |
|---|---|---|
| Reel | **Yes**: `media_type=REELS` with cover, thumb offset and share-to-feed (`providers/instagram.py:98-115`) | **No**: an ordinary `/videos` video (`services.py:1088`) |
| Story | Yes, image and video (`services.py:512`) | **Bug**: `STORY` → `else → create_photo_post` (`services.py:1090`), so video stories fail |
| Carousel | Yes | **Bug**: only the first image is posted |
| Video limits | Not checked before publishing | Not checked before publishing |

### Meta facts (checked with the MCP, v25)
- **Facebook Reels:** `/{page-id}/video_reels` with `upload_phase` start → upload → finish (`video_state=PUBLISHED`). Limits: 3–90 s, 9:16, at least 540×960, 24–60 fps. Error codes 1363128 (duration) and 1363129 (frame rate). **At most 30 API-published Reels per Page in any rolling 24 hours.**
- **Facebook Stories:** `photo_stories` takes an unpublished `/photos` id; `video_stories` is start, upload, finish. Video up to 60 s. **Media already used in a published post can't be reused as a Story.**
- **Hosted files** must be fetchable by `facebookexternalhit`. The upload can also take bytes directly (resumable with `offset`) instead of a `file_url`.
- **Instagram story insights** last only 24 hours, and fewer than 5 viewers gives error code 10. The Facebook story insights window is still to be confirmed with the MCP.
- **Our media URLs** are public CloudFront URLs with no signature or expiry (`AWS_QUERYSTRING_AUTH = False`, `AWS_S3_CUSTOM_DOMAIN`, `settings.py:243-258`).
  - A Reel scheduled days ahead can't hit an expired URL.
  - The trade-off: **unpublished and scheduled media can be reached by anyone with the URL.** The random UUID in the key makes guessing impractical. The audit states this.
  - Check that CloudFront's robots.txt, and any WAF or hotlink rule, doesn't block `facebookexternalhit`.
- **Still open, confirm with the MCP before coding:**
  - Instagram Reel, Story and feed-video limits (duration, ratio, size, fps);
  - Facebook multi-photo posting (unpublished `/photos` plus `attached_media`) and its photo limit;
  - the Facebook story insights window;
  - the error code for the Facebook Reel rate limit.
  `constraints.py`, `create_multi_photo_post` and the error mapping depend on these.
- **ffmpeg/ffprobe** are in the single Dockerfile that web, `celery_worker` (publishing), `celery_ai` and beat all build from (`docker-compose.yml`).

### Gap coverage: every audit gap and where it closes
| Gap (who has it) | Closes in | Evidence level now / notes |
|---|---|---|
| Facebook Reels, Stories, carousel (all) | Stopgap → 0a | Bug today |
| Video checks before publishing (Later, Buffer) | 0a | |
| Large video uploads (all) | 0c | |
| Link-in-bio (Later Linkin.bio, Buffer Start Page) | 0b → 1 | |
| Sales / ROI reporting (Hootsuite, Metricool, ManyChat) | 1 | |
| Month view, drag and drop, slot queue, best time (all) | 2a | |
| Account analytics, report export, email reports (all) | 2b | |
| Competitor benchmarking (Metricool, Sprout) | 2b, **conditional** | Only if Instagram `business_discovery` works with Instagram Login tokens |
| Recurring / evergreen, bulk CSV import (Hootsuite, Metricool) | 2b | With safeguards |
| Media library, simple editor, templates, hashtag sets (Later, Buffer, Hootsuite) | 3 | |
| Stock images (Hootsuite, Later) | 3, **conditional** | Depends on the API terms (see Phase 3) |
| Canva (Buffer, Later, Hootsuite) | 3, **conditional** | Canva Connect access |
| AI rewrite, brand voice, hashtags, image generation (Hootsuite, Buffer, Later) | 4 | Image generation needs a decision on provider and cost |
| AI ideas from results (Hootsuite) | 4, conditional | Needs Phase 1 data |
| External client approval links, inline comments (Planable, Sprout) | 5 | |
| Mobile (all have native apps) | 5, **partial** | PWA only, not native. iOS web push works only when the app is installed to the home screen |
| Comment and DM inbox (Hootsuite, Sprout, Meta) | Already built | **Tested** (cite suites in the audit); not shown in production |
| First comment, alt text, collaborators, location, Reel settings | Already built | **Tested**: `test_post_options` (25); not shown in production |
| Other networks | **Not closing** | User decision |
| Social listening / hashtag monitoring | **Not closing** | Needs a different login and another review |
| Paid ads management | **Not closing** | Outside Content Studio. Click-to-WhatsApp referral credit is a proposal only |

---

## Build order
**Stopgap → 0b → 0a → 0c → 1 → 2a → 2b → 3 → 4 → 5**, each with its own go-ahead.
- 0b comes before 0a because it's on the filter and small.
- 0a is exempt from the filter but bigger.

**Authorized now (2026-11-01): the stopgap plus `docs/audits/CONTENT-STUDIO-MARKET-AUDIT-2026-11.md` only.** Finishing one phase doesn't authorize the next.

Stopgap acceptance criteria:
- Facebook Story and carousel are blocked in readiness with the agreed message.
- Facebook Reel shows the regular-video warning.
- Dispatch re-runs readiness and fails already-scheduled unsupported Facebook channels as FAILED (VALIDATION).
- The Instagram channel of the same post still publishes.
- The audit reports already-scheduled affected channels.
- Other formats keep their existing behaviour.
- Tests cover both relevant flag states and dispatch behaviour.

**First go-ahead: the stopgap plus the audit document.**
- Both are small, driven by correctness, and need nothing from Meta.
- The only code changes are readiness, the dispatch check and the `publishing_audit` section.
- Then 0b, with the four Instagram credit cases.
- Everything after needs its own go-ahead and, for live checks, the test Page and Instagram account.

**File references drift.** The line numbers in this plan (`services.py:1088/1090`, `instagram.py:98-115`, `settings.py:243-258`, `drafting.py:472`, `views.py:1215`) were read on 2026-11-01. Re-check each one at the start of the stopgap and of every phase before editing.

## Stopgap (unflagged, ships now)
- In `readiness.py`, block Facebook + `story` and Facebook + `carousel`: "Not yet supported on Facebook. Post to Instagram, or use an image post."
- Facebook + `reel` shows a warning: "publishes as a regular video on Facebook for now".
- **Posts already scheduled:**
  - Readiness is **re-run at dispatch** (`publish_due_posts` / `publish_variant_async`). An unsupported Facebook story or carousel channel becomes FAILED (VALIDATION) with the same message, instead of publishing the wrong thing. The Instagram channel publishes normally.
  - The existing `alerts.py` emails Owners and Admins.
  - A read-only check (a new section in `manage.py publishing_audit`) lists the scheduled Facebook story and carousel channels that exist now, so they can be told before their time comes.
- This is **removed only when `cs_fb_formats` is released**: readiness checks `is_released("cs_fb_formats")`.
- **Tests:**
  - both flag states, for readiness and for the dispatch path;
  - an already-scheduled Facebook story is failed at dispatch, while its Instagram channel still publishes;
  - the audit section lists it;
  - **with the flag off, dispatch keeps today's behaviour** for every other format.
- Added to the audit's release blockers.

## Phase 0a: Facebook format parity (`cs_fb_formats`)
1. `providers/facebook.py`: `create_reel`, `create_photo_story`, `create_video_story`, `create_multi_photo_post`. Each video call polls the status object (`uploading_phase`, `processing_phase`, `publishing_phase`). Errors go through `providers/errors.py`, with 1363128 and 1363129 mapped to VALIDATION.
2. `services.py`: send every `MediaType` to its own call, behind the flag, and **remove the `else → create_photo_post` fallback** on the flagged path. Facebook Reels and video Stories go to PROCESSING through the existing poller. Update `resolve_post_id` and the `metrics.py` REGISTRY.
3. **Reel rate limit:**
   - Our own rolling 24-hour count per Page only gives an early warning in readiness and the queue, because it can't see Reels posted outside Akilent.
   - **Meta's rate-limit error is the authority.** Map it to a RATE_LIMIT **deferral**: the channel is rescheduled after the window, using the existing retry scheduling (`test_retry_scheduling`), not a hard fail.
4. **Story reuse rule:**
   - A **content hash (SHA-256)** is stored with the media metadata. Readiness blocks a Facebook Story whose hash this Page has already published.
   - Meta's reuse rejection is still mapped to a clear VALIDATION message as a backstop.
5. **Server-side checks, on their own queue:**
   - A new `media` queue served by a **separate `celery_media` worker** (concurrency 1, same image), added to `docker-compose.yml`. A batch of large uploads can never delay publishing on `celery_worker`.
   - **Probe:** `ffprobe` against the public CDN URL. MP4s whose metadata is at the end of the file make range reads slow, so the timeout is generous (about 120 s) and a timeout counts as a failed probe.
   - **Hash:** small files uploaded through the existing Django upload get their hash in the view, where the bytes are already in memory. Large files are **stream-hashed** in the `celery_media` worker (a full streamed read, chunked, no temp file). Phase 0c may replace this with S3's own SHA-256 checksum at upload.
   - It stores duration, width, height, fps and the hash.
   - While it runs, the composer shows the media as "Checking…", and readiness **blocks** scheduling or publishing until it finishes. A failed check blocks, with a reason.
   - The composer's `<video>` metadata is only an instant hint.
   - **Backlog handling:**
     - After 3 minutes of "Checking…", the composer says "Media checks are running slowly. You can save as a draft now and schedule once the check finishes." No endless spinner.
     - A beat task alerts Owners and the operator console through `alerts.py` and `api.py` when the oldest pending job in the `media` queue is older than 10 minutes.
     - Tests cover the message and the alert.
6. **Story metrics:** confirm the Facebook story insights window with the MCP. Stories keep the 1h/12h/23h cadence. Error code 10 ("not enough viewers") becomes the `below_threshold` state.
7. **Tests** (`test_facebook_formats.py`, plus extensions to `test_facebook_channel.py` and `test_ig_video_regression.py`):
   - each format goes to the right endpoint, in both flag states;
   - a processing timeout becomes RECONCILING, not a duplicate;
   - a `finish` call that returns an error becomes FAILED with its error type;
   - **no duplicate publish after an ambiguous timeout** (same video id);
   - Meta's rate-limit error becomes a deferral;
   - a reused Story is blocked by hash, and Meta's reuse error is mapped;
   - the probe blocks too-long, low-res and pending media.

## Phase 0b: Minimal link-in-bio (`cs_bio_page`), first after the stopgap
- `BioPage` (account, slug, title, active, **`activated_at`**, set the first time the page is published) and `BioTile` (FK `TrackedLink`, position, active, pinned, archived_at).
- Public `/b/<slug>`. Each tile goes through the existing `/go/<code>` (`public_views.py`), so the credit goes to **that tile's post**.
- A WhatsApp-CTA Instagram post offers "Add to bio page".
- **Tile lifecycle:**
  - At most 12 active tiles.
  - Tiles older than 30 days are archived automatically by a daily beat task. Both numbers are per-account settings. Pinned tiles are exempt.
  - Adding a 13th tile archives the oldest unpinned one.
  - An archived tile's `TrackedLink` stays active, so links already shared, such as story stickers, still credit their post.
- **Instagram credit labels (one rule, four cases).** They appear on the Results card, the sales report and `journey_evidence`. Each is a read-time label: no records are rewritten, which keeps them unchanged as the attribution rules require. Where a click came from (bio tile or not) is recorded on `LinkClick`, so the label follows the click that started the chat.

  | Case | Label |
  |---|---|
  | No `BioPage.activated_at` for the account | "via bio link, post not confirmed" |
  | Chat started before `activated_at` | "via bio link, post not confirmed" |
  | After activation, through a bio-page tile | Confirmed for that post |
  | After activation, through a **direct `/go/` link, not a bio tile** (e.g. pasted into the bio, or a story sticker) | "direct link: confirmed for the link, not proven to be from this post" |

  Each case has its own test.
- **Bio link setup:**
  - An onboarding step, "Set this as your Instagram bio link", with a copy button.
  - On each Instagram sync, read the profile `website` field (IG User `website`, needs only `instagram_business_basic`, which is already submitted; confirmed in the MCP docs).
  - If it isn't the `/b/<slug>` URL, show a non-blocking **"Bio link not set"** warning on the bio page and the Results card. It's never required.
- Help article `post-results` updated.
- Until 0b ships, the audit states: **pilot data can't support conclusions about individual Instagram posts.**

## Phase 0c: Direct-to-S3 uploads (`cs_direct_upload`, separate go-ahead, security-sensitive)
- Presigned **POST** with:
  - the key built on the server, `publishing/<account_pk>/<uuid>/<safe-name>`;
  - a type allow-list;
  - a `content-length-range` condition;
  - 10-minute expiry.
- **Bucket:** CORS for the app origin (POST only), plus a lifecycle rule that deletes unconfirmed objects under `pending/` after 24 hours. Confirmed objects are moved or copied out of `pending/`.
- **"Confirm upload":** HEAD the object, check that the key belongs to this account and that the size and type match, then queue the probe (Phase 0a, "Server-side checks, on their own queue").
- **Delivery to Meta:** `file_url` from the public CDN by default. Check whether Meta's resumable byte upload (`offset`) is better for files over about 100 MB, and use it as the fallback when a `file_url` fetch fails.
- **Hash at upload:** require S3's SHA-256 checksum on the presigned upload, if presigned POST supports it (verify). S3 checks the bytes against it, so the confirm step can reuse it, and `celery_media` stream-hashes only when it's missing.
- **Tests:**
  - security: another account's key, a wrong type, too large, an expired policy, a missing confirm;
  - **resumable fallback:** a failed `file_url` fetch (mocked Meta error) retries through the byte upload with `offset` and publishes once, with no duplicate.

## Phase 1: Outcomes (`cs_sales_report`)
1. **"Which posts sold" report:**
   - Tracked chats, leads, orders, and revenue **per currency, never summed**, via `results.py` (`results_for`, `post_credit_q`, `_per_currency`).
   - Tracked and guessed credits are shown separately, and so are "bio link, not confirmed" credits.
   - **Minimum sample:** fewer than N tracked chats (default 5) shows "too early to judge", not a rank.
2. Bio page polish: avatar, colors, reordering, tile click stats.

## Phase 2a: Scheduling (`cs_scheduling`)
1. Month view plus drag to reschedule through `reschedule_variant`. Locked states can't be dragged, and there's a keyboard alternative.
2. Posting-slot queue, `PostingSlot` in `workspace_timezone()`.
3. Best time to post from `VariantMetricSnapshot`, with a minimum sample. Below it, "Not enough posts yet".

## Phase 2b: Analytics and bulk work (`cs_analytics`, `cs_recurring`, `cs_csv_import`)
1. **Account analytics:** daily `AccountMetricSnapshot`, with the same registry and states. Metric names verified with the MCP.
2. **Reports:** CSV and PDF export, plus an optional weekly email to Owners and Admins.
3. **Benchmarking (conditional):** check `business_discovery` with Instagram Login tokens through the MCP first. If it's unavailable **or needs any scope not in the current submission**, record it as not closing (scope freeze).
4. **Recurring / evergreen:**
   - New drafts or scheduled posts only, through `composer.save_new_or_existing` plus approval.
   - At least 7 days between repeats.
   - An Instagram repeat needs a changed caption or media, or it's held as a draft.
   - Reel rate limits apply.
   - A run that fails readiness is skipped and reported.
5. **CSV import:** preview with `readiness.check` per row, then **drafts only**. URL media goes through the SSRF-safe check. Row limit. Errors are shown per row.

## Phase 3: Media library and editor (`cs_media_library`)
1. `MediaAsset` (account, key, kind, mime, size, dimensions, duration, fps, hash, parent for derived copies, tags, uploaded_by) plus tracking of where it's used.
   - **Delete rule:** blocked while a draft, scheduled, publishing or reconciling post uses the asset. Assets used only by published posts are archived (hidden, file kept). Only unused assets are deleted for good.
2. Library page plus a composer picker, with multi-file upload through 0c.
3. Editor: Cropper.js crop and rotate per channel ratio, keeping the original; one text layer; a Reel cover-frame grab with ffmpeg. **No full design tool.**
4. Caption templates and hashtag sets.
5. **Stock images (conditional):**
   - Unsplash's API guidelines appear to require hotlinking and download tracking, which conflicts with copying into `MediaAsset`. That rule comes from secondary sources and must be confirmed against the official text.
   - Check Pexels' terms on storing files. Build only with a provider whose terms allow the image to be stored and published to Meta, with the credit kept. Otherwise ask the provider (e.g. api@unsplash.com) or record it as not closing.
6. **Canva (conditional)** on Canva Connect access.

## Phase 4: AI (`cs_ai_plus`, AI optional)
1. Brand voice in `AISettings`, passed into `content_caption_prompt` (`apps/ai/drafting.py:472`).
2. Rewrite in place: `AIDraft` kind `content_rewrite`, using the same async path as `generate_caption`.
3. Hashtag suggestions, within Instagram's 30-tag limit.
4. **Conditional:** ideas from results, once Phase 1 data is above the minimum sample.
5. **Image generation:** decision on provider and cost first. Output goes into `MediaAsset` marked "AI-generated", passes through the editor and approval, and is metered under entitlements.

## Phase 5: Collaboration and mobile (`cs_collab`, `cs_pwa`)
1. **External approval links:**
   - Signed, expiring, **single-post** links. Editing the post invalidates the link, and Owners and Admins can revoke it.
   - The endpoint is **rate-limited**.
   - Decisions are logged in `ApprovalLog` as **"approved by link holder (identity unverified)"** and tied to the content fingerprint. The UI shows the same label.
2. Inline threaded comments per post or channel version, with @mentions.
3. **PWA (partial mobile):**
   - Manifest, shell service worker, and web push for approval, failure and "needs a check" (reusing `alerts.py` triggers).
   - Mobile approve and reschedule screens.
   - Help text explains that iOS push needs the app installed to the home screen.

---

## Rollout and rollback
- **Flags:** one key per phase in `UNRELEASED_FEATURES` (`apps/core/releases.py`).
  - Pages are gated by URL prefix through `ReleaseGateMiddleware`.
  - Behaviour that isn't a page checks `is_released()`. Every flagged code path is tested in both states.
  - Releasing means `RELEASED_FEATURES` plus §4 of `messaging.md` in the same change.
  - The stopgap is unflagged and is lifted by releasing `cs_fb_formats`.
- **Migrations:** schema-only additions (`BioPage`, `BioTile`, `AccountMetricSnapshot`, `PostingSlot`, `MediaAsset`, `RecurrenceRule`, approval links, comments) are reversible by default. Any data migration has `reverse_code`.
- **Rollback order:** flag off first, then migrations. Migrate and roll back once on Postgres 16 before each release.

## The audit document (runs in parallel)
`docs/audits/CONTENT-STUDIO-MARKET-AUDIT-2026-11.md`:
- **Capabilities**, each with an evidence level (Tested / Code-only / Staging / Prod), citing a `pytest` run.
- **Release blockers:**
  - App Review in progress: 14 first-time Facebook/Instagram scopes, Meta says up to about 20 days;
  - the Facebook format bugs, until 0a ships;
  - Instagram bio misattribution, plus the old credits, until 0b ships;
  - the staging checklist;
  - `publishing_audit` on prod;
  - Facebook Page reconnect;
  - the Postgres run.
- **Competitor table** (ManyChat, Later, Hootsuite, Metricool, respond.io, Sprout, Meta Click-to-WhatsApp and the Conversions API) with the vendor URL and date checked.
- **Working positioning:** *organic Facebook and Instagram publishing connected to WhatsApp conversations, recorded leads and paid orders recorded by the business, with transparent attribution.* No "better than competitors" reliability claim without evidence from production. Public wording only through the claim register (`messaging.md` §10).

## Cross-cutting rules
- Tests for each phase, plus Postgres for anything that locks rows.
- Rebuild Tailwind.
- `{% comment %}` for multi-line template comments.
- Media always rendered as `<img>`/`<video>`.
- `hx-boost="false"` on POST forms with their own scripts.
- Help articles for new features.
- No git commits.
- Memory after approval: correct `akilent-meta-apps.md` (all 14 Facebook and Instagram scopes are first-time requests, in review since the end of October 2026, WhatsApp up for renewal, MCP "REJECTED" means not granted yet), plus the roadmap, its gate rules and the test Page.

## Verification
- `poetry run pytest apps/publishing apps/conversations apps/ai -q` passes, including both flag states. PG suites pass on Postgres 16. `makemigrations --check` is clean, and backwards migrate works.
- **Stopgap:** the composer blocks a Facebook story or carousel with the message, and an Instagram-only version still schedules.
- **0a, on the test Page and Instagram account:**
  - a Facebook Reel, Facebook photo and video Stories, a Facebook multi-photo post and an Instagram Reel each appear as the right format, and `publication_url` opens them;
  - a 2-minute clip and a reused Story are blocked;
  - metrics read, or show `below_threshold`.
- **0b:** a bio-tile click → WhatsApp chat → lead → paid order is credited to the tile's post (`journey_evidence`), and old Instagram credits show the "not confirmed" label.
- **0c:** the security tests pass, and a 300 MB Reel uploads, is probed, and publishes from a schedule set more than one day ahead.
- **Later phases:** browser check with Claude in Chrome, with no console errors.
