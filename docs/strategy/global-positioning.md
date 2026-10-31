# Akilent Global Competitive Positioning Strategy

Market strategy · 31 October 2026 · Social-commerce SMEs · USA, UK and EU first

> **Status:** adopted by the product owner on 2026-10-31. It merges three owner inputs from that day: the global positioning recommendation, the competitor research update, and "Akilent: SWOT and Global Market Strategy". Facts were checked against the repo, against Meta's developer docs and against the web on 2026-10-31. Anything that couldn't be confirmed is marked **(?)** or "reported".
>
> **Freeze exception (owner, 2026-10-31):** the pilot feature freeze no longer applies to critical components that strengthen Akilent's position. Each component is still built under its own approved plan, in the order set in §9.

**Three connected documents, each with its own job:**

| Document | Job | Who changes it |
|---|---|---|
| `docs/marketing/messaging.md` | What Akilent can truthfully say and demonstrate **today**. It is the only source for customer-facing copy. | Copy changes follow its truth rule and claim register |
| `docs/strategy/global-positioning.md` (this file) | Which market Akilent pursues, how it competes and differentiates, its commercial model, and what to build next | Owner decisions |
| `docs/strategy/meta-growth-optimisation.md` | Long-term product direction (organic and paid growth engines, experiment engine) | Owner-authored; preserved as written |

The audit that checked the messaging guide against the repo is `docs/audits/MESSAGING-AUDIT-2026-10-31.md`.

---

## 0. Message layers and capability horizons

| Layer | Message |
|---|---|
| Global positioning | Growth intelligence for conversation-led commerce |
| Brand theme | Turn conversations into customers |
| Public lead | Stop losing customers in your inbox |
| Product explanation | Bring customer conversations, follow-ups, social publishing and sales measurement together |
| Long-term differentiation | Evidence-based attribution and human-controlled growth optimisation |

The long-term positioning is **not** a claim that the growth-optimisation engine exists. Only the public lead and the product explanation appear on customer-facing surfaces, and both follow `messaging.md`.

**Capability horizons.** These keep future work from turning into present-day claims.

| Horizon | What Akilent does |
|---|---|
| **Today** | Collects the post metrics Meta makes available. Records the conversations, leads, orders and paid revenue that explicit evidence links to a post (a tracked link or a comment reply). |
| **Next** | More reliable reporting, comparable baselines, and metric provenance. |
| **Later** | Recommends experiments, which a person reviews before anything runs. |
| **Much later** | Connects organic and paid intelligence. Any change to paid campaigns needs separate authorisation. |

The strongest differentiation is the **complete business journey** (post → chat → lead → order marked paid). It does not come from channel count, AI captions, scheduling or engagement analytics.

**Attribution is not causation.** A tracked post linked to a paid order shows where the customer came from. It does not prove that the post caused the purchase. Akilent says this openly.

---

## 1. Executive summary

Akilent launches globally from day one, in English, and concentrates marketing and support on the **USA, the UK and the EU**. The wedge is being the simplest tool that shows a small seller which posts brought chats, leads and paid orders. The figures are always explicit-evidence lower bounds.

- **Target:** small businesses (retail, beauty, food, services) that sell through Instagram, Facebook Pages and WhatsApp.
- **Channel fit by region:** Instagram and Facebook Page DMs first in the USA. WhatsApp first in the UK, Germany and Spain.
- **Price:** $49 or $99 a month. Meta bills customers directly for WhatsApp messages, with no markup from Akilent.
- **Before scaling:** GDPR-ready consent and data handling, a plain page explaining Akilent's price against Meta's costs, and 10–20 customers with documented results.
- **Main risks:** crowded competition (Respond.io, Wati, Zoko, Gallabox, SleekFlow, Trengo), dependence on Meta (including Meta's own Business Agent), and compliance across several regions.
- **Unverified:** that USA and EU users adopt more readily than users in Africa is an **owner statement**. It needs sign-up data by country.

---

## 2. Akilent today

Akilent puts WhatsApp, Instagram and Facebook Page messages into one shared inbox. It adds follow-up reminders, automations, AI answers (off by default), leads, orders that the business marks paid, campaigns, and Content Studio for planning and publishing to Instagram and Facebook Pages.

- **Core loop:**
  1. A post carries a tracked WhatsApp chat link.
  2. The chat lands in the inbox.
  3. The business records the order and marks it paid. **Akilent never collects money for businesses.**
  4. Each post shows its clicks, chats, leads, orders and paid revenue.
- **Attribution is a lower bound.** A customer who reaches the business another way is not credited to a post, and the product says so.
- **AI** is off until the business switches it on. Replies it sends are marked and logged.
- **WhatsApp:** each business connects its own number through Meta's guided setup (Embedded Signup), and Meta bills the business directly.
  - This is an owner statement, and it is consistent with the code: there is no credit-line call, and the billing docs and comments say so (audit §E).
  - It still needs a final check in Meta Business Manager.
- **Plans as seeded** (`seed_plans`; live database not verified; see audit §A):

| Plan | Price | Conversations/mo | WA numbers | History |
|---|---|---|---|---|
| Trial | $0, 14 days | 100 | 1 | 3 days |
| Starter | $19 | 1,000 | 1 | 14 days |
| Professional | $49 | 5,000 | 1 | 30 days |
| Business | $99 | unlimited | 1 | 90 days |

The SWOT lists no Starter plan. Whether Starter is active on prod is an open question. Public pricing bullets come from `billing_api.plan_card`, never from copy.

What is Live and what isn't follows `messaging.md` §4, as audited.

---

## 3. Market opportunity

Published estimates differ by a factor of about two. Plan on the low figure and treat the high one as upside. All of these sources are vendor or analyst blogs, so the figures are **indicative**.

| Figure | Value | Source (as cited by) |
|---|---|---|
| Conversational commerce, low | ~$12.6B (2026) | Mordor Intelligence (Omnichat) |
| Conversational commerce, high | ~$26B (2025), >$30B (2026) | Juniper Research (eGrow) |
| US social commerce | forecast >$100B (2026); ~46.8M Americans shop on Instagram | eMarketer and Capital One Shopping (Ringly) |
| WhatsApp in the USA | >100M users (July 2024), ~32% of adults | TimelinesAI |
| WhatsApp in Europe | ~122M users; Germany ~52M | TimelinesAI |

**Meta pricing, verified against Meta's own docs on 2026-10-31:**
- From **1 October 2026**, service messages (non-template replies inside the 24-hour window) are charged **per message**, as are utility templates sent inside an open window.
- The **72-hour free entry point window** for Click-to-WhatsApp ads and Facebook call-to-action buttons is unchanged.
- From **1 August 2026**, Meta charges per token for **Meta Business Agent** messages, Meta's own AI agent.
- Meta may change rates only on the first day of a quarter.
- A free allowance of about 1,000 service messages per number per month is **reported** by third parties (YCloud, Zoho). It was not found in the Meta docs extract. Confirm it on Meta's rate card before quoting it.

Sources: [Meta: pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing); [Meta: non-template message pricing updates](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages).

---

## 4. Segmentation and ideal customers

| Segment | Order | Pain | Evidence still needed |
|---|---|---|---|
| **Social-commerce retailers** (fashion, beauty, electronics, home-based and Instagram-led sellers) | Beachhead | Enquiries get lost across phones. Nobody knows which post sold | Sign-ups, activation and retention by country and channel |
| D2C brands and independent online stores | Adjacent 1 | Social is an acquisition channel, but its revenue is invisible | Whether they need a Shopify or WooCommerce order sync |
| Service businesses (bookings through messaging) | Adjacent 2 | Bookings and follow-ups are lost | Whether "order" maps to "booking" well enough |
| Agencies running social commerce for clients | Adjacent 3 | They report outcomes across many clients | Demand for multi-workspace management and reporting |

Region and channel fit: Instagram and Facebook Pages first in the USA, plus WhatsApp for communities where it is common. WhatsApp first in the UK, Germany and Spain.

**Not the goal:** matching Respond.io on channel count or enterprise features.

---

## 5. Competitors

**Stated plainly:** competitors already sell attribution, revenue analytics and AI agents. Akilent cannot win by claiming they lack these things.

Prices below were checked 2026-10-31. Most come from third-party pricing breakdowns, because vendor pages were not directly retrievable. Treat them as **reported**, and confirm on the vendor's own page before using any of them externally.

| Platform | Entry price (USD/mo) | Strengths | Attribution and AI | Meta fees |
|---|---|---|---|---|
| **Akilent** Professional | 49 | WhatsApp, IG and FB Page inbox; Content Studio; post → paid order | Explicit-evidence post attribution (lower bounds); AI answers, off by default | Customer pays Meta directly; no markup |
| **Zoko** Starter | 49.99 (Plus 79.99, Elite 139.99, Max 499.99) | WhatsApp CRM for Shopify | Analytics; AI sales agents on the customer's own OpenAI key | Meta billed directly; Zoko adds a per-conversation fee on Starter |
| **Wati** Growth | 59 annual / 69 monthly (Pro 119/149) | WhatsApp-first inbox, broadcasts, Shopify | Revenue analytics, Click-to-WhatsApp tracking, AI agents | Reported markup on template messages |
| **Respond.io** Starter | 79 annual / ~99 monthly (Growth 159, Advanced 279) | Broad omnichannel, workflows | Advanced reporting; AI agents from Growth | Billed separately |
| **Gallabox** | from ~112 annual (reported) | WhatsApp and Instagram AI agents, lead qualification | Click-to-WhatsApp ad attribution per conversation; sends purchase events back to Meta through CAPI for ROAS | Billed separately |
| **SleekFlow** Pro | 149 (free "Startup" tier, 100 contacts) | AI automation, Shopify | AI on Pro | Billed separately |
| **Trengo** Boost | ~€299 annual / €349 monthly | Team collaboration, many channels | AI surcharge (reported) | Usage-based conversations |
| **HubSpot** Marketing Hub | Starter ~$20/seat; Pro ~$800–890 + onboarding; Enterprise ~$3,600 | Full CRM and marketing automation | Broad marketing attribution and reporting | Not a WhatsApp-first product |
| **Meta Business Agent** | Per-token charges from 2026-08-01 | Meta's own AI agent for WhatsApp replies | Native to the platform | Meta |

**Where competitors lead today:** Shopify depth (Zoko, Wati, SleekFlow); ad attribution and CAPI (Gallabox); AI agents (all of them, now including Meta itself); channel count (Respond.io, Trengo); brand and budget.

**Where Akilent is different:** an honest lower-bound attribution model with an untracked bucket and no guessed credit; Content Studio and inbox in one tool; a low entry price; no markup on Meta fees.

Sources: [eesel: Wati pricing](https://www.eesel.ai/blog/wati-pricing); [respond.io pricing (IT)](https://respond.io/it/pricing.md); [eesel: Respond.io pricing](https://www.eesel.ai/blog/respond-io-pricing); [eesel: Zoko pricing](https://www.eesel.ai/blog/zoko-pricing); [Pulse Signal: Gallabox pricing](https://getpulsesignal.com/pricing/gallabox); [Gallabox docs: CTWA ads](https://docs-next.gallabox.com/whatsapp/ad-campaigns/ctwa-ads.md); [SleekFlow pricing](https://sleekflow.io/pricing); [eesel: Trengo pricing](https://www.eesel.ai/blog/trengo-pricing); [Docket: HubSpot Marketing Hub pricing](https://docket.io/resources/research/hubspot-marketing-hub-pricing); [Meta: non-template message pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages).

---

## 6. Category and differentiation

**Category:** growth intelligence for conversation-led commerce. This is a positioning term, not public copy.

**Revised thesis (after the competitor research):** Akilent does not differentiate by *having* attribution or AI. It differentiates by building a **more rigorous, transparent link between marketing origin and verified commercial outcomes**, followed by **controlled optimisation**.

What makes that link rigorous today, as found in the code:
- Only explicit evidence credits a post (a tracked link or a comment reply). Guesses credit nothing, and a database constraint enforces this (`attribution_guess_credits_nothing`).
- Revenue is reported as disjoint **credited**, **guessed** and **untracked** buckets, per currency. Nothing is shown as 0 before tracking exists.
- The outcome is a **paid order recorded by the business**, not a click or a lead form.
- Every number on a post is a lower bound, and the page says so.

Compare Gallabox, whose docs say its numbers "won't match Meta's native reporting exactly" because events are defined by Gallabox. Akilent's pitch is *we tell you what we know, and what we don't*.

**The three layers:**

| Layer | Today (Live) | Future work |
|---|---|---|
| Customer intelligence | Shared inbox, contacts, leads, orders, follow-ups, Business Health | Richer journey views |
| Marketing intelligence | Per-post clicks → chats → leads → orders → paid revenue; IG and FB post metrics | Baselines, metric provenance, content-theme reporting, better attribution coverage |
| Growth optimisation | — | Recommendations people review before acting; controlled experiments; honest "inconclusive" results |

**What must be proven for the thesis to hold:**
1. Pilot businesses understand lower-bound numbers and act on them.
2. Enough of their sales are tracked to make the numbers useful (the untracked share falls over time).
3. Prospects value "honest numbers" over "bigger numbers".

---

## 7. Pricing, packaging and unit economics

- **Price points** of $49 and $99 sit at the low end of the market. Packaging changes are **hypotheses** until the pricing matrix is approved.
- **Meta's WhatsApp charges are a variable cost to the customer, not to Akilent.** They depend on the recipient's market and the message category (marketing, utility, authentication, service). Since 1 October 2026 they also cover service replies. They raise the customer's total cost, not Akilent's cost of goods sold.
- **Cost transparency is a selling point.** Proposed: a "What you pay Akilent, and what you pay Meta" page. Writing it is a copy change, so it follows `messaging.md`.
- **Akilent's own variable costs to model per plan:** AI tokens (per AI answer and caption), email sending, media storage and transcoding, and infrastructure per workspace. *Gross margin per plan (?): needs real figures.*
- **Customer total cost to model:** subscription + Meta messages (market × message mix × campaign volume) + any AI usage.
- **Illustration only, not a forecast:** 10,000 customers × $49 = $490k MRR, or $5.88M ARR, assuming no churn or discounts.

---

## 8. SWOT and strategy matrix

Items marked (?) are guesses and need Akilent's own data.

**Strengths**
- A complete loop: post with chat link, shared inbox, order marked paid, and revenue per post.
- A low entry price ($49). Customers pay Meta directly, with no markup on messages.
- Customers keep their own WhatsApp number through Meta's guided setup.
- Trust features: AI is off by default and its replies are logged; data is deleted on request.
- Honest measurement: attribution is stated as a lower bound.
- Developer options: REST API, SMTP and webhooks.

**Weaknesses**
- One WhatsApp number on every plan.
- A tight trial (see the audit §A1 for the seeded limits).
- No customer logos or case studies yet.
- Three messaging channels only: no SMS, website chat (not released) or TikTok Shop.
- No in-product payments, by design. *Check whether prospects expect them (?).*
- No visible GDPR material such as data processing terms or EU data hosting (?).
- Support hours and languages across time zones (?).
- A small team and low brand awareness (?).
- Meta setup and billing steps may cause drop-off during signup (?).

**Opportunities**
- US social commerce above $100B. Instagram plus WhatsApp fits both the USA and Europe.
- Meta's October 2026 billing change makes plain cost explanations valuable.
- The 72-hour free window for Click-to-WhatsApp ad conversations lowers customers' costs when chat links are used with ads.
- Agencies reselling to small clients (?).
- Vertical templates for retail, beauty, food and services (?).

**Threats**
- Meta controls WhatsApp access, pricing and policy. It changed billing on 1 October 2026 and now sells its own AI agent.
- Established rivals with bigger budgets and existing customers in the USA and EU.
- Price pressure: Zoko's entry plan matches Akilent's, and SleekFlow has a free tier.
- GDPR fines (up to €20M or 4% of turnover) and Meta number bans for breaking opt-in rules (Kanal). Customers need consent tools.
- Small businesses cancel quickly when volume is low (?).
- Several privacy regimes at once: UK GDPR and US state laws (?).

| Pairing | Move |
|---|---|
| Strengths + Opportunities | Lead with the post-to-paid-order loop: Instagram and Facebook Page sellers in the USA, WhatsApp sellers in the UK, Germany and Spain. Back it with revenue-per-post case studies, shown as lower bounds. |
| Weaknesses + Opportunities | Offer a second WhatsApp number and get 3 case studies from US and EU customers. **Non-goal:** in-product card or wallet payments, because Akilent never collects money for businesses. |
| Strengths + Threats | Publish the "what you pay Akilent and what you pay Meta" page, and a consent flow that helps customers meet GDPR and Meta's opt-in rules. |
| Weaknesses + Threats | Finish the GDPR basics before paid acquisition (data processing terms, consent records, opt-out, deletion, EU hosting if needed). Don't chase channel count. |

---

## 9. Go-to-market and market entry

**Stages.** Each stage needs a gate before the next one widens the effort.

1. **Launch-ready.** English self-serve signup, GDPR-ready consent and data handling, and the cost-transparency page.
2. **Prove it in the USA and Europe.** 10–20 customers with documented revenue per post (lower bounds), split between Instagram/Facebook-led sellers in the USA and WhatsApp-led sellers in the UK and EU.
3. **Scale what works.** Partners, agencies and ads aimed at the best-performing country and channel; then support in the languages the signup data asks for.
4. **Review and widen.** Open other regions only where self-serve demand appears and support can cover it.

**Distribution:** direct and organic first (SEO, content, referrals, product-led trial). Agencies and ecosystem partnerships (Shopify, WooCommerce) come later, gated by demand.

**Country criteria:**

| Criterion | What to check |
|---|---|
| Demand evidence | Sign-ups, trial-to-paid conversion and churn by country and channel |
| Channel fit | WhatsApp reach (~32% of US adults; Germany ~52M users) against Instagram and Facebook use |
| Compliance | GDPR consent, records, opt-out and data requests; Meta opt-in rules; UK GDPR and US state rules (?) |
| How the business's customers pay | Akilent records payment but does not take it, so this only affects how orders are marked paid |
| Language | Product, support and AI replies in the main local languages |
| Meta message prices | Vary by recipient country; paid by the customer; changed 1 Oct 2026 |
| Competition and partners | Local competitor prices; agencies or resellers |

Exact gate numbers (customers, churn, conversion) are set once real figures are in. The 12-month roadmap gets one gate per quarter; owners and dates are **TBD by the team**.

**International readiness checklist** (status from audit §F):

| Capability | Status |
|---|---|
| Multi-currency reporting | Exists: revenue per currency in `journey()` |
| International phone numbers | Not audited (?) |
| Time zones | Exists: `USE_TZ`; a time zone per workspace |
| Language and i18n | **Gap:** English only (no translation tags, no `locale/`) |
| Subscription payments | PayPal (the only live provider) |
| Data deletion on request | Exists (`data-deletion` route) |
| Consent records and opt-out | Partly exists: contact `ConsentStatus` (opted in, opted out, unknown) and email consent fields |
| DPA / data-processing terms, hosting-region statement | **Gap:** not in `templates/legal/` |
| Country on the workspace | The field exists; its use in signup and reporting is unverified |

---

## 10. Defensibility

| Moat | How Akilent builds it |
|---|---|
| Workflow integration | Become how the business runs enquiries, follow-ups and orders every day |
| Attribution integrity | Reliable, explainable links from content to conversation, lead and paid order |
| Historical business intelligence | Longitudinal views of each business's own performance |
| Optimisation intelligence | Learn which experiments improve outcomes, and say so honestly when results are inconclusive |
| Customer trust | Transparency, privacy, human control, no markup on Meta fees |

**Data caveats.** Data must be accurate, permissioned and isolated per tenant. Customer data is never reused freely as training data. More data is not a moat by itself.

**Meta dependency.** Keep Instagram and Facebook Pages strong alongside WhatsApp, and tell customers early about Meta changes. Meta's own Business Agent makes AI replies a weak differentiator. Attribution integrity and the full journey matter more.

---

## 11. Three-year thesis

The stages are gated by evidence, not by dates.

1. **Credibility:** complete the pilot scope; the journey test and the staging journey pass.
2. **Segment validation:** pilots show activation, retention and willingness to pay.
3. **International demand:** US, UK and EU customers recruited and compared.
4. **Repeatable distribution:** SEO, content, referrals, then agencies.
5. **Differentiation:** reliable growth analytics, then recommendations people review before acting, then controlled experiments.
6. **Expansion:** new segments and capabilities where evidence supports them.

---

## 12. Metrics, risks and data needed

**Monthly metrics** (targets are set after the first quarter of real data):

| Metric | Why it matters | Source in the repo |
|---|---|---|
| Sign-ups and trial-to-paid by country and channel | Tests the claim that the USA and EU adopt first | Needs a country field on signup (?) |
| Monthly churn (and reasons) | SMBs cancel when results aren't visible | Billing |
| Attributed paid revenue per customer (lower bound) | The proof behind the positioning | `journey()`, `manage.py journey_evidence` |
| Setup completion rate | Cost of Meta setup friction | Onboarding registry, Pilot Command Center |
| Opt-out and block rate on WhatsApp numbers | Consent quality and number rating | (?) |
| Support tickets per customer, by time zone | How far the team scales | (?) |
| Usage of attribution features and recommendations | Is the differentiator valued? | Business Health, post Results |

| Risk | Response |
|---|---|
| Meta changes WhatsApp access, approval or pricing | Keep IG and FB strong; tell customers early |
| Customer consent practices break GDPR or Meta rules | Consent records, opt-out handling and clear templates in the product; responsibilities set out in the terms |
| Rivals match the price or copy attribution | Win on rigour, simplicity, proof and cost transparency |
| WhatsApp reaches fewer people in the USA | Lead with IG and FB in the USA |
| Support can't cover several time zones | Self-serve help, support hours by region |
| Spreading across too many countries | Open each region only after its gate is met |

**Data needed:**
- [ ] Sign-ups, trials and paying customers by country and channel
- [ ] Trial-to-paid conversion and monthly churn, with reasons
- [ ] Revenue, variable costs and margin per plan
- [ ] Which business types sign up most, and where they drop out of setup (onboarding registry)
- [ ] Customer feedback: what they like most and complain about most
- [ ] Competitors customers mention, and the prices they quote
- [ ] Team size and support coverage across time zones
- [ ] How the businesses' own customers pay in the USA and EU
- [ ] GDPR status: data processing terms, EU hosting, consent tools

---

## 13. Evidence-gated build roadmap

The freeze no longer applies to this roadmap, but each step still needs its own plan.

| Priority | Work | Permission |
|---|---|---|
| 1 | Complete and verify the approved pilot foundations | Existing approved scope |
| 2 | Reliable growth analytics | Separate implementation plan |
| 3 | Human-reviewed organic recommendations | Separate implementation plan |
| 4 | Controlled creative experiment engine | Separate implementation plan |
| 5 | Read-only paid performance intelligence | Separate decision and plan |
| 6 | Cross-channel growth intelligence | Evidence and separate approval |

**The autonomous growth agent stays out of scope.**

What each priority contains, and the gate it needs:
1. **Pilot foundations and launch readiness.**
   - Contents:
     - the staging journey checklist (Phase 5, J1–J10) run with real accounts;
     - the messaging-audit fixes, which include five blockers: reachable unreleased features, payment-link remnants, and the footer and demo wording;
     - the GDPR basics (consent records, opt-out, DPA, hosting decision);
     - time zones and i18n where they block the USA and EU;
     - the cost-transparency page.
   - *Gate:* the journey test and the staging journey pass.
2. **Reliable growth analytics.**
   - Contents:
     - wider attribution coverage, so less revenue is "untracked" (for example tracked entry points for IG and FB DMs, and per-campaign links);
     - comparable baselines;
     - metric provenance (source, time collected, delay);
     - outcomes grouped by content theme or format (pillar currently lives on `ContentPlanItem`, not on the post);
     - a country field for signups.
   - *Gate:* the untracked share is measured, and pilots read the reports correctly.
3. **Recommendations people review before acting.**
   - Recommendations are explained, never dictated, and built only on verified outcomes.
   - *Gate:* pilots act on them, measured as the recommendation adoption rate.
4. **Controlled experiment engine.**
   - It runs hypothesis → variant → comparison over a window, needs human approval before every publish, and reports "inconclusive" honestly.
   - *Gate:* the analytics have enough volume to compare.
5. **Read-only paid intelligence.**
   - Requires an ads integration, permissions and a data model, none of which exist today.
   - *Gate:* a separate decision.
6. **Cross-channel growth intelligence.**
   - *Gate:* evidence and separate approval.

**Demand-gated items, outside the sequence:** Shopify and WooCommerce order sync; a second WhatsApp number; agency multi-workspace.

**Data availability re-check (2026-10-31).** The notes in `meta-growth-optimisation.md` §C (verified 2026-10-25) still match the code:
- `apps/publishing/metrics.py` collects the same IG and FB metrics.
- `profile_visits` is not collected.
- No ads integration exists.

Meta's pricing changed after that note: per-message service charges from 2026-10-01, and Business Agent token charges from 2026-08-01. Re-check again before any priority 2 or 5 plan relies on these notes.

---

## Appendix: adopted decisions (2026-10-31)

| Decision | Strategic direction | Guardrail note |
|---|---|---|
| Market ambition | Global | English self-serve from day one; marketing focused on the USA, UK and EU |
| Initial customer segment | Social-commerce SMEs | — |
| Initial product category | Growth intelligence for conversation-led commerce | Positioning term only, never present-day copy |
| Primary positioning | Turn conversations into customers | Brand theme. The public lead stays "Stop losing customers in your inbox" |
| Immediate differentiation | Customer journey and revenue attribution | Differentiate on rigour and transparency (lower bounds, explicit evidence), not on having attribution |
| Long-term differentiation | Evidence-based marketing optimisation | Human approval before every publish; no autonomous agent |
| Initial growth engine | Direct acquisition and organic growth | — |
| Distribution expansion | Agencies and strategic partnerships | Gated by demand |
| AI philosophy | Human-controlled, progressively more intelligent | AI is off by default |
| Geographic expansion | Evidence-driven, not geographically restricted | USA, UK and EU first, by owner statement; verify with signup data |
| Payments | Akilent never collects money for businesses | In-product card and wallet payments are a non-goal |
