# Akilent Problem–Workflow Framework

Product strategy · 31 October 2026 · Owner-directed · The reference for product decisions

> **Status:** written 2026-10-31 at the owner's direction. It says **which problems** Akilent must solve and **how** the product should solve them. It authorises nothing on its own.
> - The first build slice (the follow-up redesign) is approved separately. It is specified in `docs/design/followups-interaction-contract.md` and staged in `docs/design/followups-implementation-checklist.md`.
> - Everything else here is documented, not authorised.

| Document | Job |
|---|---|
| `docs/strategy/global-positioning.md` | Where Akilent competes and why; the build roadmap (when, and under what conditions) |
| **This file** | Which problems Akilent solves, how its workflows solve them, and how we know they work |
| `docs/marketing/messaging.md` | What Akilent may truthfully say today |
| `docs/strategy/meta-growth-optimisation.md` | Long-term product direction (owner-authored, preserved) |

The public lead is unchanged: "Stop losing customers in your inbox."

---

## 1. Central problem: the marketing-to-revenue gap

Social sellers spend time and money on content, attention and replies to customers. Even so, they struggle to answer five questions:
1. Which marketing activity brings valuable conversations?
2. Which conversations become real customers?
3. Which potential customers are being lost?
4. Which marketing efforts bring revenue?
5. What should they improve to get better results?

**Akilent's purpose:** connect marketing, customer conversations and commercial outcomes into one measurable business workflow.

The philosophy: Akilent connects the whole commercial journey.

| Stage | Question it answers |
|---|---|
| Marketing | Where did customers come from? |
| Conversations | Who showed interest? |
| Opportunities | Who could become a customer? |
| Revenue | Who actually bought? |
| Evidence | What worked, what failed, and what should improve? |

## 2. Target customer

Small and growing social sellers who **close through conversations** on WhatsApp, Instagram and Facebook Page messages.
- They often take payment outside any checkout, by cash, bank transfer or mobile money, and record it by hand.
- They are owners and small teams, not analysts. Many are first-time software users, and many work mainly from a phone.

## 3. The six core problems

| # | Problem | What the business experiences | Cause | Business consequence | What Akilent must solve |
|---|---|---|---|---|---|
| P1 | Marketing is disconnected from acquisition | Posts get engagement and WhatsApp enquiries arrive, but nobody knows which post brought which enquiry | Chats arrive without their origin; channels don't share identifiers | Marketing effort can't be judged | Link published content to incoming conversations; keep the origin; separate tracked, untracked and uncertain; measure each post's commercial contribution |
| P2 | Conversations are disconnected from sales | A customer asks, negotiates and buys, but the sale can't be tied to the chat | Sales close in chat and are paid offline | Revenue can't be traced to where it came from | Link conversations → contacts → leads → orders → paid; credit outcomes to the conversation they came from |
| P3 | Attribution can't be trusted | Some origins are known, others missing or guessed | Evidence is incomplete by nature | Decisions are made on false certainty | Keep provenance; separate verified from inferred; keep an explicit untracked bucket; never credit guesses; show coverage and limits; keep history when content is deleted |
| P4 | Opportunities are lost | Replies are late, chats go cold, follow-ups are forgotten | Work is spread across phones and people | Avoidable lost revenue | Show conversations needing attention; detect unanswered chats; surface leads to follow up; show what's at risk; track whether follow-ups convert |
| P5 | Content performance isn't tied to outcomes | The most-liked post isn't the one that sells | Engagement metrics stand in for value | Content strategy aims at the wrong target | Compare posts on chats, leads and paid orders; later, by content theme; keep engagement separate from revenue |
| P6 | They don't know what to improve | Data exists but doesn't lead to decisions | Analytics stop at "what happened" | No learning loop | *Later:* find weak stages, patterns and recurring losses; recommend measurable improvements; run controlled experiments. **Depends on P1–P5 resting on trustworthy data.** No autonomous engine |

## 4. Desired outcomes

| # | Outcome (measurable) |
|---|---|
| P1 | Businesses can see which marketing brought each tracked conversation, and what share is untracked |
| P2 | Every paid order shows the conversation it came from, where one exists |
| P3 | Every paid order has exactly one evidence state (credited, guessed or untracked), and the owner can see the coverage |
| P4 | Fewer conversations go unanswered or forgotten (the Pilot Command Center's starting point compared with now) |
| P5 | Posts can be compared by paid revenue and chats, not only by likes |
| P6 | *Later:* recommendations are acted on, and their outcomes are measured honestly |

## 5. Core workflow

The six problems form **one connected workflow**, not six modules.

| Stage | What happens | Module |
|---|---|---|
| 1. Attract | Create and publish content; attract buyers | Content Studio |
| 2. Capture | Receive enquiries; identify the customer; keep the origin where evidence exists | Conversations (shared inbox) |
| 3. Manage opportunities | Recognise buyers; create leads; follow up; don't let anyone slip | Leads, Follow-ups, Business Health |
| 4. Convert | Create orders; track them; record payment the business has verified | Orders |
| 5. Measure | Connect content → conversations → leads → paid orders; report evidence quality | Revenue and growth analytics (post Results, Insights) |
| 6. Improve | Find weaknesses; form hypotheses; run experiments; evaluate | Future optimisation engine |

- **Forward flow:** a Facebook post leads to a WhatsApp chat, the chat to a lead, the lead to an order, and the order is marked paid. The post then shows that commercial contribution.
- **Reverse navigation:** when analytics shows a problem, the owner can click through to the conversations, leads, orders or posts behind the number.

## 6. Failure points: the seven transitions

These transitions are Akilent's **product-quality checkpoints**. A feature is valuable when it makes one of them more reliable, measurable or actionable.

| Transition | Possible failure | What Akilent must ensure |
|---|---|---|
| T1 Marketing → Conversation | The origin is lost | Keep the source and the evidence |
| T2 Conversation → Lead | A buyer is overlooked | Make spotting and creating leads reliable |
| T3 Lead → Follow-up | A customer is forgotten | Surface outstanding opportunities |
| T4 Lead → Order | Sales progress is invisible | Keep commercial records connected |
| T5 Order → Payment | Revenue is assumed, not verified | Record the real payment status |
| T6 Content → Revenue | Engagement is mistaken for value | Measure attributable commercial outcomes |
| T7 Analytics → Improvement | Data doesn't lead to decisions | *Later:* support evidence-based experiments |

**Current state** (read-only mapping of the repo, 2026-10-31):

| Transition | Works today | Broken or incomplete |
|---|---|---|
| T1 | Tracked WhatsApp `/go/` link with ref matching (`apps/publishing/links.py:98-105`, `apps/conversations/entry_points.py:31-62`); comment private reply → DM (`entry_points.py:65-91`); immutable `ConversationAttribution` provenance (`apps/conversations/models.py:1040-1182`); guesses credit nothing (CheckConstraint `attribution_guess_credits_nothing`, `models.py:1149-1161`) | **Click-to-WhatsApp ad `referral` isn't parsed** (no handling in `apps/whatsapp`), so ad chats are untracked. No IG/FB DM tracked link exists (`links.py:104` builds WhatsApp only), so cold DMs are untracked. Untracked chats leave no row (`apps/conversations/attribution.py:81`). A mistyped picker id silently becomes a guess (`attribution.py:55-65`) |
| T2 | `create_lead` records attribution once (`apps/crm/services.py:22-101`); manual and workflow paths share it | `Lead.source` is free text, not derived. The first conversation wins (later chats aren't credited), which isn't written down as a rule. No cross-channel contact dedupe (WhatsApp by phone; IG and FB create separate contacts). Contact merge (`apps/contacts/merge.py`) ignores attribution. Deal conversion re-reads the entry point instead of copying it (`attribution.py:92`) |
| T3 | `FollowUp` model (`models.py:862-966`); missed-chat detection (`apps/conversations/recovery.py:39-80`); due notifications (`apps/conversations/followups.py:255-349`); "Opportunities at risk" (`apps/conversations/reporting.py:784-900`) | **The follow-up UI is confusing** (about 12 names, counts that disagree, duplicate lists): this is the first build slice. No link from follow-up to conversion. Stale leads are judged by `created_at` (`reporting.py:814-817`). No lost reason on Lead or Deal. `rule_lead_followup_gap` is labelled as leads but counts conversations (`apps/insights/rules.py:77-120`) |
| T4 | `create_order` fixes attribution at creation, in one transaction (`apps/commerce/services.py:21-83`) | `currency` silently defaults to `"USD"` (`services.py:26`). An order without a conversation is untracked by design |
| T5 | `mark_order_paid` is locked and idempotent; `paid_at`, method and reference are recorded; unpaid can be reversed (`services.py:176-238`) | **No cancel or refund path**: REFUNDED and CANCELLED are unreachable. **The revenue definitions disagree**: `reporting.revenue` (`reporting.py:510-519`), `journey()` and `results_for` (lifetime, no window) all differ, and `journey` counts orders by `created_at` but revenue by `paid_at` (`apps/publishing/results.py:171-222`) |
| T6 | Per-variant commercial results (`results.py:28-85`); engagement card with freshness provenance (`templates/publishing/_metric_card.html`) | No ranking of posts by revenue or chats (`journey` shows its top 5 by chats only). Pillar (`ContentPlanItem.pillar` via `ContentPost.plan_item`) is never grouped. Engagement is never shown beside revenue. **Deleting a post cascades its entry points and clicks** (`models.py:996-1000`, `apps/publishing/models.py:601-603, 632-634`), so credited revenue moves silently to untracked |
| T7 | Deterministic insight rules with per-recommendation outcome signals (`apps/insights/actions.py:80-110`); some drill-downs (journey leads and orders, at-risk links) | Dead ends: journey clicks and chats (`apps/conversations/insights_views.py:119-120`), the whole `_sales.html` funnel, the post Results card. No coverage %. No overall recommendation→outcome rate; "acted on" counts clicks |

## 7. Capability map

Status key: **Live** = built and in use · **Partial** = built but incomplete (see §6) · **Missing** = not built.

| Problem | Live | Partial | Missing |
|---|---|---|---|
| P1 | WhatsApp tracked link; comment reply → DM; provenance | IG/FB origin (comment path only) | Click-to-WhatsApp ad referrals; IG/FB DM links |
| P2 | Lead and order from a conversation; marked paid; attribution fixed at creation | Cross-channel identity | Cancel and refund |
| P3 | Explicit-only credit; credited/guessed/untracked buckets per currency; lower-bound copy | History after post deletion; one revenue definition | Coverage %; untracked rows |
| P4 | Follow-ups; missed-chat detection; at-risk panel; due notifications | Follow-up UX (redesign approved); stale-lead rule | Follow-up → conversion; lost reasons |
| P5 | Per-post commercial results; engagement metrics | — | Ranking by outcome; theme grouping; engagement beside revenue |
| P6 | Rule-based insights with outcome signals | — | Experiments; aggregate outcome rates |

## 8. Acceptance criteria

Every transition has **two independent forms of acceptance**. Technically correct isn't automatically good.

- **Data acceptance:** is it recorded correctly, are the relationships between records preserved, are the results accurate, is the history kept?
- **UX acceptance:** can an ordinary business owner understand what happened? Is it clear why it appears? Is the next action obvious? Can it be done on a 360px phone? Is it clear what the customer can and can't see?

| Transition | Data acceptance | UX acceptance |
|---|---|---|
| T1 | Every conversation with evidence keeps its origin (link, comment reply, and later ad referral). Untracked stays untracked. A mistyped picker id is refused, not guessed | The conversation shows where it came from in plain words ("From your post on 3 Oct"), or "We don't know where this came from" |
| T2 | A lead keeps its conversation and attribution. The first-conversation rule is written down | Making a lead is one obvious action from the chat. The owner can see why someone is a lead |
| T3 | Every open follow-up is counted once, the same way everywhere. Cancelled ones keep their history. *Later:* follow-up → paid is measurable | Follow-ups contract (`docs/design/followups-interaction-contract.md`): one list, why it's there, one next action, internal-only promise, 360px |
| T4 | An order keeps its lead, conversation and attribution. Currency is always explicit | Creating an order from a chat is obvious; the link back to the chat is visible |
| T5 | Only paid orders count as revenue. One revenue definition with one time basis across post, journey and sales reports. *Later:* cancel and refund leave an audit trail | "Mark as paid" says the business received the money, and that Akilent doesn't take payments |
| T6 | Deleting or archiving a post never changes historical credited revenue. Posts can be ranked by paid revenue and chats | Engagement and revenue sit side by side, clearly labelled. Lower bounds are explained |
| T7 | Every Insights number can be traced to its records. Coverage % is shown | Every number links to its records. Recommendations explain why, and how sure they are |

The existing extension point is `apps/publishing/tests/test_journey_acceptance.py`. It covers post → paid order with negative controls. It doesn't yet cover IG/FB DMs, ad referrals, post deletion, refunds or contact dedupe.

## 9. Prioritisation

**Eight principles:**
1. **One connected customer journey.** Marketing, conversations, leads and orders never behave like separate systems.
2. **Conversation-first.** Conversations are the heart of the sale, not messages to be stored.
3. **Business-verified outcomes.** An enquiry isn't a sale; an order isn't revenue until payment is verified.
4. **Honest attribution.** Missing evidence stays missing; never manufacture certainty.
5. **Actionable visibility.** Showing a problem helps the owner see where to look or act.
6. **Human control.** The business controls customer interactions, commercial records and consequential actions.
7. **Evidence before intelligence.** Recommendations and automation are earned through trustworthy measurement.
8. **Self-explanatory for anyone.** Plain words. Every screen says *why* something is shown and gives *one obvious next action*. No internal jargon (policy, source, attribution, SLA). Works on a 360px phone. Always says what the customer will and won't see.

**Workflow priorities** (strategic order, not authorisation):

| Priority | Workflow | Why it matters |
|---|---|---|
| 1 | Conversation-to-sale tracking | Establishes commercial outcomes |
| 2 | Marketing-to-conversation attribution | Establishes where customers come from |
| 3 | Opportunity and follow-up management | Reduces lost sales. **First build slice: follow-up redesign** |
| 4 | Content-to-commercial performance | Shows which marketing works |
| 5 | Revenue and growth analytics | Makes measurement trustworthy |
| 6 | Evidence-based optimisation | Improves performance |

**Feature tests:**
1. Does it answer at least one of these?
   - Does it help businesses attract potential customers?
   - Does it strengthen the link between marketing and conversations?
   - Does it help turn conversations into recorded sales?
   - Does it prevent lost opportunities?
   - Does it improve the quality of commercial evidence?
   - Does it help businesses understand or improve their results?
2. Does it **strengthen an existing workflow**, or add another disconnected one? The first is preferred.

**Rules:**
- Strengthen and verify existing capabilities before rebuilding them.
- Each build needs its own approved plan (the 2026-10-31 freeze exemption, `global-positioning.md` §13).
- Documenting an issue doesn't authorise implementing it.

**UX audit backlog** (recorded here; **not** part of the first slice):
- The inbox has 7 or more tabs, and the Missed and Needs attention tabs overlap.
- Two pages are titled "What needs your attention" (the dashboard and Business Health).
- Business Health numbers are dead ends (§6, T7).
- Lead detail has no next step beyond follow-ups.
- No lost-lead reasons.
- No follow-up → conversion tracking.
- The automation "Whose list" field is free text (fixed only inside the follow-up step, Stage 6).

## 10. Validation requirements

**Questions for real businesses** (pilots first):
1. Can retailers say where their enquiries come from?
2. Do they record paid orders consistently?
3. Do they know which posts bring valuable conversations?
4. Do they trust a lower-bound, evidence-based report?
5. Would they change their marketing because of it?
6. Would they pay for measurement **on its own**, not just as part of an inbox?

**Evidence per problem:**

| Problem | What to learn | Data source |
|---|---|---|
| P1 | Share of chats with a known origin | `journey()` `untracked_chats`; `manage.py journey_evidence` |
| P2 | Share of paid orders linked to a conversation | Orders; `journey_evidence` |
| P3 | Whether owners read lower bounds correctly | Pilot interviews |
| P4 | Unanswered and forgotten trend; follow-up completion. Metrics, baseline, pilot and gates: [Follow-ups outcome validation](followups-outcome-validation.md) | Pilot Command Center (`/manage/pilot/`, `pilot_report`); Follow-ups |
| P5 | Whether ranking by outcome changes what owners post | Pilot interviews; post results |
| P6 | Whether recommendations are acted on and help | `RecommendationLog.outcome_summary` (`apps/insights/models.py:144`) |
