# Outreach plan: QR codes, post audiences, comment DMs, referrals, ad capture, content help

## Context

Content Studio already proves which posts sell. A post's tracked WhatsApp link (`/go/<code>`) puts "(ref CODE)" in the customer's first message. `entry_points.match_ref_code` turns that code into a `ConversationEntryPoint`, and `ConversationAttribution` copies the post onto the leads and orders that follow. But it mostly reaches people who already follow the business or have already chatted. The owner asked for a plan for six outreach ideas (TikTok excluded):

1. QR codes for posts and offline offers
2. Click-to-WhatsApp ad tracking (store-only, decided)
3. Comment keyword → DM with the post's link
4. Campaign lists built from a post's chats
5. Referral links (credit + manual reward, decided)
6. Ready-made post ideas and a holidays/paydays calendar

Constraints that apply to all of them:
- **Pilot feature freeze:** this is a plan. Each phase needs the owner's go-ahead before it's built.
- **Each phase ships behind its own release flag** (`apps.core.releases.is_released`).
- **Only explicit evidence earns credit.** Never infer from timing, and never pool different sources together.
- **Messaging truth rule:** nothing goes into marketing copy or the claim register (`docs/marketing/messaging.md`) until it's released. Each phase updates its help guide.
- **Akilent never handles money for businesses.** Rewards are recorded by hand.

Suggested build order: **0 → A → B → C → D → E → F**. A and B are the cheapest and both strengthen "which posts sell". E only stores data.

## Phase 0: Links that aren't a post's (needed by A, D and E)

Today `TrackedLink.post` is required, and `ConversationEntryPoint` has a check (`entry_point_names_a_post_or_campaign`) that every entry point names a post or a WhatsApp campaign.

- `apps/publishing/models.py` `TrackedLink`:
  - Make `post` nullable.
  - Add `kind` (`post` | `offline` | `referral`, default `post`), `label` (e.g. "Counter poster") and `referrer` (FK `contacts.Contact`, nullable).
  - Add checks: `kind=post` needs a post, `kind=referral` needs a referrer, `kind=offline` needs a label.
- `apps/conversations/models.py` `ConversationEntryPoint`:
  - Widen the check to "a post, a WhatsApp campaign, or a link".
  - Add a uniqueness rule on `(conversation, link)` for non-post links.
- `apps/conversations/entry_points.match_ref_code`: for a link with no post, look it up by link instead of post. Keep the existing account check, so another business's code never matches.
- `apps/conversations/attribution` (where `source_post=entry.post`) needs no change: `entry_point` is already stored, so reports read `attribution__entry_point__link__kind`. `post_credit_q` keeps excluding these, so offline and referral credit never ranks a post.
- **`apps/publishing/sales.py`:** its "what no post is credited with" figure must count only chats with **no** entry point. Offline and referral chats get their own rows (A, D) instead of being counted as unattributed.
- One migration each in `publishing` and `conversations`.

## Phase A: QR codes (idea 1). Flag `cs_qr_links`

- **QR for a post:** a "QR code" button next to each channel's tracked link on the post page (`templates/publishing/post_detail…`). It shows an SVG and has download links for SVG and PNG.
- **Offline links:** a new **Content Studio › Links** page (`apps/publishing/link_views.py`, tab added in `templates/publishing/_subnav.html`).
  - Owners create a link with a label and an optional prefilled message, using `links.new_code()` and `whatsapp_number_for()`.
  - The page lists clicks (`LinkClick`), chats, leads, orders and paid revenue for each link, and has "Print / download QR".
- **QR rendering:** reuse `segno`, already a dependency and used in `apps/accounts/views.py` (`segno.make(uri, error="M")`). The QR encodes `links.short_url(link)`, so every scan goes through `go_link` (`apps/publishing/public_views.py`) and counts as a click.
- **Reporting:** an "Offline and other links" table in Which posts sold (`sales.report`), from `attribution__entry_point__link__kind="offline"`, with the same per-currency revenue rules.
- **Help:** a section in the Content Studio help article on printing QR codes.

## Phase B: Campaign lists from a post's chats (idea 4). Flag `cs_post_audiences`

- **`apps/contacts/segments.py`:** new field `came_from`, with values `post:<id>`, `link:<id>` and later `referrer:<contact id>` and `ad:<source_id>`.
  - It compiles to `Q(conversations__entry_points__post_id=…)` (or `…__link_id`).
  - Operators: `eq` and `in`.
  - Add it to `builder_fields()` with the business's recent posts and links as choices.
- **Existing pieces it combines with:** `has_order`, `whatsapp_reachable` and `last_engaged_days`.
- **Post page and sales row:** a "Follow up with these customers" action creates a segment "Chatted from ‹post› · no order", i.e. `came_from=post:X AND has_order=false AND whatsapp_reachable=true`. It then opens the WhatsApp campaign composer with that segment. `apps/whatsapp/campaigns.audience_contacts(segment=…)` already sends to a segment.
- **Results:** campaign replies to these customers are credited by the existing `match_campaign_reply`. No new attribution is needed.

## Phase C: Comment keyword → DM with the post's link (idea 3). Flag `cs_comment_dm_link`

Comment-to-DM already works on both channels: `apps/instagram/models/trigger.CommentTrigger` (`reply_type=dm`, `channels` covers Facebook too), fired by `apps/facebook/services/triggers.py` and Instagram's equivalent. These DMs are already credited to the post through `record_comment_entry`. What's missing:

- **`CommentTrigger.post`:** optional FK to `ContentPost`. When set, the rule only fires on comments on that post's variants (the thread's `content_variant`).
- **Placeholders in `reply_template`:** `{whatsapp_link}`, the post's tracked link for that channel (`links.active_link(variant)` → `short_url`), and `{bio_link}`. Render them where `{username}` is replaced today, in both channels' `_fire_trigger`. If no link exists, the reply goes out without it and the run is noted, never failing.
- **Composer:** an "Auto-reply to comments" box on the post ("When someone comments *price*, send them a private message …"). Saving it creates a trigger with the post set. The rule builder (from the earlier inbox-pages plan) shows these rules with the post's name.
- **Help:** update the comment rules help guide.

## Phase D: Referral links (idea 5). Flag `cs_referrals`

- **Creating links:** "Referral link" on the customer page (`templates/contacts/detail…`) creates or reuses a `TrackedLink(kind="referral", referrer=contact)`. The customer's personal link and QR can be copied, or sent through a WhatsApp template.
- **Prefill:** "Hi! ‹Name› sent me (ref CODE)". The ref code already does the matching.
- **Credit:** a chat from the link gets an entry point with that link. Its leads and orders are then credited to the referrer through `attribution.entry_point.link.referrer`.
- **Customer page:** shows "Referred by ‹X›" on the referred customer, and "Referred N customers · N orders · revenue" on the referrer.
- **Manual reward:** a `ReferralReward(link, referred_contact, note, given_at, given_by)` record. The owner clicks "Mark reward given" with a free-text note, e.g. "10% off next order". No money moves through Akilent.
- **Reporting:**
  - A "Referrals" row and a top-referrers list in Which posts sold.
  - The segment field `came_from=referrer:<id>` from Phase B.
  - A segment "customers who referred someone" for thank-you campaigns.
- **Help:** a new "Referral links" guide.

## Phase E: Click-to-WhatsApp ad referral capture, store-only (idea 2). Flag `ads_referral_capture`

This is the "store-only referral capture" option named in `docs/strategy/akilent-ads.md`, approved here on its own. It doesn't approve Gates 0–4, and it adds no ad reporting.

- **New model `AdReferral`** (in `apps/conversations`): account, conversation, channel, `source_type`, `source_id` (ad id), `source_url`, `headline`, `ctwa_clid`, `raw` (JSON), `received_at`. One record per conversation and ad.
- **WhatsApp:** in the inbound handler (`apps/whatsapp/tasks.py` `_handle_inbound_message`), read `messages[].referral` from the event's raw payload, which `WebhookEventLog` already keeps.
- **Instagram:** in `apps/instagram/services/inbound.py`, stop dropping `referral` entries silently and store them. Today they're skipped at "Only 'message' entries are messages".
- **Paid takes precedence**, as the strategy requires: when a conversation has an `AdReferral`, `match_ref_code` and `record_comment_entry` still record the entry point, but flag it `via_ad=True` (new boolean). Which posts sold leaves `via_ad` entry points out of organic credit. Nothing else displays them yet.
- **Backfill command:** `ad_referrals_backfill` replays stored webhook events, so Gate 0's data check has history to look at.
- **Inbox:** a small "From an ad" tag in the conversation details panel, with the ad headline. This doesn't count as a report.
- **Docs:** update the strategy doc's Gate 1 note ("store-only capture approved and built on …").

## Phase F: Ready-made post ideas and a holidays/paydays calendar (idea 6). Flag `cs_content_help`

- **Post idea library:** `apps/publishing/idea_library.py`, about 40 starter ideas tagged by industry (the welcome card's `INDUSTRY_CHOICES` in `apps/accounts/welcome.py`) and by goal (sell, trust, engage). Each has a topic, an objective and a caption outline. They're shown in Planning as "Ideas for ‹industry›". "Use this idea" creates a `ContentPlanItem` (the existing planning model) and can pre-fill a `CaptionTemplate`.
- **Holidays:** add the `holidays` package with `poetry add holidays`. Map `Account.country` to an ISO code with the existing `apps/accounts/forms.country_name/country_choices` helpers. The Planning calendar and Calendar show the next 60 days' public holidays as markers, each with a "Plan a post" button that pre-fills the date and topic.
- **Paydays:** a `PublishingSettings.paydays` field (days of the month, plus a "last working day" option), set in Content Studio settings and shown as markers in the same way. Off until set.
- **Help:** a section in the Content Studio help article.

## Files touched (representative)

- **Models and migrations:** `apps/publishing/models.py`, `apps/conversations/models.py`, `apps/instagram/models/trigger.py`
- **Logic:** `apps/conversations/entry_points.py`, `apps/publishing/links.py`, `apps/publishing/sales.py`, `apps/contacts/segments.py`, `apps/facebook/services/triggers.py` and the Instagram trigger service, `apps/whatsapp/tasks.py`, `apps/instagram/services/inbound.py`
- **New:** `apps/publishing/link_views.py`, `apps/publishing/idea_library.py`, the `AdReferral` model, the `ReferralReward` model
- **Templates:** the post page, `publishing/_subnav.html`, Which posts sold, the Planning calendar, the customer page, the composer
- **Help:** `templates/help/articles/content-studio.html` and the comment rules and referrals guides

## Verification

For each phase, before asking for the next go-ahead:

- **Unit tests:**
  - A ref code from an offline or referral link creates an entry point and credits the order to the link or referrer, never to a post.
  - Another business's code is ignored.
  - `post_credit_q` rankings are unchanged.
  - A segment `came_from=post:X AND has_order=false` returns the right customers.
  - A trigger scoped to a post fires only on that post's comments, and `{whatsapp_link}` renders the right channel's link.
  - The `AdReferral` parser handles WhatsApp and Instagram payloads, and `via_ad` entry points are left out of organic credit.
  - Holiday markers appear for the account's country, and none appear for an unknown country.
- **Database:** run the migrations on Postgres (the check constraints change), not just SQLite.
- **Regression:** run the publishing, conversations, contacts, whatsapp, instagram and facebook suites (`poetry run pytest`).
- **End to end, on a local server:**
  - **Phase A:** create an offline link, scan its QR with a phone (or open `/go/<code>`), send the prefilled WhatsApp message to the test number, and see the chat, then a lead and an order, on the Links page and in Which posts sold.
  - **Phase B:** from a post with chats, "Follow up" opens the campaign composer with the right audience count.
  - **Phase C:** comment the keyword on a test post, and the DM arrives with the link.
  - **Phase D:** a referral link chat shows "Referred by", and marking the reward given is recorded.
  - **Phase E:** replay a stored CTWA webhook fixture.
- **CSS:** rebuild with the standalone Tailwind 4.3.3 binary when new utility classes are added.
