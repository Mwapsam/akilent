# Akilent Meta Growth Optimisation Strategy

Future product strategy · 25 October 2026 · Organic and paid growth · Human-controlled

> **Status:** strategy direction, authored by the product owner on 2026-10-25. It authorises **no** implementation. The pilot feature freeze applies. The current work is Phase 0 (prove the foundations; see `docs/ROADMAP-GAPS.md`, Tier 3 pointer). Sections A and B are the owner's text; image and citation links from the original paste were removed. Sections C and D are additions: verified data notes and internal strategy input.

---

## A. Strategy

Akilent should evolve from a platform that helps businesses publish content and track the sales that follow into a data-driven growth adviser that helps businesses make better marketing decisions.

The goal is not to trick Meta's algorithms. It is to help each business consistently produce content and advertising that earn attention from relevant audiences, generate meaningful engagement, and turn that attention into customers.

The defining advantage should be the connection between three things that are often managed separately:

- **Content performance:** what gets attention and engagement?
- **Paid performance:** which creatives and campaigns reach the right audience efficiently?
- **Business outcomes:** which activities generate conversations, qualified leads, orders and revenue?

Akilent should use those signals to recommend what a business should do next. Strategic decisions, approvals and publishing stay under human control.

**Product north star: turn marketing data into better growth decisions.** Akilent should help businesses understand what works, why it works, what to test next, and whether the results actually benefit the business.

- **Organic growth:** reach, attention, engagement and audience growth.
- **Paid growth:** efficient spend, qualified demand and incremental results.
- **Customer acquisition:** conversations, leads and orders linked to marketing.
- **Human control:** explainable recommendations, approval and accountability.

### 1. The strategic model: two growth engines, one measurement system

Organic and paid growth should share business context and creative learning, but they must not be treated as the same optimisation problem.

- **Shared business intelligence:** business goals, audience, offer, content history, conversations, leads, orders and revenue.
- **Organic engine (earn attention):** content concepts, creative quality, distribution, engagement and audience growth.
- **Paid engine (buy distribution):** campaign objectives, creative testing, spend efficiency and conversion quality.
- **Shared outcome measurement:** compare performance fairly, account for attribution limits, and recommend the next human-approved action.

The two engines can inform each other:
- Strong organic engagement may suggest a creative concept worth testing in paid campaigns.
- A paid campaign may reveal which offer resonates with a target audience.

Neither result proves that the same creative will perform equally well in the other environment.

### 2. What Akilent should optimise

The engine should never pursue a single universal engagement score. It should use a hierarchy of objectives, chosen to match the business's goal.

| Objective | Primary measures | What Akilent should learn |
|---|---|---|
| Discovery | Non-follower reach, impressions, reach growth | Which original content reaches relevant new audiences? |
| Attention | Average watch time, retention, completion where available | Which hooks and formats keep the audience interested? |
| Engagement | Shares and likes per reach, saves, meaningful comments | Which content is valuable enough to interact with or share? |
| Audience growth | Profile visits, follows and available follow-through metrics | Which content attracts the right long-term audience? |
| Leads | Qualified conversations and leads | Which content attracts potential customers, not just attention? |
| Revenue | Paid orders, revenue by currency, cost per acquisition where measurable | Which marketing activities generate economically valuable outcomes? |

Instagram's publicly described ranking signals include predicted viewing and interaction behaviour, and their relative importance varies by surface and audience. Meta's advertising systems optimise delivery according to campaign objectives and conversion events.

**The optimisation rule:** Akilent should optimise for the highest-value objective the business can reliably measure.
- If the goal is awareness, optimise for relevant reach and attention.
- If the goal is engagement, optimise for meaningful interactions relative to exposure.
- If the goal is sales, optimise for qualified leads, paid orders and acquisition economics, not likes alone.

A business should be able to change its objective without losing its historical data. The system should also show trade-offs: a creative may generate more reach but fewer sales, or fewer leads at a better cost.

### 3. Organic growth engine

The organic engine should help users discover what resonates with their audience and create better content from that evidence.

- **A. Diagnose performance.** Compare posts within the same account, platform, media type and comparable time window. Establish baselines instead of treating one viral post as proof of a winning strategy.
- **B. Recommend creative experiments.** Suggest alternative hooks, opening frames, formats, topics, storytelling approaches and calls to action. Explain the evidence behind each suggestion and what the experiment is meant to test.
- **C. Let the user choose.** Present recommendations in Content Studio for review. The user selects, edits, approves and publishes. Akilent should not silently change the content calendar or publish experiments.
- **D. Evaluate the result.** Compare the result against the baseline and, where possible, a comparable control. Record whether the hypothesis was supported, inconclusive or contradicted.

The system should account for seasonality, audience size, paid promotion, posting format and differences in distribution. It should not confidently recommend "post at 7 pm" just because a few successful posts happened to go out at that time.

### 4. Paid growth engine

Paid optimisation needs its own logic. An advertiser controls budgets, campaign objectives, audience settings and creative tests in ways that organic distribution does not allow.

The future paid engine should cover four areas:
- **Creative performance.** Identify which approved creative concepts, opening hooks, offers and formats produce the desired outcome. Recommend new variants when performance deteriorates or the evidence supports a test.
- **Campaign efficiency.** Monitor spend, cost per result, conversion rate and acquisition cost where the data is available. Flag overspending, deteriorating efficiency, and campaigns without enough data for a reliable conclusion.
- **Conversion quality.** Connect ad results to conversations, qualified leads and paid orders where attribution supports the connection. Optimise for customer quality, not cheap but unqualified clicks.
- **Experiment recommendations.** Suggest a clearly defined creative or campaign test, with its hypothesis, budget implications, primary metric, evaluation window and stopping criteria. The user decides whether to run it.

Meta's own advertising guidance supports creative testing and warns that too many edits can disrupt campaign learning. Akilent should therefore avoid recommending constant changes based on short-term fluctuations.

**Important boundary:** this strategy does not authorise paid advertising management. Before building it, Akilent needs a separate decision on:
- Ads Manager account connections and permissions;
- campaign and ad creation;
- budget controls and billing exposure;
- supported API capabilities.

Reading ad performance is a smaller scope than changing or launching campaigns.

### 5. A shared learning system without misleading attribution

This is where Akilent can become more useful than a conventional social scheduler. The engine should keep three levels of evidence distinct:

1. **Observed platform performance.** Meta reports that a post or ad received certain impressions, views, interactions or conversions. Store the source, reporting period, retrieval time and metric availability.
2. **Attributed business outcomes.** A tracked post or ad is connected to a conversation, lead or paid order by a documented attribution method. Keep explicit matches distinct from heuristic matches and from unattributed activity.
3. **Causal impact.** Evidence that a marketing activity caused results beyond what would otherwise have happened. This generally requires an experiment or lift measurement; ordinary attribution alone does not establish causation.

Two examples:
- If a Reel generates many shares but few qualified conversations, Akilent might recommend improving the offer or adding a more relevant call to action.
- If an ad generates inexpensive conversations but few paid orders, Akilent should look into lead quality and conversion rather than automatically increasing spend.

These are hypotheses for a human to review, not claims that the algorithm knows exactly why a post succeeded.

### 6. Recommendation design: explain, don't dictate

Every recommendation should be a small, auditable decision proposal, not a mysterious AI score.

*Illustrative example (not a finding from real Akilent account data): "Test a product-demonstration Reel"*
- **Observation:** product-demonstration posts performed better than the account's usual posts on shares per reach over the selected reporting period.
- **Hypothesis:** showing the product solving a recognisable customer problem in the opening seconds may increase meaningful engagement.
- **Proposed experiment:** keep the product and offer constant; test two different opening hooks; use the same primary call to action; compare performance over a suitable observation window.
- **Primary metric:** shares per reach. **Guardrail metric:** qualified conversations per reach.
- **Decision rule:** repeat the approach if the improvement is meaningful and repeatable without harming business outcomes. Otherwise revise the hypothesis or record the result as inconclusive.
- **Human decision required.**

Recommendations should include their evidence, confidence, data limitations and expected trade-offs. Low-volume accounts should get conservative guidance, not false certainty.

> Zero and low reach posts: see `docs/strategy/content-performance-recovery.md` (diagnose visibility before content; missing numbers are never zero).

### 7. Trust and platform integrity

The strategy must improve legitimate distribution, not exploit or deceive the ranking systems. Akilent should:
- favour original, useful and audience-relevant content;
- identify possible recommendation-eligibility issues and point users to the relevant platform status or policy information when available;
- avoid fake engagement, automated engagement manipulation, mass repetitive comments, misleading engagement bait and attempts to evade enforcement;
- never claim to know hidden ranking weights, guarantee reach or promise a specific algorithmic outcome;
- respect Meta permissions, rate limits, account restrictions and advertising policies;
- make AI-generated recommendations reviewable and preserve the user's final authority.

This is not just a compliance constraint. Meta has explicitly described recommendation eligibility and its efforts to prioritise original content over unoriginal material.

### 8. Phased roadmap

The growth engine is a future direction, not permission to expand the current pilot. Each phase needs a separate scope decision before implementation.

| Phase | Name | Scope | Exit / gate |
|---|---|---|---|
| 0 (current priority) | Prove the foundations | Finish the approved pilot work: reliable publishing, approval correctness, tenant isolation, end-to-end attribution, currency-safe revenue reporting and staging verification. | Exit: the existing customer journey is proven end to end. No growth-engine implementation is implied. |
| 1 (future proposal) | Reliable growth analytics | Period-based organic and paid reporting, metric provenance, comparable baselines, and explicit limits on what the available Meta data supports. | Gate: API access, metric coverage, account permissions and data quality verified. |
| 2 (future proposal) | Human-reviewed organic recommendations | Explainable patterns, creative hypotheses and a review workflow. Start with simple rules and statistical comparisons before more complex models. | Gate: recommendations are measurable, understandable and evaluated against actual outcomes. |
| 3 (future proposal) | Paid performance intelligence | Ad reporting, creative comparisons, acquisition economics and conversion-quality analysis. Creating or editing campaigns and changing budgets stay out of scope until separately authorised. | Gate: ad account access, billing and permission boundaries, attribution quality and evaluation method established. |
| 4 (future proposal) | Cross-channel growth intelligence | Combine organic and paid evidence with conversations, leads, orders and revenue. Recommend which hypotheses to repeat, stop or test next. | Gate: the system distinguishes platform metrics, attributed outcomes and causal evidence. |

### 9. How to measure whether the strategy works

Akilent should measure its own usefulness, not just report its customers' marketing metrics.

| Dimension | Success measure |
|---|---|
| Recommendation quality | Share of recommendations accepted, edited, rejected or deferred, with reasons where users give them |
| Experiment quality | Experiments completed with enough data and a documented conclusion |
| Organic effectiveness | Improvement in relevant reach, attention and meaningful engagement against suitable baselines |
| Paid effectiveness | Improvement in cost per qualified result and acquisition economics where measurable |
| Business outcomes | Qualified leads and paid orders, with attribution quality disclosed |
| Trust | Incorrect claims, unsupported recommendations, unwanted actions and policy-related incidents |

Do not define success as "the AI's recommendations are accepted most often". An accurate recommendation may be rejected for sound business reasons. Nor should Akilent take credit for a customer's sales just because a tracked post came before a purchase.

### Final strategic decision

The long-term product should be a human-controlled Meta growth intelligence platform with separate organic and paid optimisation engines, joined by customer and revenue measurement. Its competitive advantage should not be a claim to have found a secret algorithm hack. It should be the ability to learn from each business's real performance and turn that evidence into better marketing decisions.

For the current pilot, keep the existing freeze and close the approved correctness, attribution, security and staging gaps first. The roadmap above defines the direction to evaluate afterwards. It does not authorise building the new engines, paid campaign controls or broader AI automation.

**Owner decisions recorded 2026-10-25:**
- Organic first; paid is a later, separate system that begins read-only.
- The end state for autonomy is an experiment engine: generated variants and analysed tests, with human approval before every publish.
- An autonomous growth agent is out of scope.

---

## B. Positioning and product principles (owner's companion note, 2026-10-25)

Akilent can adapt by treating AI as a way to help businesses create better content, respond to customers faster, and understand which marketing efforts generate sales, not simply as a tool for generating more posts.

Producing more content is not the same as producing better marketing results. Businesses need enough creative variety to keep up with changing audiences, but they also need control over what gets published and evidence that their efforts are working.

### 1. Advertising challenges translated into Akilent's strategy

| Challenge | Akilent response | Akilent fit |
|---|---|---|
| More content | More productive content creation: turn one marketing idea into channel-appropriate drafts instead of starting from scratch each time. Over time, AI could help develop alternative hooks, captions and calls to action. | Content Studio and AI drafting; more advanced creative generation is future scope. |
| More creatives | Controlled creative experimentation: compare different approaches to the same offer, audience or topic. Measure which variations earn meaningful engagement and enquiries, rather than producing dozens of alternatives. | Future organic recommendations and a human-approved experiment engine. |
| Better experiences | Connect content to conversations: when a customer responds to a post, help the business continue the conversation, keep customer context, follow up appropriately and turn genuine interest into an order. | Conversations, contacts, leads, follow-ups and orders. |
| Better oversight | Human control and clear evidence: make drafts, approvals, publishing status, failures and results visible. Explain why a recommendation is made, what supports it and what the owner must decide. | Approval workflows, publishing reliability and attribution reports. |
| Timely content | Better planning and execution: plan around real promotions, customer questions and relevant opportunities. Later, performance data could show when a topic deserves another post. | Existing planning and scheduling, with future evidence-informed suggestions. |
| Faster speed to market | Remove operational friction: shorten the time from deciding what to say to having a reviewed, ready-to-publish post. Detect blockers early and make the next action obvious. | Improve the reliability and usability of the existing publishing workflow before adding automation. |

### 2. The feedback loop that makes Akilent different

The loop:
1. **Plan:** choose a business goal, offer and topic.
2. **Create and review:** draft, inspect and approve.
3. **Publish and engage:** reach people and handle incoming conversations.
4. **Convert:** connect conversations to leads, orders and paid revenue.
5. **Measure:** understand what happened and how reliable the evidence is.
6. **Recommend the next experiment:** suggest what to repeat, improve or test next; the owner decides.

The final step of the loop is a future capability. The current product does not deliver it.

The strongest opportunity is connecting content creation to actual business outcomes, not another AI content generator. That depends on trustworthy attribution: a conversation matched heuristically to recent activity must not be presented as a confirmed conversion from a specific post.

### 3. Now versus later

| Stage | Priority | Decision |
|---|---|---|
| Now: foundation | Prove the complete journey from post to paid revenue | Fix approval and AI-draft correctness, improve failure visibility, test attribution, verify tenant isolation and validate the real Meta integration in staging. |
| Next: evidence | Reliable, understandable organic performance data | Verified metrics, account-specific baselines and honest indications of missing or insufficient data. |
| Then: assisted growth | Recommend what to repeat, improve or test | Explainable, human-reviewed organic recommendations once the foundations and pilot evidence justify them. |
| Later: experimentation | Learn systematically which creative approaches work | A controlled experiment engine with human approval and measured business outcomes. |
| Separate decision: paid | Understand paid performance | Start with read-only intelligence; anything that changes campaigns needs separate authorisation. |

A sophisticated recommendation engine built on incomplete attribution or unverified metrics would produce confident-sounding advice without dependable evidence.

### 4. Product principles

- **Optimise for outcomes, not volume.** More posts are useful only when they serve a business goal.
- **Keep people in control.** AI can draft, analyse and recommend; publishing and consequential changes go through approved workflows.
- **Measure meaningful attention.** Reach, saves, shares, substantive engagement, conversations, leads and paid orders matter for different reasons. No single metric tells the whole story.
- **Be honest about uncertainty.** Distinguish measured results from estimated attribution, unavailable metrics and insufficient sample sizes.
- **Make the next action clear.** An owner should understand what needs approval, what failed and what action is worth considering.
- **Earn automation through evidence.** More autonomy follows demonstrated reliability and pilot results; it does not come first.

### 5. The "90% of marketers" statistic

**Unverified.** The claim that 90% of marketers agree generative AI is a key creative-development tool has no confirmed source, survey population or question wording. It must not appear in `docs/marketing/messaging.md` or any public copy until those are confirmed. Industry adoption also does not show that AI-generated content improves conversion rates, or that Akilent customers need more AI features. Those are hypotheses to validate in the pilot.

---

## C. Data availability notes (verified 2026-10-25)

Checked against the code and Meta's *Instagram Media Insights* reference (updated 2026-06-18).

| Item | Status |
|---|---|
| Collected today (`apps/publishing/metrics.py`) | IG: reach, views, likes, comments, saves, shares, replies (stories), total_interactions. FB: reach, views, reactions, comments, shares. Clicks come from Akilent's own `LinkClick`. |
| In Meta docs, not yet collected | IG `profile_visits` and `profile_activity` (breakdown: bio link, call, direction, email, text); `reels_skip_rate` (first 3 seconds, "in development"); `reposts`; story `navigation`; story `link_clicks`; `total_views`, `total_likes` and `total_comments` (include boosted reach; Facebook-login only). FB `video_insights` view-time metrics. |
| Not in IG media insights | Non-follower reach. |
| Unverified | Reels average watch time and follows per media. Check before relying on them. |
| Limits | Carousels (albums) return **no** insights. Story metrics last only 24 hours, and fewer than 5 viewers returns error 10. Data can lag up to 48 hours. A missing metric returns an empty set, not 0. Metrics marked "in development" can change. |
| Post descriptors | No structured hook field. Pillar lives on `ContentPlanItem`, not on the post. `format_choice`, `media_type` and CTA exist. |
| Paid | No ads integration, permissions or data model exist. |

---

## D. Content Studio switching case

**Strategy input, not approved messaging.** Nothing here changes `docs/marketing/messaging.md`, the claim register, the website or demo copy. Competitor statements are *verify before public use*: none were checked against current competitor pricing or feature pages.

### Why would a social media manager leave their current tool?

**Honest answer:** Akilent cannot win on channel count, creative tooling or agency features:
- It publishes only to Facebook and Instagram.
- It has no media library, post templates, brand voice, month view, report export or workspace switcher.
- Meta Business Suite is free for FB and IG scheduling, insights and an inbox *(verify before public use)*.

Akilent is worth switching to for **one job**: *showing which posts brought in customers and paid orders, and replying to those customers in the same place.* Schedulers stop at likes and reach. Akilent follows the post into the chat, the lead and the paid order.

- **Who should switch:** a social media manager, or the owner, at a business that sells through WhatsApp and Instagram DMs and is asked "what did our posts actually sell?"
- **Who should not switch:** agencies managing many brands on TikTok, LinkedIn, X or YouTube; content-heavy teams that need asset libraries and design tools.

### Switch reasons and whether they are true today

| # | Reason | True today? | What makes it true |
|---|---|---|---|
| 1 | See which posts started chats, leads and paid orders, per currency | Code yes; **not proven live** | Journey acceptance test and staging verification |
| 2 | Reply to the customers a post brought in, in one inbox (WA, IG DMs, IG comments) | Yes; IG waits on App Review | IG App Review (T0-15) |
| 3 | Every post can carry a trackable WhatsApp chat link (`/go/<code>`) | Yes | — |
| 4 | Honest numbers: unavailable is never shown as 0, and untracked chats are never credited to a post | Partly | Explicit/heuristic split and an unattributed bucket in reports |
| 5 | Approvals you can trust: an edit after approval needs re-approval | Partly (hash check exists) | In-review state, approved hash, AI provenance |
| 6 | Publishing that says when it failed and why, and never double-posts | Partly (no alert) | Failure alerts |
| 7 | A report the boss reads: posts → chats → orders → revenue | In-app Insights journey only | Candidate: shareable or exported journey report (needs approval) |

### Switching blockers (table stakes a manager will miss)

Each is a feature and needs pilot evidence or an explicit freeze exception:
- duplicate a post, or save one as a template;
- a month calendar view;
- a media library;
- export or scheduled delivery of the journey report.

More channels are **not recommended**; they are not the differentiator.

### Go-to-market input (owner, 2026-10-25)

**Five buying reasons:**
1. Know which posts bring paying customers.
2. Manage customer conversations in one place.
3. Stop losing potential customers.
4. Make better marketing decisions (a future capability).
5. More control over publishing.

**First targets, in order:**
1. Social-commerce retailers selling through WhatsApp and Instagram (the best initial target).
2. Service businesses that take bookings.
3. Businesses already spending on boosts or marketing services.

The common trait is that inquiries already arrive through social media.

**What a buyer needs to see:**
- a verifiable link between a tracked post and a conversation;
- leads and orders connected to the journey;
- correct paid-order reporting, by currency;
- clear publishing status and failure alerts;
- eventually, evidence-based recommendations.

Lead with outcomes, not AI generation, channel count or autonomy.

**Validation before expansion:**
1. Recruit businesses that sell through WhatsApp and Instagram.
2. Learn how they track inquiries today.
3. Demo the full post-to-paid-order journey.
4. **Ask them to pay for the pilot.**

Success means they can answer "which of our posts brought us paying customers?". If their pull is design tools, more channels or cheaper scheduling instead, record that as evidence against this positioning.

### Messaging decision and switch condition (owner, 2026-10-25)

- **Public lead stays:** "Stop losing customers in your inbox". Existing public messaging is unchanged.
- **Support:** WhatsApp and Instagram conversations together with contacts, leads and orders.
- **Secondary, qualified:** Akilent is *designed to* connect tracked content to conversations and sales. Never present this as a proven live outcome before the switch condition is met.
- **Pilot-demo message:** "Know which social media posts bring you customers". Use it only in sales demos with the seeded demo workspace.
- **Switch condition:** the journey acceptance test passes **and** the real staging journey is verified. Both are required; passing automated tests alone never qualifies. Meeting the condition makes a public change *eligible* for a decision, based on pilot feedback and observed customer value. The change is not automatic.
