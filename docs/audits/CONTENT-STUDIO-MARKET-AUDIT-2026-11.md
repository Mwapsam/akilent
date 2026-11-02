# Content Studio market audit: 2026-11-01

How Content Studio compares with Buffer, Later, Hootsuite, Sprout Social, Metricool, Planable and Meta Business Suite, checked against the code. The roadmap that closes the gaps is the approved plan of 2026-11-01. Each phase needs its own go-ahead, and finishing one doesn't authorize the next.

Related: `docs/marketing/messaging.md` (§4 release status, §6 truth rule, §10 claim register), `docs/STAGING-VALIDATION-CHECKLIST.md`, `docs/ops/instagram-app-review.md`.

**Scope:** Facebook Pages and Instagram only (owner decision). Other networks are out of scope.

**Feature filter for the roadmap:** does it help a business turn social media activity into measurable commercial outcomes?

**Evidence levels used below:**
- **Tested:** automated tests pass (run cited).
- **Code only:** built, no direct test.
- **Staging:** verified on staging.
- **Prod:** shown working in production.

Nothing in this audit has reached Staging or Prod yet.

---

## 1. Release blockers

| # | Blocker | State | Closes when |
|---|---|---|---|
| B1 | **Meta App Review.** Submission `1086006971089663`, "Review in progress". All 14 Facebook and Instagram scopes are first-time requests, and the WhatsApp scopes are up for renewal in the same submission. Until approval, only accounts with a role on app `1080201821670178` can connect. Meta says most reviews take about 20 days; that's not a promise. | Waiting on Meta | Approval. The pilot on real accounts starts after approval, not on a date. Scope freeze until then: no new scopes, use cases or app settings |
| B2 | **Facebook stories and carousels published wrongly.** A story went out as a regular post (a video story to the photo endpoint), and a carousel as its first image only (`apps/publishing/services.py`, `_publish_variant_facebook`). | **Stopgap built 2026-11-01** (§4). **Phase 0a built 2026-11-01, unreleased** (§8) | Releasing `cs_fb_formats` after the live check on the test Page and the `celery_media` worker is deployed |
| B3 | **Instagram bio-link misattribution.** A bio holds one post's `/go/` link (`templates/help/articles/post-results.html`), so every chat that starts from the bio is credited to that post. **Until Phase 0b ships, pilot data can't support conclusions about individual Instagram posts.** | **Built 2026-11-01, unreleased** (§7). Read-time credit labels already show on every Instagram link credit | Releasing `cs_bio_page` after a live check on the test account |
| B4 | Staging run of `docs/STAGING-VALIDATION-CHECKLIST.md`, including the failure-recovery part | Not run | Completed checklist with `manage.py journey_evidence` output |
| B5 | `manage.py publishing_audit` on prod | Not run | Exit code 0. Also read the new section on Facebook stories and carousels (§4) |
| B6 | Every Facebook Page reconnects once (`read_insights`, `pages_manage_engagement`) | Not done | Connections hold the new scopes |
| B7 | Postgres 16 run of the scheduling, metrics and attribution suites (the row-lock tests skip on SQLite) | Not run | Green PG run |

---

## 2. What Content Studio does today (checked against code and tests)

Test runs on 2026-11-01, SQLite, with the stopgap in place:
- `poetry run pytest apps/publishing apps/automation apps/core -n 4 -m "not performance"`: **1908 passed, 4 skipped**.
  - That run used default import mode, so the legacy module `apps/publishing/tests.py` failed to collect (its name clashes with the `tests/` package).
  - Run on its own with the project's config (`--import-mode=importlib`), it gives **48 passed, 5 failed. The same 5 fail on clean `HEAD`, without the stopgap.** The module is collected only when run on its own (a directory run skips it). They are older failures in tests written for the retired post-level publishing path: `CancellationGuardTest` ×2, `PublishingIndexViewTest`, `PostNewViewTest.test_schedule_action_sets_scheduled_status`, `MonthBoundaryTest`.
- `poetry run pytest apps/instagram apps/facebook apps/conversations -n 4 -m "not performance"`: **807 passed, 10 skipped**.

| Capability | Code | Backing tests | Evidence |
|---|---|---|---|
| Instagram publishing: image, video, Reel (with cover, thumb offset, share to feed), story, carousel | `providers/instagram.py`, `services.py` | `test_ig_video_regression`, `test_ig_publish_claim` (5), `test_ig_publish_ambiguity` (4), `test_post_options` (25) | Tested |
| Facebook publishing: image, video, text | `providers/facebook.py` | `test_facebook_channel`, `test_fb_format_stopgap` (17) | Tested |
| Facebook Reels | Published as an ordinary `/videos` video. The composer warns "Publishes as a regular video on Facebook for now". | `test_fb_format_stopgap` | Tested (as a video, not a Reel) |
| Facebook stories and carousels | **Blocked** (B2) | `test_fb_format_stopgap` | Tested (blocked) |
| Per-channel versions and scheduling, workspace time zone | `services.apply_schedule`, `reschedule_variant`, `variant_is_due` | `test_per_channel_scheduling` (21), `test_variants`, `test_variant_sync` | Tested (SQLite only, B7) |
| Composer: live preview, readiness checks, autosave, crop | `composer.py`, `readiness.py`, `static/js/composer.js` | `test_composer` (29) | Tested |
| Channel options: alt text, location, collaborators, first comment | `post_options.py` | `test_post_options` (25) | Tested |
| Approval: workspace switch, Owner/Admin review, content fingerprint, log | `ContentPost.is_approval_valid`, `ApprovalLog` | `test_approval` (27), `test_phase_c_*`, `test_workspace_review` | Tested |
| Recovery when publishing fails: claim and lease, unknown outcome → reconciliation, stuck-post recovery, polling | `services._claim_publish`, `tasks.py` | `test_claim_recovery` (7), `test_unstarted_post_recovery` (6), `test_polling_reliability` (14), `test_reconciliation_operations` (17), `test_phase_b3_stale_publishing` (12), `test_remaining_hardening` (20), plus PG row-lock tests | Tested (PG locks not run, B7) |
| Metrics per channel version, with honest states (pending, unsupported, needs permission, below threshold) | `metrics.py`, `metrics_collector.py` | `test_variant_metrics` (26) | Tested. Instagram insights wait on B1 |
| Post → WhatsApp chat → lead → paid order (tracked `/go/` link, hard evidence only) | `links.py`, `public_views.py`, `conversations/entry_points.py`, `results.py` | `test_post_attribution` (19), `test_attribution_states` (15), `test_journey_acceptance` (9) | Tested. Instagram is weakened by B3 |
| Instagram and Facebook comment and DM inbox | `apps/instagram`, `apps/facebook`, `apps/conversations` | Inbox suites above | Tested |
| AI captions: three options from business notes, brief, goal and CTA | `apps/ai/drafting.py` `content_caption_prompt` | `test_brief_context` | Tested |

**Media URLs:** S3 behind a public CloudFront domain with no signature or expiry (`AWS_QUERYSTRING_AUTH = False`). So a post scheduled days ahead never meets an expired URL. The trade-off: unpublished and scheduled media can be fetched by anyone who has the URL. The random part of the key makes guessing impractical, but the files are not private.

---

## 3. Gaps against competitors, and where each closes

Phase order: **stopgap → 0b → 0a → 0c → 1 → 2a → 2b → 3 → 4 → 5**, one release flag per phase in `apps/core/releases.py`.

| Gap (who has it) | Closes in | Notes |
|---|---|---|
| Facebook Reels, stories, carousel (all) | Stopgap (done) → 0a | |
| Video checks before publishing (Later, Buffer) | 0a | ffprobe on the server, on its own queue |
| Large video uploads (all) | 0c | **Built 2026-11-01, unreleased** (§9): straight to S3 up to 300 MB; Facebook byte upload as the fallback |
| Link-in-bio (Later Linkin.bio, Buffer Start Page) | 0b → 1 | **Built 2026-11-01, unreleased** (§7, §10): tracked tiles, four Instagram credit labels, picture, colours, order, tile clicks and chats |
| Sales / ROI reporting (Hootsuite, Metricool) | 1 | **Built 2026-11-01, unreleased** (§10): posts ranked on confirmed credits, minimum sample, per currency |
| Month view, drag and drop, posting slots, best time (all) | 2a | **Built 2026-11-01, unreleased** (§11). Best time is per channel, from the business's own posts, with a minimum sample |
| Account analytics, export, email reports (all) | 2b | **Built 2026-11-01, unreleased** (§12). Export is CSV plus a print / "Save as PDF" page; no PDF library was added |
| Competitor benchmarking (Metricool, Sprout) | **Not closing** (§12) | `business_discovery` is Facebook Login only and needs `instagram_basic` + `instagram_manage_insights`, which aren't in the submission (scope freeze) |
| Recurring posts, CSV import (Hootsuite, Metricool) | 2b | **Built 2026-11-01, unreleased** (§12). Imports are drafts only; repeats go through readiness and approval; Instagram repeats need a change |
| Media library, simple editor, templates, hashtag sets | 3 | **Built 2026-11-01, unreleased** (§13). No full design tool |
| Stock images | 3 | **Pexels built, unreleased** (§13), off until `PEXELS_API_KEY` is set. **Unsplash not closing** until it confirms copies are allowed |
| Canva | **Not closing yet** | Needs an Akilent app in the Canva Developer Portal; whether other Canva users can connect before Canva reviews it is unconfirmed (§13) |
| AI rewrite, brand voice, hashtags, ideas from results | 4 | **Built 2026-11-02, unreleased** (§14) |
| AI image generation | **Not closing yet** | Needs a decision on provider and cost (§14) |
| Client approval links, inline comments (Planable, Sprout) | 5 | Link approvals labelled "identity unverified" |
| Mobile | 5, **partial** | PWA, not a native app. iOS push needs the app installed to the home screen |
| Other networks | **Not closing** | Owner decision |
| Social listening, hashtag monitoring | **Not closing** | Needs another login and another review |
| Paid ads management | **Not closing** | Outside Content Studio |

---

## 4. Stopgap (built 2026-11-01, uncommitted)

**Why:** Facebook would publish a story as a regular post and a carousel as its first image. This is a correctness fix, exempt from the feature filter.

**Rule:** `apps/publishing/constraints.py` `facebook_format_blocked(media_type)` is true for `story` and `carousel` until `cs_fb_formats` is released. It is used in two places:
- **Readiness** (`readiness.py`):
  - Facebook + story or carousel gives `fb_format_unsupported`: "Not yet supported on Facebook. Post to Instagram, or use an image post." It blocks submit for review, schedule and publish now. Saving a draft still works.
  - Facebook + Reel gives the warning `fb_reel_as_video`: "Publishes as a regular video on Facebook for now."
  - An Instagram-only post is not affected.
- **Dispatch** (`services._publish_variant_facebook`): before any call to Meta, a Facebook channel of a blocked format becomes FAILED (VALIDATION) with the same message.
  - This covers posts scheduled before the block existed, and every route: scheduler, publish now, retry and workflow step.
  - Automatic retry skips VALIDATION, and `alerts.py` emails Owners and Admins.
  - The Instagram channel of the same post publishes normally.
- **Audit:** `manage.py publishing_audit` now lists Facebook story and carousel channels still waiting to publish (post, account, type, time), so their owners can be told first. It isn't a deploy blocker.

**Flag states:**
- Unreleased (the default, and `settings_test`): the block applies.
- Released: today's behaviour returns (the old warnings, publishing to the photo endpoint) until Phase 0a replaces it.

Both states are tested.

**Not changed:** the old `publish_post` / `_publish_facebook` path has no production caller (variant publishing is mandatory since Step 4.6B), so it has no guard.

---

## 5. Competitors' attribution: what their documentation shows (checked 2026-11-01)

The claim "no competitor tracks a post to a sale" is **false**. Several tools measure social outcomes:

| Tool | What it does | Source | Status |
|---|---|---|---|
| Meta: Click-to-WhatsApp ads plus the Conversions API for Business Messaging | A chat from an ad carries a `referral` object (ad id, `ctwa_clid`). A Purchase event with value and currency can be sent back to Meta against that click id. | [Conversions API for Business Messaging](https://developers.facebook.com/documentation/ads-commerce/conversions-api/business-messaging); [WhatsApp messages webhook (referral)](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages/contacts/) | Verified (Meta docs) |
| Later | Link in Bio analytics: page views, post clicks, button clicks, click-through rate. UTM parameters let Google Analytics track traffic and revenue. | [help.later.com/hc/articles/360042743234](https://help.later.com/hc/articles/360042743234) | Verified (help centre) |
| Hootsuite | Advanced Analytics: ROI by network or ad account. Google Analytics and Adobe Analytics integration. Custom metrics. Business and Enterprise add-ons. | [hootsuite.com/platform/social-media-roi](https://www.hootsuite.com/platform/social-media-roi); [what's new](https://www.hootsuite.com/whats-new/prove-the-impact-of-your-social-campaigns-with-advanced-analytics) | Verified (vendor pages, marketing) |
| Metricool | Conversions, conversion value and ROAS for connected Meta Ads and Google Ads accounts. | [Conversions and Results in Meta Ads](https://help.metricool.com/conversions-and-results-in-meta-ads-antg4) | Verified (help centre). Ads only |
| Sprout Social | Premium Analytics: organic-only and paid-vs-organic reports. | [Paid ad reporting](https://support.sproutsocial.com/hc/en-us/articles/38421053686157-Sprout-Social-Reporting-Effectively-Managing-and-Analyzing-Your-Paid-Social-Ad-Performance); [organic-only report](https://support.sproutsocial.com/hc/en-us/articles/31040394769421-How-do-I-make-an-Organic-only-report) | Verified (help centre). Performance, not orders |
| respond.io | Attributes WhatsApp conversations to ads, including non-Meta ads through Google Ads and GA4. | [Attribute WhatsApp conversations to non-Meta ads](https://respond.io/help/capture-leads/how-to-attribute-whatsapp-conversations-to-non-meta-ads) | Verified for this. **Sending purchase events to Meta: reported, vendor page not found yet** |
| ManyChat | Reported: an Instagram content leaderboard linking posts to automation sends and contacts captured | Not found in ManyChat's own docs | **Unverified.** Don't cite until a vendor page is found |

**Open checks** (each could narrow the positioning further):
- Does ManyChat connect an Instagram post to a WhatsApp chat and to a sale?
- Does Later or Hootsuite record an *order* inside the product, or only through GA or a store?
- Does respond.io attribute *organic* sources, or ads only?

**Working positioning (internal, not public copy):** *organic Facebook and Instagram publishing connected to WhatsApp conversations, recorded leads and paid orders recorded by the business, with transparent attribution.*
- "Paid" means the business marks the order paid by hand. Akilent collects no payments.
- "Transparent" rests on the hard-evidence-only rule (`entry_points.py`), the tracked/guessed/none states (`test_attribution_states`) and `journey_evidence`.
- It is weakened for Instagram until B3 closes.
- Paid ads are left out: Meta and respond.io already cover them.

**Reliability:** recovery is **tested, not yet shown in production**. Don't claim it is "better than competitors" without production delivery evidence: published, failed, reconciled and time-to-resolve per channel, after the pilot.

Any public wording goes through the claim register (`docs/marketing/messaging.md` §10) and the truth rule (§6).

---

## 6. Verified Meta facts the next phases depend on (MCP docs, v25)

- **Facebook Reels:** `/{page-id}/video_reels`, start → upload → finish. Scopes `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, all already in B1, so no new scope if B1 is approved. Limits: 3–90 s, 9:16, at least 540×960, 24–60 fps. **At most 30 API-published Reels per Page in any rolling 24 hours.**
- **Facebook stories:** `photo_stories` (from an unpublished photo) and `video_stories`. Video up to 60 s. Media already used in a published post can't be reused as a story.
- **Instagram story insights** last 24 hours. Fewer than 5 viewers returns error code 10.
- **The Instagram profile `website` field** can be read with `instagram_business_basic`. Phase 0b uses it for a "bio link not set" warning.
- **Still open:**
  - Instagram Reel, story and video limits;
  - Facebook multi-photo (`attached_media`) and its limit;
  - the Facebook story insights window;
  - the error code for the Facebook Reel rate limit.

---

## 7. Phase 0b: link-in-bio page (built 2026-11-01, behind `cs_bio_page`, uncommitted)

- **Models:** `BioPage` (slug, title, `activated_at`, `max_tiles` 12, `archive_after_days` 30, last Instagram `website` reading) and `BioTile`. `LinkClick.bio_tile` and `ConversationEntryPoint.bio_tile` record that a click or chat came through a tile. Migrations: publishing `0021_bio_page`, conversations `0028_entry_point_bio_tile`. Schema-only, reversible.
- **Tile codes:** each tile has its own ref code, in the same namespace as tracked-link codes (`links.new_code`).
  - `/go/<tile code>` counts the click against the tile and opens WhatsApp with "(ref TILE)".
  - `entry_points.match_ref_code` resolves the tile, scoped to the workspace.
  - An archived tile keeps working. A disabled link stops it.
- **Lifecycle:** at most `max_tiles` active tiles, and adding one more archives the oldest unpinned tile. The daily beat `archive_stale_bio_tiles` archives unpinned tiles past `archive_after_days`. Removed posts can be put back with the same code.
- **Four Instagram credit labels** (`bio.credit_basis`, read-time, no record rewritten):

  | Case | Label |
  |---|---|
  | No bio page | "via bio link, post not confirmed" |
  | Before `activated_at` | "via bio link, post not confirmed" |
  | Through a tile | Confirmed |
  | After `activated_at`, through the post's own link | "direct link: confirmed for the link, not proven to be from this post" |

  - Facebook and comment credits stay confirmed.
  - The labels show on lead and order pages (`_source_post.html`), as a split of the Instagram chats on the Results card, and in `journey_evidence` (`credit_basis`, `bio_tile_id`).
  - **They apply whether or not the flag is released**, because they correct how existing Instagram credits are read. The sales report they also belong on is Phase 1.
- **Bio link check:** reads the Instagram profile `website` field (`instagram_business_basic`, already submitted) when the page is created, on "Check again", and daily (beat `check_bio_links`). "Bio link not set" is a warning, never a requirement. A failed read keeps the last reading.
- **Gate:** `/b/` and `/publishing/bio/` return 404 until released. While unreleased, the "Bio page" tab, the "Add to bio page" button, the onboarding step `bio_link` and the help-article section are hidden.
- **Not built in 0b:** avatar, colours, reordering and click stats per tile (Phase 1); a composer checkbox (adding is done from the post's Results card).
- **Tests:** `apps/publishing/tests/test_bio_page.py`, **31 passed**. Suites `apps/publishing apps/conversations apps/accounts apps/core apps/instagram apps/facebook apps/commerce apps/crm apps/insights` (project config): **3112 passed, 18 skipped**. `makemigrations --check`: no changes.
- **Live check still to do** (needs the test Instagram account with an app role):
  1. create the page and set the bio link;
  2. tap a tile → WhatsApp chat → lead → paid order;
  3. confirm `journey_evidence` credits the tile's post with `credit_basis: confirmed`.

---

## 8. Phase 0a: Facebook format parity and media checks (built 2026-11-01, behind `cs_fb_formats`, uncommitted)

Releasing the flag lifts the stopgap (§4), and Facebook then publishes each format on its own endpoint.

**Meta facts this phase relies on** (checked with the developer docs MCP, 2026-11-01):
- **Facebook Reels:** `/{page}/video_reels` start → `rupload.facebook.com/video-upload/{v}/{video_id}` with `Authorization: OAuth` and a `file_url` header → finish with `video_state=PUBLISHED`. `GET /{video_id}?fields=status` reads the uploading, processing and publishing phases. Limits: 3–90 s, 9:16, at least 540×960, 24–60 fps. 30 API-published Reels per Page in any rolling 24 hours.
- **Facebook stories:** `photo_stories` (from an unpublished `/photos` id) and `video_stories` (start/upload/finish), video ≤ 60 s. Media already used in a published post can't be reused.
- **Facebook multi-photo:** unpublished `/photos` (deleted by Meta after about 24 hours if unused), then `/feed` with `attached_media`.
- **Instagram:** Reel 3 s–15 min, 23–60 fps, ≤ 300 MB; story video 3–60 s, ≤ 100 MB; 400 containers and 50 published posts per 24 hours.
- **Still unverified:**
  - the error code for the Facebook Reel limit (Meta's general rate-limit codes are used);
  - the Facebook story insights window. Facebook stories show no metrics until it's confirmed.

**What changed:**

| Area | Change |
|---|---|
| Provider | `providers/facebook.py`: `start_video_upload`, `upload_hosted_video`, `finish_reel`, `finish_video_story`, `video_status`, `upload_unpublished_photo`, `create_photo_story`, `create_multi_photo_post`. Error codes 1363xxx (as code or subcode) → VALIDATION. |
| Dispatch | `services._publish_fb_format`, flagged: every format goes to its own call. Old path unchanged when unreleased. |
| Reels / video stories | Go to PROCESSING. The beat `poll_fb_video_variants` (every 60 s) confirms them through `try_finish_fb_video_variant`. A phase error → FAILED (VALIDATION). After 48 h → RECONCILING (escalation now covers Facebook videos that have a video id). |
| Duplicate safety | The video id is kept on the variant from the first step. A retry resumes the same video: it asks Meta first, skips the upload if done, and never finishes twice if publishing already started. Failures before finish are retryable. An unknown outcome at finish (or at the multi-photo `/feed` call) → RECONCILING. |
| Daily Reel limit | Meta's rate-limit error on a Reel defers the channel: SCHEDULED one hour later, up to 24 times, then FAILED. The task returns `deferred` instead of retrying in minutes. Readiness warns (doesn't block) at 30 Reels in 24 hours sent through Akilent. |
| Media checks | `MediaCheck` (migration publishing `0022_media_check`) holds SHA-256, duration, size, fps and rotation-aware dimensions. Uploads hash from memory. Videos are probed by ffprobe on the new **`media` queue / `celery_media` worker** (docker-compose; added to `WORKER_QUEUES` for the heartbeat). Only our own media host is ever probed or fetched (SSRF). |
| Readiness | A pending check blocks ("Checking your video…"; after 3 minutes "running slowly… save as a draft"). The composer re-checks every 5 s while one is pending. A failed check blocks with its reason. Videos outside a channel's limits block that channel. A Facebook story whose file hash this Page already published is blocked. |
| Backlog | The beat `alert_media_check_backlog` (5 min) emails Owners once a day per business when a check has waited 10+ minutes. Operators see it in the console (`api.attention_reasons`). |

**Unreleased behaviour is unchanged:** no media checks, the stopgap applies, and the Reel publishes as a video.

**Tests:**
- `test_facebook_formats.py`: **31 passed**. Each format, the failure paths (finish error, unknown finish, upload timeout, resume without duplicate, processing error, 48 h escalation, ambiguous multi-photo), deferral and its cap, every readiness rule, probe parsing, SSRF refusal, timeout, upload hashing, the queue, and the backlog alert.
- `test_fb_format_stopgap.py`: updated so the released state is real Facebook stories.
- Full `apps` suite (project config, `-m "not performance"`): **5476 passed, 25 skipped**.

**Before release:**
1. Deploy the `celery_media` worker.
2. Confirm `ffprobe` runs in the image.
3. On the test Page, publish a Reel, a photo story, a video story and a multi-photo post. Check that a 2-minute clip and a reused story are blocked.
4. Confirm CloudFront serves media to `facebookexternalhit` (robots.txt, WAF).

---

## 9. Phase 0c: direct-to-S3 uploads (built 2026-11-01, behind `cs_direct_upload`, uncommitted)

Files no longer pass through the web server, so the composer takes videos up to **300 MB** (Instagram's Reel limit) instead of 100 MB. It's offered only when `cs_direct_upload` and `cs_fb_formats` are both released and S3 storage is in use. Otherwise the composer uploads through the server as before.

**How an upload works:**
1. **Start.** `POST publishing/posts/media/direct/start/` returns a presigned POST. The server builds the key under `media/pending/publishing/<account>/<uuid>/<safe name>`, and the signed policy pins:
   - that exact key, the type (allow-list) and the exact size;
   - a 10-minute expiry;
   - when the browser could hash the file (≤ 100 MB, Web Crypto), its SHA-256. S3 then rejects bytes that don't match.

   It uses the bucket's regional endpoint, because the global one can redirect and a cross-origin POST can't follow a redirect.
2. **Upload.** The browser posts the file straight to S3, with progress.
3. **Confirm.** `POST …/direct/confirm/` with the signed token from step 1 (valid for an hour). The server:
   - checks the token belongs to this business and to its `pending/` prefix;
   - HEADs the object; a size, type or checksum that doesn't match deletes it;
   - copies it out of `pending/` with the usual cache headers, deletes the pending copy, and records the media check.

   The S3-verified hash is kept. Without one, the `media` worker stream-hashes the file. A second confirm of the same upload returns the same file.
4. **Never confirmed.** Readiness blocks a `pending/` URL (`media_unconfirmed`), and the media worker refuses to probe one. The bucket's lifecycle rule deletes it after a day.

**Facebook delivery fallback:**
- Meta still fetches videos from the CDN by `file_url` first.
- With the flag on, a refused fetch is retried once by streaming the bytes to `rupload` (`offset` and `file_size` headers, from Meta's Reels guide). Permission and rate-limit errors are not retried this way.
- An interrupted byte upload resumes from Meta's `uploading_phase.bytes_transfered` with an HTTP Range request.
- Only our own media host is streamed (SSRF), and the media host must honour the byte range.
- The duplicate rules from §8 still hold: the same video id is reused, and a finish that already went through isn't repeated.

**Not covered:**
- Instagram's own resumable upload (`upload_type=resumable`). Instagram still fetches by `video_url`, which handles 300 MB Reels.
- Hashing in the browser above 100 MB: those files are hashed by the `media` worker instead.
- Whether S3 accepts the checksum fields on a presigned POST is documented in community answers, not in an AWS page I could find. boto3 builds the policy with them (tested offline), but it still needs a staging upload. If S3 refuses, the browser can send no checksum and the worker hashes the file.
- The 10-minute policy expiry may be too short for a 300 MB upload on a slow connection. AWS's documentation doesn't say whether expiry is checked when the upload starts or when it finishes. This needs a staging upload on a slow connection too.

**Tests:** `test_direct_upload.py`, **36 passed**. It covers:
- **Security:** another business's upload, a tampered token, an expired token, a missing object, a wrong type, too large, and a size, type or checksum mismatch, which deletes the object.
- **Gating:** unflagged, without Phase 0a, and without S3.
- **The real boto3 policy**, built offline.
- **Confirm:** confirming twice, and the verified hash being kept.
- **Unconfirmed uploads:** readiness and the worker refuse them.
- **Facebook fallback:**
  - a refused fetch sent as bytes, published once, and a retry with no second upload or finish;
  - resuming from Meta's offset;
  - the byte upload's own error;
  - no fallback on permission errors, for other hosts, or with the flag off;
  - the provider's Range, offset and `file_size` headers.
- **The read-only bucket check command.**

**Before release (staging, then the test Page):**
1. Run `manage.py direct_upload_bucket` and add the CORS rule (POST from the app's origin) and the lifecycle rule (`media/pending/`, 1 day) it prints. It changes nothing itself, because putting a CORS configuration replaces the existing rules.
2. Check that the app's IAM user can `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on `media/*`. The presigned POST is signed with its keys, and the HEAD, copy and delete run as it.
3. Upload a 300 MB Reel on staging. Confirm it's probed, then schedule it more than a day ahead and confirm it publishes. Repeat on a slow connection to test the expiry.
4. Upload one file under 100 MB to confirm S3 accepts the checksum fields.
5. Confirm CloudFront answers Range requests with `206` for the byte-upload resume.

---

## 10. Phase 1: which posts sold, and bio page polish (built 2026-11-01, uncommitted)

### "Which posts sold" (`cs_sales_report`, page `/publishing/sales/`, "Sales" tab)

It uses the same evidence as the results card (`apps/publishing/sales.py`): only chats that named a post count, so every figure is the least a post brought in.

**One row per post:**
- Chats started from it, and leads and orders credited to it.
- Paid orders and revenue, **per currency, never added across currencies**.
- **Periods match the Insights journey.** Chats count when they were matched, leads and orders when they were created, and revenue when it was paid. The periods are 7, 30 or 90 days, with the same plan rule for 90 days.

**How sure each credit is:**
- Each chat, lead, order and payment keeps how sure its credit is (`bio.credit_basis`), read from the chat's first entry point. That's the same one `attribution.record` credits.
- **Only confirmed credits rank a post.** "Via bio link, post not confirmed" and "direct link" credits are shown under the post, labelled, and marked "Not used to rank".
- Bio-tile chats, Facebook links and comment replies are confirmed.

**Ranking:**
- Posts are ranked by paid orders, then orders, leads and chats.
- **Minimum sample:** a post with fewer than 5 confirmed chats in the period appears under "Too early to judge", with its numbers but no rank. The number is `sales.MIN_CHATS`.

**Kept apart from the posts:** revenue paid in the period that no post is credited with, both the recent-chat guess and everything else, and chats that started without naming a post. The credited share is not read as the whole.

### Bio page polish (under `cs_bio_page`; migration publishing `0023_bio_page_look`)
- **Picture:** JPEG, PNG or WebP under 2 MB, checked with Pillow. It's stored under `publishing/<account>/bio/`.
- **Colours:** your own background and button colours, behind a "Use my own colours" checkbox, because a colour picker can't send "none". They're validated as `#rrggbb` before going into the page's style. Text is black or white, whichever reads better; with pure black, one of the two is always at least 4.58:1 (WCAG AA). The test checks every grey and a grid of hues.
- **Order:** up and down buttons. Pinned tiles stay above the rest, and the public page follows the order.
- **Tile stats:** clicks from people (bots left out) and chats started, per tile, over the last 30 days.

**Tests:** `test_sales_report.py`, **23 passed**. It covers:
- **Ranking:** by paid orders, with the minimum sample at exactly 5; ties broken by orders and leads.
- **Revenue and credit:** never summed across currencies; a guess credited to no post and shown apart; only the period counted; another business never counted; no report when the business can't publish.
- **Instagram labels:** unconfirmed credits shown but never ranked; bio tiles confirmed and direct links not; comment replies confirmed.
- **The page:** hidden until released; ranked, too early and not credited all shown; a message when nothing can be tracked.
- **Bio polish:** reordering in the manager and on the public page, stats, the contrast sweep, saving the look, going back to the site's colours, and refusing bad colours and bad, unsupported or oversized pictures.

`test_bio_page.py` still passes (31).

**Before release:**
1. On staging, run the report against seeded data (`manage.py journey_evidence`) and check the ranked posts against the results cards.
2. Upload a picture with S3 storage, so its URL comes from CloudFront.
3. Check the public bio page with your own colours on a phone.

---

## 11. Phase 2a: scheduling (built 2026-11-01, behind `cs_scheduling`, uncommitted)

All times are on the business's own clock (`workspace_timezone`). Nothing here skips approval.

### Month view (Calendar → Month)
- **Layout:** whole weeks, Monday to Sunday, navigated with `?month=YYYY-MM`. Below `lg` it falls back to the month's days as an agenda.
- **Moving a post:**
  - Drag a post to another day and it keeps its time of day, even across a clock change. The test moves 09:30 London from 20 October to 3 November.
  - Dropping fills in and sends the post's own **Move** form (`<details>` with a date input). The keyboard path and drag go through the same server check, `scheduling.move_to_day`.
- **Locked posts:** a published, publishing, failed or checking channel has no Move form and isn't draggable. The server refuses it too.
  - The rule (`scheduling.can_move`) is the same one the post page's per-channel reschedule uses.
- **What the server also refuses:** a day already past, a post without a valid approval, and another workspace's post.
- **Channels:** a post with one time per channel moves only the channel dragged.
- **After a move:** you go back to the calendar page you came from, same site only.
- **Styles:** scoped to `_cal_month.html`, so `static/css/app.css` isn't rebuilt.

### Posting slots (Calendar → Posting slots, `PostingSlot`, migration publishing `0024_posting_slot`)
- **Who:** Owners and Admins add slots (several days at one time) and remove them.
- **Next free slot:** in the composer, it fills in the next slot that none of the post's chosen channels is already scheduled at, looking 8 weeks ahead and skipping anything under 5 minutes away.
  - It **only fills in the time**. Saving still runs readiness and approval as usual.
  - Two drafts filled in at the same moment can get the same slot. Nothing stops both being scheduled then, which is the same as picking the time by hand.

### When your posts do best (on the slots page)
- **Sample:**
  - posts on that channel from the last 90 days that have been out at least 3 days;
  - stories left out;
  - each post's latest numbers.
- **Measure:** interactions per person reached, so a post isn't favoured just because more people saw it.
  - Instagram: `interactions`, or else likes, comments, saves and shares.
  - Facebook: reactions, comments and shares.
- **Grouping:** by day of the week and 3-hour block, compared by median.
- **Minimum sample:**
  - below 10 measured posts on the channel it says "Not enough posts yet: N of 10";
  - a block needs 3 posts of its own.

  It's worded as a guide, not a promise.

**Tests:** `test_scheduling.py`, **23 passed**. It covers:
- **Month view:** hidden until released; whole weeks; movable and locked posts marked; navigation.
- **Moving:** the local time kept across a clock change; one channel moved alone; refusals for a published post, a past day, a missing approval and another workspace; hidden until released; the safe return to the calendar.
- **Slots:** skipping past and taken slots per channel; the clock change; none, or all taken; the composer endpoint and button; Owner add and remove; Members refused; other workspaces' slots safe; hidden until released.
- **Best times:** the minimum sample; ranking by rate, not reach; stories, unsettled posts and posts with no reach left out; Facebook's formula; the page.

**Not checked yet:** drag and drop in a real browser. The server side of the drop is tested, but not the browser's drag events.

**Before release:**
1. In a browser, drag a post on the month view in Chrome, Safari and Firefox, and use Move with the keyboard.
2. Check a business whose timezone isn't UTC.
3. Add slots and use Next free slot from the composer.

---

## 12. Phase 2b: analytics, recurring posts and CSV import (built 2026-11-01, uncommitted)

### Meta facts (developer docs MCP, 2026-11-01)

**Instagram account insights:**
- `/{ig-user-id}/insights` with `period=day`, `metric_type=total_value` and since/until.
- Metrics: `reach`, `views`, `accounts_engaged`, `total_interactions`, all with `instagram_business_manage_insights`, which is submitted.
- Instagram says the data can be up to 48 hours late.
- `follower_count` and `follows_and_unfollows` need 100+ followers. Followers therefore come from the `followers_count` field instead (`instagram_business_basic`).

**Facebook Page insights:**
- `page_total_media_view_unique` (reach) and `page_media_view` (views), both `read_insights`.
- Meta says several Page metrics were retired on 15 June 2026 and marks some rows in its table without explaining the mark. **Only unmarked metrics are used.** Followers come from the Page's `followers_count` field (`pages_read_engagement`).
- Page insights need 100+ likes.

**Benchmarking: not closing.**
- `business_discovery` is documented only for the Instagram API with **Facebook** Login.
- It needs `instagram_basic`, `instagram_manage_insights` and `pages_read_engagement`. The first two aren't in the submission, and the scope freeze rules out adding them.

### Account analytics (`cs_analytics`, Content Studio → Analytics)
- **`AccountMetricSnapshot`:** one per channel per business day, holding the registry in `metrics.ACCOUNT_REGISTRY`. It's in migration `0025_analytics_recurring`.
- **Collection:** `collect_account_metrics` reads the settled day, 2 days back, every 6 hours. A day still missing numbers is read again for a week.
  - Followers are read only on the newest day, so an older day never gets today's count.
  - The task does nothing until the flag is released, so no new calls reach Meta before then.
- **States:** a missing number keeps its state (pending, unsupported, needs permission). An invalid-metric error asks for each metric on its own. A rate limit stops the run.
- **Summaries:** reach and accounts engaged are people per day, so they're averaged, never added. Views and interactions are added. Followers show the last reading and the change. Each figure says how many days it came from.
- **Exports:** a CSV of every channel's day (a blank value with its state, never 0), and a printable page for the browser's "Save as PDF". I didn't add a PDF library; that's a dependency decision for you.
- **Weekly email:** Owners and Admins turn it on. It's sent Monday from 08:00 on each business's clock, once a week (a cache key per ISO week). It covers each channel's 7 days and what posts brought in, with the lower-bound wording.

### Recurring posts (`cs_recurring`, a post's page → Repeat this post; Calendar → Recurring)
- **Rules:** every 7 to 365 days (a database check enforces at least 7), the first repeat more than a day ahead and at least 7 days after the post went out, and up to 10 alternate captions used in turn.
- **How a copy is made:** a day ahead, through the **Schedule button's readiness check** and `composer.save_new_or_existing`, copying the channels' options and chat links.
  - **In a workspace that reviews posts, the copy is sent for review, never scheduled.**
- **Instagram:** a copy with the same caption and media as the last copy is **held as a draft**.
- **Failures:** a copy that fails readiness is **skipped** with the reasons, and a time missed by downtime is skipped rather than sent late. Owners and Admins are emailed about held and skipped copies.
- **One post per run:** each run is recorded once (`RecurrenceRun`, unique per rule and time), so a task that runs twice makes one post. Resuming a paused repeat skips the times that passed.

### CSV import (`cs_csv_import`, Library → Import CSV)
- **Reading:** up to 100 rows and 1 MB, UTF-8, with a header row.
- **Preview:** each row's errors (channel, media type, time, media), then the composer's readiness check as "to fix before scheduling". Nothing is saved.
- **Import:** **rows become drafts only**, even with a time, which is kept as the suggestion. Rows are checked again at import.
- **Media:**
  - Our own media host, or a public `https` image copied in through the knowledge importer's SSRF guard. Every resolved address must be public, the connection is pinned to the checked address, and redirects aren't followed.
  - It must be JPEG, PNG or WebP, under 8 MB, and readable by Pillow.
  - External videos are refused: upload them in the composer, where they're checked.

**Tests:** **46 passed**.
- **`test_account_analytics.py` (15):** collection with the day's bounds; missing numbers not zero; finding the invalid metric; a revoked token; a rate limit; Facebook; not connected; an older day keeping its followers; retrying only incomplete days; the gated task; averages, totals and the followers change; the page, CSV and print view; the gate; Owner-only weekly switch; the Monday-once email.
- **`test_recurring.py` (15):** refused rules; the first repeat a week after the post; a scheduled copy through the composer with options; the next time on the business clock; not due, and once only; Instagram held; captions in turn; skipped and reported; the review workspace; downtime skipped; the gated task; the post-page form, pause and resume; another workspace; the gate.
- **`test_csv_import.py` (16):** unreadable files; preview errors per row, including plain http, external video and an internal address; what to fix; drafts only; the image copied through the guard; bad content type, too large or 404; an internal address never fetched; preview then import through the page, once; the gate.

**Found while testing, not changed:** readiness doesn't block scheduling a Facebook post when the workspace has no Facebook Page connected. That's existing behaviour, so the recurring test uses a real blocker (an empty Facebook text post) instead.

**Before release:**
1. Read one day's account insights for the test Page and the test Instagram account, and compare them with Meta Business Suite.
2. Confirm Instagram returns a single day's total for `since`/`until`.
3. Repeat a test post in a workspace that reviews posts.
4. Import a CSV with a public image.

---

## 13. Phase 3: media library and editor (built 2026-11-01, behind `cs_media_library`, uncommitted)

### The library (Content Studio → Media)
- **`MediaAsset`** (migration `0026_media_library`) holds: name, kind, MIME type, size, dimensions, duration, fps, SHA-256, `parent` (for edited copies), source, tags, credit and uploader. It's unique per business and URL.
- **What gets recorded:** once released, every upload (composer, direct-to-S3, CSV import) is recorded. A failure to record never fails the upload.
- **Backfill:** `manage.py media_library_backfill` adds the media of existing posts. It covers our own media host only and is safe to run again.
- **Where a file is used** is read from posts, never stored: their `media_urls` and the channels' options, such as a Reel cover.
- **The removal rule** (`media_library.remove`):
  - **blocked** while a draft, scheduled or publishing post uses the file, or a channel is still processing or being checked;
  - **archived** (hidden, file kept) when only published, failed or cancelled posts use it;
  - **deleted**, file and all, when nothing uses it.
- **The page:** a grid with filters by type, tag and name, an archived view, multi-file upload through the same endpoints as the composer (straight to S3 when 0c is on), and tags and names.
- **In the composer:** **From library** adds a file as already uploaded, filtered to the post's format.

### Editor (no full design tool)
- **Pictures:** Cropper.js at 1:1, 4:5, 1.91:1, 9:16 or free; turn left or right; one line of text (top or bottom, white or black, with an outline). It's drawn in the browser and saved as a **new** asset with `parent` set. **The original is never changed.**
  - The image is loaded with CORS, the same as the composer's re-crop. A file from a host without CORS can't be read back, and the page says so.
- **Reel covers:** choose a time, and ffmpeg takes that frame on the `media` queue (`grab_cover_frame`), from our own media host only (SSRF), with a 60 s timeout. The frame is saved as a new picture. A failure is kept for the page to show.

### Caption templates and hashtag sets
- **Where:** managed from Media → Caption templates and hashtags.
- **Templates:** in the composer, a template goes into the caption being edited (all channels, or one channel's own version).
- **Hashtag sets:** a set adds only tags not already there. A set over 30 tags is refused (Instagram's limit).

### Stock photos
- **Pexels (built):** its API documentation says content may be used "for anything you'd like" within its guidelines, which ask for a prominent link to Pexels and credit to photographers. They say nothing about hotlinking (checked 2026-11-01).
  - A photo is looked up **by id**, so the file and the credit come from Pexels, never from the browser.
  - The file is copied only from `images.pexels.com`, and the credit ("Photo by … on Pexels" with a link) is stored on the asset.
  - It's off until `PEXELS_API_KEY` is set.
- **Unsplash (not closing):** its official API guidelines say apps "must use the hotlinked image URLs returned by the API … for all uses" and must trigger the download endpoint. A copy in our storage for Meta to fetch isn't a hotlink. Unsplash would need to confirm in writing (api@unsplash.com).
- **Canva (not closing yet; checked 2026-11-02):**
  - **Canva MCP is the wrong tool for this.** It exposes Canva to an AI assistant that picks tools itself. An "Import from Canva" button needs a fixed list-and-export, which is the Canva REST API (formerly Connect). Both use the same Developer Portal app and per-user OAuth.
  - **How it would work:** each user connects their own Canva account (OAuth with PKCE, scopes `design:meta:read` and `design:content:read`; refresh tokens are single-use and rotate). They pick a design; an export job returns files on `export-download.canva.com`, which expire after 24 hours, so the file is copied into `MediaAsset` straight away, from that host only, the same pattern as Pexels.
  - **Canva's rules that apply:** no fetching or indexing designs without a user action, no keeping user data beyond need, no Brand Kit content outside Canva output, nothing used to train AI. Exports with premium elements can fail with `license_required`. Export is limited to 20 requests a minute per user.
  - **What blocks it:** someone has to create the Akilent app in the Canva Developer Portal (client ID and secret, redirect URL, scopes). Canva's docs don't say whether other Canva users can connect to it before Canva reviews it, so that has to be confirmed in the portal or with Canva before building.

**Tests:** `test_media_library.py`, **27 passed**. It covers:
- **Recording:** recorded once released; one asset per URL; an upload never failing because of the library.
- **Removal:** blocked by draft, scheduled, publishing and still-being-checked posts; a Reel cover counts as a use; archived with the file kept; deleted with the file; another business's post not counted; remove and restore through the page.
- **The page:** listing, tag filtering, tag cleaning, the Media tab, the gate, another business's asset.
- **Editing:** a new picture with the original kept; a non-picture refused.
- **Covers:** a frame from our own host at the given time; never another host; the task reporting a failure.
- **Stock:** copied with its credit; another host refused; off without a key.
- **The composer:** the picker showing only live files of the right kind; templates and sets, including the 30-tag limit; nothing until released.
- **Backfill.**

**Before release:**
1. Run `media_library_backfill`.
2. Check the media host sends CORS headers for GET, so edits can be read back.
3. Take a Reel cover from a real video on staging (ffmpeg in the `celery_media` image).
4. Set `PEXELS_API_KEY` if you want stock photos.
5. In a browser, crop, add text and save; pick from the library in the composer.

---

## 14. Phase 4: AI tools (built 2026-11-02, behind `cs_ai_plus`, uncommitted)

### The rules it keeps
- **AI stays optional.** Every tool needs the flag released **and** AI on for the business (`apps.ai.api.is_available`: the site's AI provider, the `ai_assistant` plan feature, and the owner's opt-in). When any is missing, the composer shows no AI tools and the AI page says why.
- **AI only suggests.** A rewrite or hashtag is used only when the person picks it, and a rewrite can be undone. What's picked is saved through the composer's normal save, readiness and approval like anything typed. A review workspace still needs approval, and an edit after approval still voids it.
- **What was used is recorded.** The post is marked AI-assisted, and an accepted rewrite is kept as an accepted caption (`ContentCaption`), the same as a picked caption suggestion. Only finished drafts of the post's own business count.
- **Ideas never become posts.** An idea is added to Planning as an idea (a `ContentPlanItem`), and someone writes the post.
- **Metering:** each request is an `AIDraft` through the existing background path and daily AI limit.

### What's built
- **Brand voice** (Content Studio → AI; Owners and Admins edit it): `AISettings.brand_voice` (migration `ai 0012_content_ai_plus`). It's passed to caption suggestions, rewrites and ideas as **style only**. The prompt says it isn't a source of facts, and facts still come only from the AI notes. Before release it isn't sent at all.
- **Rewrite in place** (composer → Caption → Rewrite with AI): shorter, friendlier, clearer next step, fix spelling and grammar, or adapt for Instagram or Facebook. It works on the version being edited (shared or one channel's), and the result is shown as a word diff. The checks after (`apps.ai.content.check_rewrite`):
  - a rewrite that's empty or unchanged fails;
  - it warns if a link, phone number, @mention or hashtag was dropped;
  - it warns if it runs over the channel's caption limit;
  - it warns on any price, number or link that wasn't in the caption or the business's facts.
- **Hashtag suggestions** (composer → Suggest hashtags): tags are cleaned (no `#`, no spaces or symbols, at most 60 characters) and never repeat one already in the post. At most 10 on Instagram, and never past Instagram's 30; at most 3 on Facebook. A post already at 30 gets no request. They're added one tap at a time.
- **Ideas from results** (Content Studio → AI): built only on the posts the sales report (Phase 1) **ranks** in the last 90 days, meaning confirmed credits with at least 5 tracked chats, the top 5 by paid orders. With none, the page says there aren't enough results and no request is made. Each idea must name one of those posts; any that doesn't is dropped. It's then added to Planning on request.

### Not closing yet: image generation
This needs a decision on provider and cost first. The plan's conditions still apply: output goes into the media library marked "AI-generated", through the editor and approval, and metered under entitlements.

### Found, not changed
The existing **Start with AI** caption suggestions (`views.generate_caption`) don't check `is_available`. They run whenever the site has an AI provider, even if the business's plan doesn't include AI or the owner hasn't turned it on. The new tools do check. Fixing the old path changes released behaviour, so it's left for the owner's decision.

**Tests:** `test_ai_plus.py`, **24 passed**. It covers:
- **The gate:** off until released; off with AI off for the business.
- **Brand voice:** Owner/Admin only and capped; it reaches caption suggestions only once released, marked style only.
- **Rewrites:** the diff and the voice; dropped link and mention plus a new price flagged; empty or unchanged failing; unknown instruction refused; another business can't read a draft.
- **Hashtags:** cleaning and no repeats; room per channel; a full post refused; cut to the room left.
- **What the post used:** recorded, with someone else's or unfinished drafts not counting.
- **Approval:** AI text in a review workspace isn't published or scheduled.
- **Ideas:** only ranked posts as sources; nothing without results; ideas tied to a given post; saved as a plan item with no post created.

**Before release:**
1. Try each rewrite and hashtag suggestion with the production AI provider, and check the warnings read well.
2. Decide on the **Start with AI** gap above.
3. Ideas need real ranked posts, so check them once the pilot has some.

