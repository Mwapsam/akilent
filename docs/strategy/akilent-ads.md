# Akilent Ads Strategy

**Status:** Strategy for validation  
**Last updated:** 12 November 2026  
**Scope:** Instagram, WhatsApp and Messenger advertising  
**Messenger:** Included since 14 November 2026 (owner decision, see section 10)

> **This strategy authorises no implementation. The pilot feature freeze applies.** Each gate below needs its own owner approval before any work starts. The text is the owner's, written 2026-11-12, with a correction to the App Review status in Gate 4 and the review fixes made the same day.

## 1. Strategic position

Akilent should help businesses make better advertising decisions with evidence they already have. It should not start by teaching them how to operate Meta Ads Manager, and it should not start by building another campaign-creation tool.

The central differentiator is:

> **Akilent knows which of your posts already sold before you spend anything, and whether your inbox wasted the ad after you did.**

Internal positioning headline:

> Put money behind what already sells, and stop losing the chats it brings.

This is an internal strategic statement, not approved public marketing copy.

The product has two connected jobs:

1. **Before spending:** identify existing organic posts with evidence of customer outcomes. This gives the business a better-informed starting point when choosing what to promote.
2. **After spending:** show what happened to the conversations the advertising generated, including response times, follow-up, leads and paid orders.

The value is not in showing advertising metrics, which Meta already provides. Akilent's opportunity is connecting content, conversations and business outcomes.

The strategy must stay evidence-led. A post that succeeded in the past is not automatically a good paid advertisement. An advertisement that generates conversations is not necessarily generating customers.

### Strategic distinction

**Historically successful** means a post has accumulated evidence of attributed customer outcomes.

**Currently promotable** means the post is also suitable to promote now. Where the information can be verified, that includes product availability.

These are different claims. Never treat one as the other.

## 2. Before spending: identify posts with evidence

The first product experience should help a business find which of its existing organic posts have shown customer interest or sales.

Recommendations must use explicit attribution evidence, not inferred relationships or AI-generated assumptions.

Rankings and verdicts are rule-based. AI is optional, and is used only to help write copy. No ranking, verdict or recommendation depends on AI being switched on (the AI-optional rule).

### Evidence hierarchy

Rank posts using the strongest available evidence:

1. **Already sold:** explicitly attributed paid orders.
2. **Most leads:** explicitly attributed leads, where the paid-order threshold is not met.
3. **Most chats:** explicitly attributed conversations, where neither stronger threshold is met.

Never describe a post as having sold when the evidence only shows chats or leads.

If no post clears the applicable evidence floors, say there is not enough evidence to recommend a post with confidence.

### Proposed evidence floors

The initial proposal for the 90-day reporting period is:

| Label | Proposed minimum |
|---|---:|
| Already sold | 3 paid orders |
| Most leads | 3 leads |
| Most chats | 3 chats |

The final thresholds are decided at Gate 0, after examining actual tenant data.

A post receives the strongest label whose threshold it clears. The thresholds are qualification rules. They are not statistical proof that the post will perform well when promoted.

### Evidence window and post age

Raw totals over a 90-day period can favour older posts, because older posts have had more time to accumulate outcomes.

The reporting design must therefore:

- Show the post's age alongside its evidence.
- Prefer a consistent, fixed period after publication when comparing outcomes, if the data supports a reliable calculation.
- Avoid presenting posts of different ages as if their periods were equivalent.
- Make the reporting period and attribution basis clear.

The exact comparison window after publication must be settled from the available data. Do not invent a period that the existing records cannot reliably support.

### Product availability

Past sales do not establish that a product is available now.

Before presenting a post as a promotion candidate:

- Check whether the associated product is still available, where reliable product data exists.
- If availability cannot be verified, say so explicitly.
- Do not imply that a product is available just because the post generated orders in the past.

The recommendation should keep historical performance separate from current suitability for promotion.

### Evidence limitations

Organic performance is useful evidence. It is not a guarantee of paid performance.

A post that did well organically may not do equally well when promoted. Audience, placement, budget, competition and other factors can change the result.

The interface must make clear what the evidence establishes and what it does not.

## 3. After spending: explain what happened to the chats

The second half of the differentiator is connecting advertising-generated conversations to what happened inside the business.

An example of the intended experience:

> Your ad generated 40 chats.  
> 22 waited more than an hour for a reply.  
> 3 became paid orders.

This is an illustration, not a performance claim.

The purpose is to help the business see two things: whether the advertising generated useful conversations, and whether those conversations were handled well.

### Inbox verdict definitions

The manual demand test and the eventual product both need these definitions. The test must not use an informal definition that changes later, when the feature is built.

**Clock start**

The response clock starts when Akilent receives the **first inbound customer message of the ad conversation**. The verdict measures the time to the first qualifying reply. Later customer messages in the same conversation do not restart this clock. How the conversation continues afterwards appears separately, under follow-up activity.

**Business-hours elapsed time**

Measure elapsed time within the business's configured operating hours. Record the business schedule used for the calculation.

**Total elapsed time**

Also report total elapsed time, including nights, weekends and other time outside business hours.

Both measures matter. A customer who messages at night still waits, even if the business is closed.

**What counts as a reply**

Any reply counts unless it is a generic greeting or an away message.

A reply may be:

- Written by a person.
- Automated, including an automation that answers the customer's question or takes an order.

The verdict must show whether the qualifying reply was human or automated.

A generic acknowledgement, such as a greeting or an away message, does not stop the response clock. An automated response that actually handles the customer's request does stop it.

This prevents working automation from being counted as an inbox failure.

**Unanswered conversations**

A conversation counts as unanswered within a threshold only if no qualifying reply arrived within that threshold.

The threshold value is an open decision (§10f). The proposal is **1 hour of business-hours elapsed time**, with total elapsed time reported next to it. It must be fixed before Gate 1 outreach. Apply the same threshold and reporting convention in the manual test and in the product.

### What the verdict should explain

For each advertising source, the analysis should cover:

- Number of attributed chats.
- Response-time performance, using both business-hours and total elapsed time.
- Human versus automated replies.
- Conversations without a qualifying reply.
- Follow-up activity.
- Leads identified.
- Paid orders recorded.

The verdict should describe observable outcomes. When the evidence may point to an inbox or sales-process problem, it must not automatically blame the quality of the advertising.

## 4. Attribution model

Akilent must keep three distinct, mutually exclusive conversation categories:

1. **Organic post**
2. **Paid ad**
3. **Unattributed**

Attribution must follow the actual evidence of where a conversation came from.

### Paid advertising takes precedence over post matching

An advertising referral stays classified as paid even if the underlying creative matches a `ContentPost`.

Matching an ad conversation to a Content Studio post does not turn it into an organic conversation.

This rule is essential. Without it, a boosted post could wrongly receive organic credit for conversations that paid distribution generated.

### Unmatched ad referrals

An explicit advertising referral stays paid even when Akilent cannot match it to a known Content Studio post.

A missing post match must not erase the paid origin.

### Explicit evidence only

Never give post, campaign or advertising credit based on guesses.

Existing attribution rules stay intact, including the rule that uncertain attribution receives no credit.

### Reporting integrity

Keep these categories separate in reporting. Do not combine organic and paid results in ways that hide where conversations came from.

## 5. Reporting principles

Every metric must make its source and meaning clear.

Use three reporting categories:

| Category | Meaning |
|---|---|
| Meta-reported | Metrics supplied by Meta's advertising systems |
| Akilent-attributed | Outcomes explicitly connected to conversations, leads or orders in Akilent |
| Derived | Calculations that combine available source metrics |

Examples of derived metrics:

- Cost per chat.
- Cost per lead.
- Cost per customer.
- Revenue divided by advertising spend.

Revenue divided by spend is not profit. Akilent must not present it as profit, or imply that it accounts for product costs, operating expenses or other costs, unless those costs are actually available and included.

Revenue stays grouped by currency. Never combine currencies into one total without an explicitly defined, valid conversion basis.

The reporting design must keep Meta's advertising measurements separate from Akilent's attribution of business outcomes.

## 6. Product placement within Content Studio

Content Studio should remain the central place where businesses see how their content connects to customer outcomes.

### Post page

The post page is the natural place to show:

- Organic performance evidence.
- The post's evidence label.
- Post age and reporting period.
- Product availability, where verifiable.
- Whether the post is a promotion candidate.
- Advertising status and results, once available.

Keep the organic and paid dimensions distinct.

### Insights

Insights should connect:

- Organic post performance.
- Paid advertising performance.
- Conversations.
- Leads.
- Paid orders.
- Derived efficiency metrics.

The interface should never make a metric look more certain than its evidence.

### Settings → Channels

Connecting an advertising account belongs in the channel and account-connection experience.

### Ad policy

Akilent does not claim that an ad complies with Meta's advertising policies, and does not pre-validate compliance. Where ad data is available, it shows the ad review status that Meta reports.

Access should respect plan entitlements and the existing channel-permission model. Treat the proposed Ads capability as a separately controlled plan feature.

## 7. Competitive position

The competitive question is not whether other tools can create advertisements, connect to Meta or send conversion events. They can.

The opportunity is to connect a business's existing organic content evidence to the conversations and customer outcomes that follow paid promotion.

The competitor review on 12 November 2026 found the capabilities below. The review was not exhaustive. Re-verify these findings before making any public competitive claim.

| Competitor | Observed capability |
|---|---|
| respond.io | Conversions API workflow support, click-ID handling and campaign-level lifecycle reporting; positioned toward larger or more operationally mature teams |
| Wati | Sends lead and purchase events to Meta using `ctwa_clid` |
| AiSensy | AI advertising agent that can generate creative from a brief |
| Interakt | Advertising launcher that reduces the need to work directly in Ads Manager |

The review did not find these products combining organic post ranking by outcome with reply-quality diagnosis of paid chats. This is a research observation, not proof that no competitor offers it.

### Capabilities that are not the differentiator

These may be useful supporting capabilities, but they should not lead the strategy:

- Guided advertising setup.
- AI-generated advertising copy.
- Conversions API buyer signals.
- Campaign conversion comparisons.

They are increasingly available elsewhere. They may matter later, but they don't establish Akilent's distinctive value.

### Meta platform changes

Meta's advertising automation and conversion-event capabilities keep evolving. In particular, automatic reporting of purchase and lead events, including `ctwa_clid`, could make a basic conversion-event integration less distinctive.

Akilent should not base its differentiation on a capability that Meta or competitors can easily commoditise.

## 8. Risks and limitations

### Insufficient organic evidence

Some businesses may not have enough attributed organic outcomes to qualify for a meaningful recommendation.

When that happens, the product must say the evidence is insufficient rather than manufacture a winner.

The Gate 0 data check decides whether this problem is common enough to challenge the strategy.

### Organic winners may not win when promoted

Organic performance is a useful starting signal. It does not predict or guarantee paid performance.

The interface must communicate this limitation.

### Paid-order reliability

Paid orders are the strongest outcome signal, but only when the order is reliably recorded and explicitly attributed.

Prefer paid orders recorded in Akilent or through a verified store integration over informal chat labels.

Where order evidence is incomplete, show its source and limitation.

### Product availability

A post that succeeded before may advertise a product that is no longer available.

Where current availability cannot be verified, say so. Do not imply the post is ready to promote.

### Inbox diagnosis

A low lead or order count does not automatically mean poor targeting or creative.

Consider the business's response and follow-up behaviour before suggesting a problem with advertising quality.

### Demand risk

The strategy stays unvalidated until businesses show they are willing to pay for the proposed outcome.

Positive feedback, interest or free participation is not enough evidence of willingness to pay.

## 9. Validation gates

Each gate is a separate decision point. Passing one gate does not authorise building the next.

### Gate 0: Data availability

Before building recommendations, run a read-only `manage.py journey_evidence` analysis across active tenants.

The report must establish:

- The number and share of active tenants with at least one post above the proposed Already sold floor.
- The number and share above the Most leads floor.
- The number and share above the Most chats floor.
- The distribution of attributed orders, leads and chats.
- Whether the evidence is reliable enough to support the proposed ranking.
- Whether a consistent comparison window after publication can be calculated.
- Whether product availability can be verified from available data.

Do not pick five high-evidence businesses and use them to imply that the wider customer base has enough evidence. Report the share of all active tenants.

**Proposed stop condition:** if fewer than 20% of active tenants clear the Most chats floor, stop and revisit the differentiator before demand validation.

Gate 0 also settles the final evidence floors and the main ranking label, based on the observed data.

Do not describe any recommendation as based on paid orders unless the data shows that enough businesses actually meet the paid-order threshold.

### Gate 1: Demand test

Run a manual test before authorising work on the recommendation and inbox-verdict features.

The proposed sample is five businesses that:

- Have organic post history above at least one evidence floor.
- Already run, or are willing to run, an Instagram or WhatsApp boost during the test.
- Are genuine potential customers, not friends or people doing a favour.
- Pay the real, preselected list price, or make a genuine paid commitment.

Fix the sample and the pass criteria before outreach.

#### Prefilled-message verification

Before selecting businesses, check each channel separately:

- Whether a unique prefilled message can be configured in Meta's simple boost flow.
- Whether one can be configured for Instagram direct-message advertising.
- Whether the actual customer-facing experience keeps the message intact reliably enough for manual identification.

A customer can edit a prefilled message, so it is a strong matching signal, not guaranteed attribution.

If a channel does not support the required flow, the test must either use the store-only referral capture option (if separately approved) or exclude businesses that neither identification method can cover.

#### Manual identification before referral capture

Until automated referral capture exists, identify advertising chats by hand using:

1. A unique prefilled message configured for the relevant advertisement.
2. A stored referral, if that option is separately authorised.

Comparing timings against Ads Manager is a fallback only. Any attribution found that way must be labelled estimated, not explicit.

#### Test deliverable

For each participating business:

- Identify an organic post using the agreed evidence label.
- Explain the evidence behind the recommendation.
- Have the business promote the selected post through the supported Meta flow.
- Identify the resulting advertising conversations by hand.
- Deliver the inbox verdict using the agreed clock and reply definitions.
- Report the resulting leads and paid orders where evidence exists.

Ask each business which part they would pay for on its own:

- The post recommendation before spending.
- The inbox verdict after spending.
- Both together.

The answers show whether demand is for the full proposition or only one part.

#### Proposed pass condition

At least two of the five businesses must pay the preselected monthly list price, or make a genuine paid commitment, within 30 days of receiving the verdict.

Two out of five is a go/no-go threshold for further investigation, not proof of broad market demand.

If fewer than five businesses qualify, decide and record the fallback before reviewing the Gate 0 results. Do not quietly change the sample or the pass threshold after seeing the results.

A failed demand test stops all later gates.

### Gate 2: "What already sold" promotion candidates

Only after the demand test passes and this gate is separately approved:

- Build the promotion candidates described in §2: evidence labels, floors, post age, reporting period and availability status. They appear on the post page and in Insights (§6).
- Use existing attribution data only. No new Meta permissions are needed.
- Hand off to Meta's boost flow for the chosen post. Akilent publishes nothing, creates no campaign and spends nothing.
- Below every floor, say there is not enough evidence. Never manufacture a candidate.

### Gate 3: Advertising-chat attribution and inbox verdict

Only after the demand test passes and this gate is separately approved:

- Capture WhatsApp advertising referral information.
- Capture Instagram advertising referral information.
- Keep the referral information that attribution needs.
- Deduplicate the relevant events and conversations.
- Maintain the three separate origin categories.
- Produce inbox verdicts for each ad.
- Connect attributed conversations to leads and paid orders.

Use webhook evidence where available. Never infer explicit attribution from timing or post matching.

### Gate 4: Advertising spend visibility

Only after demand validation and separate approval:

- Prepare the rationale for the required Meta permissions.
- Verify current Meta permission requirements.
- Prepare the `ads_read` submission.
- Join read-only Meta spend information to Akilent-attributed outcomes.
- Calculate clearly labelled derived efficiency metrics.

The previous Meta App Review (submission 1086006971089663) was approved on 12 November 2026, and no review is pending. `ads_read` would need a new submission. Preparing the rationale is not authorisation to submit it.

Initial spend visibility is read-only. It does not authorise changes to campaigns, budgets or targeting.

### Gate 5: Campaign execution

Creating and publishing campaigns is a separate product decision, not an automatic continuation of the earlier gates.

Any proposal needs its own approved specification covering:

- Meta permissions and account access.
- Financial safeguards and customer approval.
- Campaign lifecycle and status handling.
- Idempotency.
- Error handling and reconciliation.
- Publishing and change confirmation.
- Acceptance tests and staging verification.

Under this strategy, Akilent must not change advertising budgets or campaigns on its own.

## 10. Owner decisions

### Decided on 12 November 2026

These are settled. Do not reopen them without a new owner decision.

- **Differentiator:** as stated in §1. Lead with the evidence before spending and the inbox verdict after it.
- **Gate order:** as in §9. Data check, then demand test, then promotion candidates, then ad-chat attribution, then spend visibility, then execution. Each gate is approved separately.
- **Demoted:** guided setup, AI copy and Conversions API buyer signals are supporting features, because competitors already offer them (§7).
- **Buyer:** businesses that already have organic post history in Akilent, and that run or are willing to run boosts.
- **Geography: resolved by Instagram parity.** Click-to-WhatsApp ads are strongest in India, Latin America, Southeast Asia and Africa. Small businesses in the USA and UK lean more on Instagram posts and DMs. The differentiator works for Instagram posts and DM ads as well as WhatsApp, so it fits the adopted USA/UK/EU-first positioning without depending on Click-to-WhatsApp adoption. Instagram gets equal weight to WhatsApp. Messenger was excluded here and was added on 14 November 2026 (below).
- **Commercial model:** a flat add-on price. No percentage of ad spend, and customers pay Meta directly (Appendix C).
- **AI:** optional, and used only for copy (§2).

### Decided on 13 November 2026: build what needs no new Meta permissions

The owner authorised building, ahead of the Gate 1 demand test, everything that works with the permissions Akilent already holds:

- **§10b store-only referral capture:** built and always on. `apps/conversations/ad_referrals.py` stores the WhatsApp and Instagram `referral` as an `AdReferral` and an `AD_REFERRAL` entry point. Backfill with `manage.py ad_referrals_backfill`. WhatsApp history comes from stored webhook payloads; Instagram's standalone "reopened from an ad" events before this date are lost.
- **Paid takes precedence (§4):** built. An ad entry point never names a post, wins over any other entry point, and is excluded from every post's organic counts. Which posts sold shows a "From Meta ads" revenue line.
- **Gate 0:** `manage.py ads_evidence_report` (read-only).
- **Gate 2:** `apps/publishing/promote.py`, flag `cs_promote_candidates`, using the proposed floors (§10e).
- **Gate 3:** `apps/conversations/ad_verdict.py`, flag `ads_inbox_verdict`, using the proposed 1-business-hour threshold (§10f) and the §3 clock.

Both flags stay off until released, and releasing them is a separate owner decision. Gates 4 and 5 remain unauthorised. The demand test (Gate 1) still decides whether the Ads add-on is sold.

### Decided on 14 November 2026: ad chats that run smoothly

The owner approved a phased plan for what a business running ads still needs before Gate 4, with no new Meta permissions. Phase 1 and Phase 3 are built. The owner then lifted the Messenger exclusion the same day, and Phase 2 is built.

- **Phase 1, ad-aware automations (flag `ads_automation`):**
  - A trigger filter `trigger.from_ad` (any ad, or listed ad ids) on "a customer messages you". It answers only the first message of a chat that came from an ad.
  - A one-click starter, "Answer ad chats straight away", that outranks the other message automations.
  - `{ad.headline}` in reply text.
  - An "ad chats waiting" alert (`apps/conversations/ad_alerts.py`, every 5 minutes): one email per workspace per sweep to owners and admins, once per chat. It uses the §3 clock and the §10f threshold, and it also shows in What needs you.
- **Phase 2, Messenger ad referrals:**
  - A Click-to-Messenger chat is stored, counted as paid and answered by the ad automations, exactly like WhatsApp and Instagram. `AdReferral.Channel.FACEBOOK`, migration conversations 0033.
  - A standalone `messaging_referrals` event (an existing thread reopened from an ad) is stored against the customer's existing chat, and never creates one.
  - The Page webhook now subscribes `messaging_referrals`. Pages connected earlier need `manage.py facebook_resubscribe` once.
  - History backfills from stored Messenger messages (`ad_referrals_backfill`).
  - No new permission: `pages_messaging` is already approved.
- **Phase 3, ad-burst load test:** `apps/core/tests/test_ad_burst_load.py` (performance mark). 500 WhatsApp and 300 Instagram ad chats go through the real signed webhooks. The test checks they are stored once, answered once and safe to replay. The verdict and the sweep are checked on 2,000 ad chats.

### Open decisions

These decisions stay visible and open until the owner explicitly settles them.

### 10a. Monthly Ads add-on price

The owner chooses the actual monthly list price before demand-test outreach.

The test must use this real price. Discounts, favours or free participation do not count as evidence of willingness to pay.

**Status:** Open. Blocks Gate 1.

### 10b. Store-only referral capture

WhatsApp and Instagram inbound processing currently throws away the referral information.

A narrow freeze exception could store the raw referral data on the inbound message or conversation, with no reporting, attribution logic or UI changes.

Potential benefits:

- Keeps referral history that would otherwise be lost for good.
- Adds a second way to identify advertising chats in the manual demand test.
- Avoids relying entirely on prefilled messages, which customers can edit.

Trade-off:

- Needs a limited schema and inbound-processing change during the current freeze.

This is a separate owner decision. This strategy does not authorise it.

**Status:** Decided and built on 13 November 2026 (see above).

### 10c. Fewer than five qualifying businesses

The demand-test sample and pass criteria must be fixed before the Gate 0 results are reviewed.

If fewer than five businesses qualify, the owner decides in advance whether to:

- Run with a smaller sample and a pre-set pass mark.
- Extend recruitment without changing the eligibility criteria.
- Revisit the target segment.
- Stop the test.

Do not choose a fallback after seeing the results.

**Status:** Open. Blocks Gate 1.

### 10d. Prefilled-message capability

Verify whether the required unique prefilled message can be configured and reliably used in:

- WhatsApp simple boost flows.
- Instagram direct-message advertising flows.

Do not assume a capability available in one channel is available in the other.

**Status:** Open. Blocks Gate 1 until each channel's actual flow is verified.

### 10e. Evidence floors and comparison window

Gate 0 decides the final evidence floors and how posts are compared.

The proposed floors are three paid orders, three leads and three chats. The final decision must rest on the observed data.

Choose a comparison window after publication only if the underlying data supports it.

**Status:** Pending Gate 0.

### 10f. Unanswered-reply threshold

The inbox verdict needs a fixed threshold for "unanswered" (§3). The proposal is 1 hour of business-hours elapsed time, reported next to total elapsed time. The manual test and the product must use the same value.

**Status:** Open. Blocks Gate 1.

## 11. Verification requirements

Evidence and implementation must move through a defined verification ladder:

1. Recorded fixtures.
2. Automated tests.
3. Staging verification.
4. A real paid test using an explicitly authorised test account.

Passing one level does not mean the next level has passed.

For any attribution or reporting change, verify that:

- Explicit advertising referrals stay paid.
- Matching a Content Studio post does not turn a paid conversation into an organic one.
- Unmatched advertising referrals stay paid.
- Untracked conversations stay unattributed.
- Conversations, leads and orders are counted separately.
- Revenue stays grouped by currency.
- Meta-reported, Akilent-attributed and derived metrics stay distinguishable.

Never claim a gate has passed without the matching evidence.

## 12. Scope boundaries and current constraints

This strategy does not authorise implementation.

The existing pilot freeze stays in effect. Each gate needs its own approval before work begins.

Outside the current authorised scope:

- Autonomous campaign or budget changes.
- Automatic campaign publishing.
- Unapproved Meta permission submissions.
- Unrelated CRM expansion.
- New integrations outside an explicitly approved specification.
- Public claims that imply proven advertising performance before validation.
- A broad growth-intelligence product build before the demand test.

Reuse the existing Content Studio, Conversations and attribution foundations where they meet the acceptance requirements. Verify existing implementation details against the actual repository code before treating them as confirmed.

### Codebase facts (checked 12 November 2026; re-verify before building)

- WhatsApp inbound (`WhatsAppInboundService`, called from `apps/whatsapp/tasks.py`) ignores the Click-to-WhatsApp `referral` object (`source_type`, `source_id`, `source_url`, `headline`, `ctwa_clid`). Instagram inbound (`apps/instagram/services/inbound.py`) skips `referral` events. As a result, ad chats are reported as untracked.
- `EntryPoint.Kind` (`apps/conversations/models.py`) has no ad kind. Today it covers only tracked links, IG and FB comments, and WhatsApp campaign replies.
- The Facebook Page scopes (`_FB_SCOPES`, `apps/publishing/views.py`) include neither `ads_read` nor `ads_management`.
- `ContentPost.fb_post_id` and `ig_media_id` make it possible to match a referral to a Content Studio post. Explicit-only crediting is enforced by the `attribution_guess_credits_nothing` constraint.

The public lead message stays:

> Stop losing customers in your inbox.

Do not replace it with advertising-performance claims, or with public promises about launching campaigns based on this strategy.

## Appendix A: Longer-term diagnostic direction

What follows is a possible future diagnostic model. It is not current implementation scope, and it is not an approved feature plan.

The diagnosis should follow the actual customer journey:

| Evidence pattern | Possible bottleneck |
|---|---|
| Few chats | Reach, creative or offer |
| Many chats, few qualifying replies | Inbox responsiveness |
| Many replies, few leads | Conversation quality or qualification |
| Many leads, few paid orders | Sales process, offer or conversion |
| Paid orders, poor revenue-to-spend ratio | Advertising economics |

These are hypotheses, not conclusions.

For example, many chats and few leads should not automatically be blamed on poor targeting or a weak offer. Check the business's reply behaviour first.

Likewise, many leads and few paid orders do not show that the advertisement was poor. The evidence may point to a problem with the sales process or the offer.

The product should name the observable bottleneck, state the evidence and its uncertainty, and leave the business in control of its decisions.

This appendix describes a possible longer-term direction only. It does not set a current goal to build a broad growth-intelligence product.

## Appendix B: Original capability layers

The wider Ads concept can be described as five capability layers:

1. Organic evidence and promotion candidates.
2. Advertising-chat attribution.
3. Diagnosis of inbox handling and customer outcomes.
4. Read-only advertising performance and spend intelligence.
5. Guided campaign execution, subject to separate approval.

The validation gates in this strategy decide whether and when any of these layers go ahead.

Being listed in this appendix does not authorise the first four layers. Campaign execution remains a separate decision.

## Appendix C: Commercial principles

- Akilent charges the customer for its product capability, not a percentage of advertising spend.
- The customer pays Meta directly for advertising.
- The add-on price must be set before the demand test.
- Do not use discounts or favours to manufacture evidence of willingness to pay.
- Make no public claims about improved advertising results until verified customer evidence supports them.

The immediate commercial question is whether businesses will pay for one connection: evidence-backed choices about what to promote, linked to the outcomes of the conversations those promotions generate.
