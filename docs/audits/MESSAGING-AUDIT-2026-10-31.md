# Messaging guide audit: 2026-10-31

Read-only audit of `docs/marketing/messaging.md` against the repository. **Nothing was changed.** Each fix waits for owner approval.

Related documents: `docs/marketing/messaging.md` (present-day claims), `docs/strategy/global-positioning.md` (market and roadmap), `docs/strategy/meta-growth-optimisation.md` (long-term direction).

**Existing executable check:** `poetry run pytest apps/accounts/tests/test_landing_claims.py -n 0`: **18 passed**. (Under xdist the run hit an execnet bootstrap error on this Windows machine; that is an environment issue, not a test failure.) A pass shows that the four checked pages are tagged and that every evidence file contains its text. It does **not** show the gaps in §C.

**Severity key:**
- **Blocker:** a public or in-app surface contradicts a release status or owner rule.
- **Should-fix:** a claim or check is weaker than the guide requires.
- **Nit:** wording.

---

## Summary

| Severity | Count | Headline |
|---|---|---|
| Blocker | 5 | Unreleased features are reachable (Support, chatbot, HubSpot); payment-link remnants; "payments" in the landing footer; demo script says "send a link to pay"; §4 leaves out the Facebook Page inbox |
| Should-fix | 12 | Channel gating gaps; weak evidence anchors; outcome claims unchecked; test coverage limited to 4 URLs; "Never miss a customer" in the product |
| Nit | 6 | Facebook headings and labels, "every channel", a stale test docstring |

---

## A. Release table (§4) against the code

| # | §4 row | Verdict | Evidence and gap |
|---|---|---|---|
| A1 | WhatsApp inbox, automations, campaigns | **Pass** | Channel registry `apps/core/channels.py:66-73`; URLs return 404 when the channel is off through `ChannelGateMiddleware` (`apps/core/middleware.py:333`) |
| A2 | Email sending, templates, campaigns, API/SMTP | **Partial** | No "email conversations" copy found. But the Campaigns nav shows with only WhatsApp on (`apps/core/context_processors.py:28`: `CAMPAIGNS_ENABLED = email or whatsapp`; `templates/components/_nav.html:20,34`). This is correct behaviour, since WhatsApp campaigns are Live, but §4 describes it as Email-gated. Fix the wording in §4, not the code. |
| A3 | Instagram DMs, comment rules, publishing | **Pass** | `channels.py:74-84`; the Meta-review restriction lives in `apps/core/models.py:32-36`; "App Review" appears only in the operator console (`templates/manage/settings.html:84`) |
| A4 | Facebook Page publishing | **Partial (blocker for §4)** | The **Facebook Page inbox (Messenger and Page comments) has no row in §4**, yet it is Live in code (`apps/conversations/services.py:550`, `apps/facebook/services/triggers.py`, `channels.py:85-97`) and claimed in §2, §8 and §10 |
| A5 | Content Studio (needs IG or a FB Page) | **Partial** | Public listing is gated on a channel (`channels.py:126-128`, `feature_visible` at `:403-408`). The in-app nav is gated on the plan only (`_nav.html:26`), and `entitled()` ignores channel state (`apps/billing/api.py:144-165`). So the nav shows with no publishing channel on. |
| A6 | Leads, orders, Business Health, AI | **Partial** | Leads and orders are catalog features (`apps/billing/features.py:160-175`); AI is gated by `usable("ai_assistant")` (`apps/ai/api.py:34`). Business Health (`/insights/`) has no plan gate, only the 90-day period is gated (`apps/conversations/insights_views.py:36-40`). "Some features depend on your plan" is **missing from `templates/billing/plans.html`**. |
| A7 | Website chatbot, Support tickets, HubSpot, custom fields: "only Coming soon" | **Fail (blocker)** | `ComingSoonFeature` is used on the pricing pages, **but the features are reachable**: the Support nav item has no gate (`templates/components/_nav.html:25`); the dashboard chatbot panel has a "Create chatbot" CTA (`templates/accounts/_dashboard_panels.html:93-131`); `/chatbot/`, `/support/` and `/hubspot/` are mounted (`automator/urls.py:124-130`); the "Connect HubSpot" card is ungated (`templates/accounts/settings_integrations.html:97-135`); custom-field creation is linked from `apps/whatsapp/views.py:210` |

**Plan catalog as seeded** (`apps/billing/management/commands/seed_plans.py:7-86`, run by hand; **live database values not verified**):

| Plan | Price | Trial | Conversations/mo | WA numbers | History | Emails/mo | Automations |
|---|---|---|---|---|---|---|---|
| Trial | $0 | 14 days | 100 | 1 | 3 days | 500 | 2 |
| Starter | $19 | — | 1,000 | 1 | 14 days | 10k | 10 |
| Professional | $49 | — | 5,000 | 1 | 30 days | 50k | 50 |
| Business | $99 | — | unlimited | 1 | 90 days | unlimited | unlimited |

Migration `0025` caps WhatsApp numbers at 1 on every plan; `0033` gives every plan all four channels. Period discounts are 0.95 / 0.85 / 0.75 (`apps/billing/pricing.py:11-13`). Note: the owner's SWOT lists no **Starter ($19)** plan. Confirm whether Starter is active on prod.

## B. Owner rules: payments, Facebook, promise, attribution, layers

| # | Rule | Finding | Severity |
|---|---|---|---|
| B1 | No business payments | `templates/accounts/landing/_footer.html:6` says "Customer conversations, follow-ups, posts **and payments** for small businesses." | Blocker |
| B2 | No business payments | WhatsApp starter templates still offer a "Payments" pack with the blurb "Reminders, payment links" and a `payment_link` template and variable (`apps/whatsapp/starter_templates.py:151-225, 636`). The template variable picker has a "Payment link" field (`apps/whatsapp/views.py:196-208`). This contradicts the slice 2-0 done criteria in `docs/design/onboarding-spec.md:480`. | Blocker |
| B3 | No business payments | `docs/sales/demo.md:57`: "You record the order and **send a link to pay**". Line 58 correctly says orders are marked paid by hand. | Blocker |
| B4 | No business payments | `templates/legal/terms.html:16` says "keep track of leads, orders and payments". Acceptable ("keep track"), but worth tightening to "orders you mark paid". | Nit |
| B5 | Facebook means a Page | `templates/accounts/landing/_faq.html:16` heading "Do Facebook messages come into the inbox?" uses the banned phrase. The answer on line 17 is correct. | Nit |
| B6 | Facebook means a Page | Bare "Facebook": `_features.html:43`, `public/for/content-studio.html:24,29`, `accounts/channels.html:106` ("Manage Facebook"), `instagram/comment_rules.html:23`, `apps/contacts/erasure.py:56` ("Facebook profiles") | Nit |
| B7 | No absolute guarantees | "Never miss a customer" as an automation category label (`apps/automation/engagement_starters.py:202`), and "never missing a customer" in `templates/help/articles/automations.html:5`. Both break §6/§7, and the second phrase is on the test's banned list (for "/" only). | Should-fix |
| B8 | Channel-independent promise | "across every channel": `templates/inbox.html:20`, `templates/scheduler/index.html:8` | Nit |
| B9 | Promise read as "helps" | The promise "…into sales, from one place" appears at `_final.html:8` and `components/welcome_tour.html:81`. The explanation (manage enquiries, follow up, record paid orders, measure where evidence exists) is in `_journey.html:17-83` and the tour steps, not next to the headline. Proposal: add one sentence under each. | Should-fix |
| B10 | Attribution is a lower bound | Pass overall. `_results_card.html:53` ("the real total can be higher"), `_source_post.html:4-8` (three states), `insights/_journey.html:66,84` ("Not credited to a post", "the least your posts brought in"), `help/articles/post-results.html:31-34`, landing `_journey.html:82-83`. Gaps: the post Results card shows no untracked count (Insights does), and the FAQ heading `_faq.html:22` "How do you know which post brought a sale?" echoes the absolute style. | Nit |
| B11 | Long-term layers not shown as current | Pass. No public hits for "growth intelligence", "optimisation", "experiments", "autonomous", "AI marketing" or "omnichannel". In-app "recommendations" (`insights/_intelligence.html`, `crm/lead_detail.html:30-35` and others) describe existing features, not the growth engine. | Pass |

## C. Claim register (§10) and its test

**C1. Weak evidence anchors** (should-fix). Each passes the substring check without proving its claim. Proposed anchors, all checked to exist in the named file:

| id | Current | Proposed |
|---|---|---|
| keywords | `apps/automation/keywords.py::def` | `::def matches` (:30) |
| business-hours | `apps/accounts/business_hours.py::def` | `::def is_open` (:236) |
| campaigns | `apps/whatsapp/campaigns.py::def` | `::def send_campaign` (:692) |
| own-number | `apps/whatsapp/embedded.py::def` | `::def discover_waba_and_phone` (:82) |
| verification-codes | `apps/whatsapp/verification_codes.py::def` | `::def send_code` (:148) |
| instagram-publishing | `apps/publishing/providers/instagram.py::class` (matches `InstagramPublishError`) | `::class InstagramPublishProvider` (:62) |

**C2. Anchors that prove only part of a claim** (should-fix):

| id | Gap | Proposed fix |
|---|---|---|
| campaign-scheduling | The anchor proves scheduling only; pause, cancel and resend are not evidenced | Add `pause_campaign` (:405), `cancel_campaign` (:449), `retry_failed_campaign` (:545) |
| auto-replies | Proves "reply when closed" only | Cover greeting and keywords too |
| ai | Proves "off by default" only | Add `reply_mode` (`apps/ai/models.py:36`) |
| roles | Proves Member only | Add OWNER and ADMIN (`apps/accounts/models.py:133-134`) |
| webhooks | Proves signing only | Use `def enqueue_event` (`apps/email/webhooks.py:60`) |
| trial | Proves trial length, not "no card required" | Add evidence for "no card required" |
| data-deletion | Proves the route name only | Anchor to the deletion code |
| workflow-testing, attribution | Anchors are docstrings | Use `def simulate` (`simulate.py:37`) and `def match_ref_code` (`entry_points.py:31`) |
| content-studio, ai-captions | Anchors prove labels and a prompt builder, not variant creation or caption generation | Find the variant-creation and caption-generation functions |

No claim was found where the code contradicts the claim.

**C3. Outcome claims have no validation rule** (should-fix). `test_register_evidence_still_exists` skips every Outcome row (`test_landing_claims.py:128-129`). Proposed rule: each Outcome row lists its supporting capability ids, and the test asserts that whenever a page shows the outcome, at least one supporting id is also on the page and every supporting id is Live. Proposed mapping:

| Outcome | Supporting capability ids |
|---|---|
| outcome | inbox, missed, followup-due, interested |
| promise | inbox, instagram-dms, facebook-inbox, content-studio, orders, post-results, attribution |
| lost-enquiries | inbox, missed, follow-ups, followup-due, auto-replies |
| social-results | post-results, attribution, post-metrics, post-chat-link, insights |

**C4. No per-claim channel gating check** (should-fix). The test parametrizes only `WHATSAPP_ENABLED` and `restricted_channels`, and it never asserts that a claim for an off channel is absent. Two real leaks exist today:
- `templates/accounts/landing/_features.html:36`: "Reach and engagement from Instagram and Facebook" sits under `{% if ch.content %}`. It shows the Facebook claim when only Instagram is live, and the reverse.
- `templates/public/for/content-studio.html:6,19,24`: hard-coded "Instagram and your Facebook Page", with no `ch.facebook` gate. The page opens when either channel is live.

Proposed test: a channel → claim-id map (for example `instagram` → instagram-dms, instagram-comments, instagram-publishing). For each channel switched off, assert that its ids are absent from the rendered HTML.

**C5. Coverage** (should-fix):
- Only `/` and the three `/for/` pages are checked.
- Unchecked: `/signup/`, `/help/` articles, `/docs/`, legal pages, the billing plans page, onboarding and the welcome tour, `docs/sales/demo.md` and email copy.
- Within `/`, `CHECKED_SECTIONS` leaves out the hero, the final CTA and `#pricing`.

**C6. Stale docstring** (nit). `test_landing_claims.py:181` says "Only WhatsApp messages reach the shared inbox today". Instagram and Facebook Page messages also do.

## D. Demo journey (§3)

**Pass.** Every cited module exists: `composer.py`, `links.py`, `go_link` (`automator/urls.py:34`), `entry_points.py` (called from `conversations/services.py:145/242/587`), `crm/services.py`, `commerce/services.py`, `attribution.py`, `results.py::results_for` (:28), `/insights/`. `seed_demo_workspace.py` `_journey` (lines 268-346) seeds the full chain: Instagram post link → WhatsApp chat → lead → order marked paid. The only failure is B3 (the demo script wording).

## E. Meta billing (who pays Meta)

**Policy, not code-verified.** No code in `apps/whatsapp/embedded.py` calls a credit-line or billing API; it does token exchange, WABA discovery and phone registration only. The "Meta bills each business directly" model is stated in:
- `apps/billing/costs.py:4,8`
- `apps/billing/models.py:805`
- `docs/billing/pricing-review.md:41,106`
- `templates/manage/plan_costs.html:11`

Absence of a credit-line call is consistent with direct billing. Final confirmation is Meta Business Manager: each customer's WABA should show its own payment method.

## F. Global readiness (input to the positioning doc)

| Item | Finding |
|---|---|
| Time zones | `USE_TZ = True`, `TIME_ZONE = "UTC"` (`automator/settings.py:202-204`); workspace `timezone` field (`apps/accounts/models.py:266`) |
| i18n | `USE_I18N = True`, but no templates or Python use translation tags and there is no `locale/` directory. **English only.** |
| Country | `country` field exists (`apps/accounts/models.py:70`). Its use in signup and reporting is unverified. |
| Consent | `ConsentStatus` with opted in, opted out and unknown (`apps/contacts/models.py:39-51`), plus email consent fields (:80-83) |
| Legal | `templates/legal/`: privacy, terms, cookies, data deletion. **No DPA or data-processing terms**, and no hosting-region statement found. |

## G. Data availability re-check (`meta-growth-optimisation.md` §C)

Still accurate on 2026-10-31:
- `apps/publishing/metrics.py` collects the IG and FB metrics listed there.
- `profile_visits` is not collected.
- No ads integration exists.

**New since 2026-10-25**, verified in Meta's docs:
- Per-message charges for service messages from 2026-10-01.
- Utility templates inside an open window are charged from 2026-10-01.
- Per-token charges for Meta Business Agent messages from 2026-08-01.
- The 72-hour free entry window is unchanged.

---

## Fix log

**2026-10-31: all 5 blockers fixed (owner approved batch 1 plus A7):**
- **A7:**
  - New `apps/core/releases.py` and `ReleaseGateMiddleware`. Support, chatbot (including `/api/chat/`) and HubSpot pages return 404, and their entry points are hidden (nav, dashboard chat-widget panel, HubSpot card, "Create support ticket", and the `create_ticket_from_conversation` view). This holds until the key is in `RELEASED_FEATURES` (env, comma-separated).
  - Contact custom fields are Live in-app; lead and deal fields have no UI. §4 now says both.
  - Tests are in `apps/core/tests/test_release_gate.py`.
- **B1:** the footer now reads "…and orders for small businesses".
- **B2:**
  - The payments starter pack is now "Reminders and receipts". The `payment_link` template, the "Pay now" button and every link variable are gone.
  - The picker's "Payment link" field is removed.
  - Guarded in `apps/commerce/tests/test_no_payment_links.py`.
- **B3:** `docs/sales/demo.md:57` now says "mark it paid when they pay you".
- **A4:** §4 gains a Facebook Page inbox row. A2's Campaigns wording is corrected in §4 as well.

**Results:**
- New and related tests: 45 passed.
- `apps/core` run serially: 847 passed.
- A parallel run of the touched apps showed 4 failures and 1 setup error that all pass serially (parallel-run interference), plus `test_phase4_load`, which is a known failure on clean HEAD.

**Deploy note:** prod needs no change to keep these features hidden. To release one, set e.g. `RELEASED_FEATURES=support` and update §4 in the same change.

## Proposed fix batches (batch 1 and A7 done; the rest awaiting approval)

1. **Owner rules (blockers):**
   - B1 footer wording.
   - B2: remove the payment-link starter pack, template and variable, or reword them to "payment reminder" with no link.
   - B3 demo script.
   - A4: add a Facebook Page inbox row to §4.
2. **Release gating (blocker A7):**
   - Gate Support, chatbot, HubSpot and custom fields behind a release flag or `ComingSoonFeature` in the nav, dashboard panel, settings card and URLs.
   - Or, if any of them should now be released, change §4 by decision.
3. **Channel gating:**
   - Gate the A5 Content Studio nav on a publishing channel being on.
   - Gate the C4 template claims on the right channel.
   - A2: correct the §4 wording.
   - A6: add the plan note on the plans page.
4. **Executable register (C1–C6):**
   - Stronger anchors.
   - The outcome rule.
   - The per-channel absence test.
   - More URLs covered.
   - The docstring fix.
5. **Wording nits:** B4–B9.
