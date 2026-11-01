# Follow-ups outcome validation

**Status:** draft for owner review (2026-10-31). This is a measurement and validation specification. It does not authorise building anything.

| | Status |
|---|---|
| This document | Approved to write; content pending owner review. The direction is approved at report level (2026-10-31). Formulas, source references and the Gate 2–3 thresholds are not yet approved |
| Committed copies | An early draft is in commit `d652392`. **No committed copy is owner-approved documentation**, and the Stage 2–7 code in commit `6543956` is not evidence of business outcomes |
| Measurement queries | Defined here; **not run** |
| Instrumentation | Gap register only (Appendix A); **not authorised** |
| Pilot | Design and proposed thresholds only; **no recruiting, no data collection** |

**Related:**
- [Problem–workflow framework](problem-workflow-framework.md): this document extends §8 (T3) and §10 (P4).
- [Follow-ups interaction contract](../design/followups-interaction-contract.md)
- [Implementation checklist](../design/followups-implementation-checklist.md)
- [Pilot metrics](../sales/pilot-metrics.md)
- [Messaging guide](../marketing/messaging.md)

---

## 0. Why this exists

The follow-up redesign (Stages 1–7) was built to a contract and checked by tests and a browser walkthrough. That evidence answers two questions:

- *Does the software behave as specified?*
- *Can a person complete the workflow?*

It does not answer the question the work was for:

> **Do businesses lose fewer customer opportunities because they can manage conversations and follow-ups more reliably?**

This document defines how that question will be answered: which numbers, calculated exactly how, from which records, compared against what, and which claims are allowed at each point. It is written to support a decision, and to be able to show that the redesign did **not** help.

---

## 1. The problem and the hypothesis

Follow-ups address framework problem **P4: opportunities are lost** (replies are late, chats go cold, follow-ups are forgotten), at transition **T3: Lead → Follow-up**.

Customer enquiries and sales opportunities are lost when business owners:

| # | Problem |
|---|---|
| F1 | Forget to reply to customers |
| F2 | Fail to follow up when they promised |
| F3 | Cannot see which conversations need attention |
| F4 | Lose track of who is responsible |
| F5 | Overlook follow-ups spread across chats and customer records |
| F6 | Cannot tell work that is due from work scheduled for later |

**Hypothesis.** With the redesigned experience, a business owner can reliably:
- identify which customers need attention;
- understand why;
- take the appropriate action;
- avoid losing opportunities to forgotten or unclear follow-ups.

**What would show the hypothesis is wrong** is set out in §8. It is decided now, before any data is seen.

---

## 2. Four kinds of evidence, never merged into one number

| Layer | Question | Example | Proves |
|---|---|---|---|
| **Product activity** | Is it used? | Follow-ups created; actions taken | Use only; not benefit |
| **Operational outcome** | Is the business handling enquiries and follow-ups better? | Overdue rate down; on-time rate up; fewer customers waiting over 24h | Behaviour change (Gate 3) |
| **Commercial outcome** | Are more opportunities reaching recorded sales? | Recovered conversations later linked to a paid order | Sequence, not cause (Gate 4) |
| **Customer value** | Is the improvement worth paying for? | Renewal, upgrade, stated willingness to pay with a price | Value (Gate 4) |

"500 follow-ups created" is activity. "Overdue rate fell from 40% to 15%" is an operational outcome. "Recovered conversations led to 6 paid orders" is a commercial sequence. "The owner renewed and named this as the reason" is customer value. A report never presents one layer as evidence for another.

**Positive feedback is not commercial value.** An owner liking the feature is recorded as interview evidence. Only willingness to pay (§7.4) counts toward commercial value.

---

## 3. Measurement rules (apply to every metric)

### 3.1 Every metric has a type

| Type | Meaning | Can fall because… |
|---|---|---|
| **Cohort** | Follows a defined set of items (e.g. those that fell due in a period) to see what happened to them | …the business handled that work better |
| **Snapshot** | A count of what is true at one moment | …items were completed, **or cancelled, or rescheduled**. A falling snapshot alone is not improvement |
| **Session** | Observed in a moderated task session | — |
| **Interview / commercial** | What owners say, or what they pay | — |

Cohort and snapshot metrics with similar names are **not interchangeable**. For example, *overdue rate* (cohort) and *overdue now* (snapshot) are separate rows in §4 and are reported separately.

### 3.2 Counting unit

- **Follow-up performance** (on-time, overdue, completion) counts **follow-ups**.
- **Customer-level visibility** (waiting, needs attention, the shared count) counts **distinct customers** (`FollowUp.contact_id`), the same unit as the product's shared count (`apps/conversations/followups.py:351` `due_customers`).

The two units are never mixed in one figure. Mixing them is the counting problem the redesign removed.

### 3.3 Inclusion and exclusion rules (decided before any formula)

| Situation | Rule |
|---|---|
| **Cancelled** (`closed_reason="cancelled"`, "Not needed") | Excluded from completion and on-time denominators. Reported separately as the **cancellation rate** |
| **Legacy blank `closed_reason`** (closed before reasons were recorded; `models.py:903`) | Kept in its own "unknown outcome" bucket, outside completion and on-time, until its meaning is established. The existing `performance.followup_completion()` counts blanks as handled (`apps/conversations/performance.py:46-54`), so this spec's figures will differ from it by design |
| **Source** (`models.py:871-875`) | Reported in three groups, never pooled: **manual** (set by a person), **missed** (detected waiting chats), **automation** (`policy` + `workflow`) |
| **Reopened** | Judged by its **latest** state. `reopen()` erases the previous close (`followups.py:267`), so earlier closure history is unavailable; this is disclosed wherever reopened items are included |
| **Snoozed / rescheduled** | Judged against the **current** `due_at`. The original due time is overwritten (`followups.py:216-245`) and unavailable. See §3.4 |
| **Several follow-ups for one customer** | Each counts in follow-up metrics; the customer counts once in customer metrics (§3.2) |
| **Assignment** | `assigned_to` null means "whole team" (`models.py:917`). It is not "unowned" in the product, but it is reported separately because no single person is accountable |
| **Accounts** | Active accounts only; staff/demo/test accounts excluded |

### 3.4 The on-time rate is approximate (read before using it)

> **⚠ Approximate.** Rescheduling overwrites `due_at` and the original is not kept. An item rescheduled five times and then done is "on time" against its last date. **The on-time rate cannot show whether the original commitment was met.** It biases results upward, and more so the more a business reschedules.

Wherever the on-time rate appears, it is shown with:
- the share of completed follow-ups rescheduled at least once (`snoozed_count ≥ 1`);
- the median `snoozed_count`.

A rising on-time rate with a rising reschedule share is **not** evidence of improvement (see disconfirming outcome D2, §8).

This spec uses **no grace period**: on time means `done_at ≤ due_at`. The existing helper allows one hour (`performance.py:57`); figures will differ from it.

### 3.5 Time and periods

- **Time-zone rule:** all "due", "today" and period boundaries use the account's business time zone, which is the time zone set in its opening hours.
  - The follow-up and reporting helpers both read it: `followups.business_zone` (`followups.py:37`) and `reporting.business_zone` (`reporting.py:283`).
  - **Fallback:** an account with no opening-hours time zone uses **UTC**. Both helpers resolve to UTC (`apps/accounts/business_hours.py:26`, `automator/settings.py:203`), so they agree.
  - A business whose figures were computed on the UTC fallback is flagged in its report, because its day and week boundaries may not match its local day.
- Periods are whole weeks (Monday–Sunday, business time).
- A cohort is reported only after its **observation window** has closed: due date + 7 days. That gives every item the same chance to be handled.

### 3.6 Volume normalisation

Enquiry volume changes from week to week. A quieter week has fewer overdue items without anyone working better. Therefore:

- Every rate states its denominator (§4).
- Every report also shows **enquiry volume**: conversations customers started in the period (`reporting.first_replies`, `reporting.py:93`), and follow-ups that fell due.
- Businesses are **never compared with each other on raw counts**. Comparisons are within one business, before vs after, using rates.
- A week with fewer than **10 enquiries** or fewer than **5 follow-ups due** is marked *low volume* and is not used for conclusions (see D4, §8).

---

## 4. Metrics

Provenance grades:
- **Now:** computable from records that exist today.
- **Approx:** computable, with a stated bias.
- **Gap:** needs instrumentation; see Appendix A, not authorised.

### 4.1 Operational metrics

| ID | Metric | Problem | Type | Unit | Definition | Grade | Records |
|---|---|---|---|---|---|---|---|
| M1 | **On-time follow-up rate** ⚠ | F2 | Cohort | follow-ups | Completed by `done_at ≤ due_at` ÷ completed. Cohort: fell due in period, not cancelled, known outcome, by source group | **Approx** (§3.4) | `FollowUp.due_at, done_at, closed_reason, snoozed_count` |
| M2 | **Completion rate** | F2 | Cohort | follow-ups | Closed `done` or `staff_replied` within the observation window ÷ fell due in period (cancelled and unknown excluded) | Now | same |
| M3 | **Overdue rate** | F2 | Cohort | follow-ups | Still open, or closed **after** `due_at + 24h`, at window close ÷ fell due in period (cancelled excluded) | Now (current `due_at`, §3.4) | same |
| M4 | **Cancellation rate** | F2 | Cohort | follow-ups | Closed `cancelled` ÷ fell due in period | Now | same |
| M5 | **Reschedule share** | F2 | Cohort | follow-ups | `snoozed_count ≥ 1` ÷ fell due in period; plus median `snoozed_count` | Now (count only; no dates) | `FollowUp.snoozed_count` |
| M6 | **Overdue now** | F2, F6 | Snapshot | follow-ups | Open with `due_at < now` | Now | `FollowUp` |
| M7 | **Seriously overdue now** | F2 | Snapshot | follow-ups | Open with `due_at < now − 24h` (current due time) | Now | `FollowUp` |
| M8 | **Customers waiting over 24h** | F1, F3 | Snapshot | customers | Distinct customers with an open `missed` follow-up | Now | `FollowUp(source=missed)`; created hourly for chats unanswered 24h, within 7 days (`apps/conversations/recovery.py:39-77`, `state.py:55`) |
| M9 | **Missed-chat rate** | F1 | Cohort | conversations | Conversations given a `missed` follow-up in period ÷ conversations customers started in period | Now | `FollowUp(source=missed).created_at`; `reporting.first_replies` |
| M10 | **Median first-response time** | F1 | Cohort | conversations | Median time from first customer message to first sent reply, for conversations started in period. Also the share answered within 24h | Now (derived from messages) | `reporting.response` (`reporting.py:150`) |
| M11 | **Ownership: due work for one person** | F4 | Cohort | follow-ups | Fell due in period with `assigned_to` set ÷ fell due in period. Reported next to the "whole team" share | Now (at close time only; reassignment history is a gap) | `FollowUp.assigned_to` |
| M12 | **Ownership: assignee changes** | F4 | Cohort | follow-ups | Follow-ups reassigned at least once | **Gap** (A3) | none |
| M13 | **Follow-ups set away from a chat** | F5 | Activity | follow-ups | Follow-ups with no conversation, or created from contact/lead pages | Approx (no-conversation only; the page of origin isn't stored) | `FollowUp.conversation` null |

M1–M5 are reported together, never singly. Together they show whether improvement is real (more done on time) or cosmetic (more rescheduled or cancelled).

### 4.2 Recovery: three stages, each with its own denominator

The journey is *missed → staff replied → lead → paid order*. Each step is a separate rate, reported separately:

**Time anchors.** Each conversation has two fixed points:
- **Missed point (M):** the `created_at` of the conversation's **earliest** `missed` follow-up created in the period.
  - It is about 24 hours after the customer's unanswered message, because of the hourly job's 24h rule.
  - A conversation counts once per period, even if it was missed again later in the same period.
- **Recovery point (P):** the timestamp of the **first** reply sent at or after M (outbound message with sent, delivered or read status), if it falls within 7 days of M.

| ID | Stage | Numerator | Denominator | Window | Unit |
|---|---|---|---|---|---|
| R1 | **Reply recovery** | Conversations with a recovery point | Conversations with a missed point in the period | P must fall in **[M, M + 7 days]** | conversations |
| R2 | **Lead progression** | Recovered conversations with at least one lead created in the window | Recovered conversations (R1 numerator) | Lead `created_at` in **[M, P + 30 days]** | conversations |
| R3 | **Paid progression** | Recovered conversations with at least one order marked paid in the window | Recovered conversations (R1 numerator) | Order `status=paid` and `paid_at` in **[M, P + 30 days]** | conversations |

**Window rules:**
- **Distinct conversations.** A conversation with two leads or three paid orders counts once in R2 or R3.
- **Lead and order windows start at M, not P.** A team often records the lead while replying, a moment before the reply is sent. Starting at P would wrongly exclude those.
  - Anything recorded **before M** belongs to earlier activity and is not counted.
- **The windows overlap and are cumulative, not exclusive.** R2 and R3 share the same window, so a conversation can count in both. R3 does not require R2: an order can be recorded without a lead.
- **Incomplete windows are excluded, never counted as failures.** A period's figures are reported only when every conversation in it has had its full window:
  - R1 when the period end + 7 days has passed;
  - R2 and R3 when the period end + 37 days has passed (the latest possible P plus 30 days).

  Until then the figure is shown as **"pending (window open)"**, not as a lower rate. Otherwise recent conversations would look worse only because they haven't had time to progress.

**Records:**
- Missed follow-ups: `FollowUp(source=missed)`.
- Replies: outbound `Message` with sent, delivered or read status.
- Leads: `Lead.conversation`, `Lead.created_at` (`apps/crm/models.py:137, 156`).
- Orders: `Order.conversation`, `status`, `paid_at`, `currency` (`apps/commerce/models.py:73-86`).

**Grade:** Approx.

**Limitations (stated with every recovery figure):**
- The only link is the **shared conversation and time order**. There is no record linking a follow-up to a lead or order (contract §14). Follow-ups set from a contact or lead with no chat can't be followed into sales at all.
- `reporting.recovery()` (`apps/conversations/reporting.py:210-280`) already counts missed, recovered, leads and paid orders. But it counts lead and order **records**, not conversations, and has no time cap. It is **not** used as-is for R2/R3: a conversation with two orders would count twice.
- An order can be un-marked as paid later (`mark_order_unpaid`, `apps/commerce/services.py:215`). Figures use the order's state at report time.
- Revenue is reported **per currency** and never summed across currencies. `Order.currency` defaults to "USD" (`apps/commerce/models.py:73`), so a business that never set it may show USD for local money. This is flagged per business.
- Manual follow-ups (not `missed`) can be traced to sales only through the same conversation-and-time rule, as an exploratory figure.

> **A paid order after a recovered conversation is evidence of sequence, not proof that the follow-up caused the sale.** The customer might have bought anyway. Unlinked outcomes stay unattributed. This matches the attribution rule in the messaging guide (`docs/marketing/messaging.md`: explicit evidence only).

### 4.3 Usability (session metrics)

Collected in moderated sessions (§6). Recorded per task:
- completion (unaided / with help / failed);
- time to complete;
- errors;
- help requests;
- whether the owner explained the screen correctly without prompting.

**Visibility time** (owner opens Akilent → acts on the right customer) is measured here. It is **not** measured from product telemetry, which doesn't exist (gap A4).

---

## 5. Baseline

**Baseline boundary.** The baseline is the **4 complete weeks (business time) immediately before the redesigned workflow is deployed to production**. The boundary is the actual production deploy time, recorded when it happens. It is **not**:
- the date of commit `6543956` (the Stage 2–7 code);
- a staging deployment;
- the first draft commit of this document (`d652392`).

As of 2026-10-31 the redesign is **not deployed**, so the boundary is not yet fixed.

**Baseline status, one per business per metric.** These are different situations and are never merged:

| Status | Meaning |
|---|---|
| **Not yet calculated** | The records exist; the authorised queries haven't been run. This is every metric today |
| **Calculated** | Computed with the §4 formulas over the 4 baseline weeks |
| **Partial** | Some baseline weeks are *low volume* (§3.6) or fall before the business joined. Computed over the usable weeks, with the count stated |
| **No historical baseline available** | The records needed don't exist for that period. Examples: the business joined less than 4 weeks before the deploy; a metric is graded **Gap** (M12); or a business hasn't recorded leads or orders (R2/R3). That business is judged on interview evidence and during-pilot trends only |

| Baseline element | Source | When |
|---|---|---|
| M1–M11 and R1–R3 for the 4 baseline weeks | Existing production records, same formulas as §4 | Computed only when separately authorised; queries are defined here, not run. R2/R3 baseline figures need 37 days after the last baseline week (§4.2) |
| Current practice, known misses, who handles chats, how they track promises | Pre-pilot interview (§7.2) | Before the business uses the redesign |
| Session tasks on the **old** experience (optional, if a staging copy is kept) | Moderated session | Before deploy, only if authorised |

**Comparability:**
- Baseline figures carry the same "Approx" biases as later figures (overwritten due times, latest-state-only), so the before/after comparison is like for like.
- They are never presented as exact.

**The existing pilot baseline is not sufficient.** `PilotEnrollment` stores average first response and **lifetime** lead and conversation counts at enrollment (`apps/accounts/models.py:373-396`, `apps/accounts/pilot.py:91-130`). It holds no follow-up metrics, and its counts are cumulative, not per period. It may support M10 context, but it can't serve as the follow-up baseline.

**Other influences.** A business may change staff, run a promotion, or enter a seasonal peak during the pilot. The weekly report records any such event the owner mentions. Results from affected weeks are interpreted with that noted.

---

## 6. Usability evaluation

**Format:** a 30-minute moderated session per business at the start of the pilot and again at the end. The session is held on the owner's own workspace, on their phone (360px-class screen) if that's how they work.

The facilitator says what the task is, never how to do it. Help is given only after 2 minutes stuck, and is recorded.

| # | Task (as said to the owner) | Success means |
|---|---|---|
| U1 | **"You've been away all morning. Which customers need you now?"** (headline) | Names the Waiting customers first, then Due, without help |
| U2 | "Why is this customer on the list?" | States the reason and how urgent it is |
| U3 | "Open the conversation for this one." | Reaches the right chat |
| U4 | "Who should deal with this? Make it theirs." | Uses Give to… and names the person |
| U5 | "This can wait until next week." | Uses Later and can say when it will come back |
| U6 | "You've dealt with this one" / "This one isn't needed any more." | Chooses Done vs Not needed correctly |
| U7 | "From this customer's page, set a follow-up for Friday." | Does it from the contact or lead page without hunting for the chat |
| U8 | "When you set that, did the customer get a message?" | Answers "no, only the team sees it" unprompted |

**Gate 2 thresholds:**

| Threshold | Status |
|---|---|
| **U1** (headline task) unaided by **≥ 80%** of owners | **Approved as provisional** (2026-10-31) |
| **U8** (customer isn't messaged) correct for **100%** of owners | **Approved as a safety/comprehension gate** (2026-10-31) |
| Every other task unaided by **≥ 70%** | Proposed; for owner approval |
| Median U1 time under **60 seconds** | Proposed; for owner approval |

- **Results are always shown as numerator / denominator** as well as a percentage, for example "U1: 4/5 owners (80%)".
  - With 5–10 businesses, a percentage is a pilot signal, **not a statistically reliable estimate** for businesses in general.
  - One owner moves the figure by 10–20 points.
- **U8 is never averaged.** It is a trust and safety question, not a usability score. Any single owner who believes a follow-up messages the customer triggers:
  - an investigation of what on the screen led to that belief;
  - a fix to the wording or layout, if one is needed;
  - a re-test of U8 with that owner (and, after a fix, with every owner).

  Gate 2 stays unmet until U8 is 100% on the re-test. Other owners' correct answers don't offset one misunderstanding.

**Questions after the session:**
- What did you do before to keep track?
- What still gets missed?
- How would you feel if this disappeared? (very disappointed / somewhat / not)

The answers are recorded verbatim.

---

## 7. Pilot design

### 7.1 Size and purpose
**5–10 businesses for 2–4 weeks.** That is enough to discover usability problems and behaviour patterns.

**It is not a statistically representative sample.** No market-wide claim, percentage improvement "for businesses", or significance test is made from it. Results are reported business by business.

### 7.2 Selection (the owner selects; candidates may come from the existing pilot waves)
- Regular enquiries through WhatsApp, Instagram or Facebook (≥ 10 a week; §3.6).
- More than one person handling conversations, where possible, so ownership (F4) can be observed.
- Already experiences missed replies or follow-ups (from the interview or baseline M8/M9).
- Willing to record leads, orders and paid outcomes honestly in Akilent. Without this, R2/R3 measure record-keeping, not sales.

Selection is noted per business so that results aren't generalised beyond businesses like these.

### 7.3 Collection points

| Point | What |
|---|---|
| **Before** | Baseline (§5), pre-pilot interview, session 1 |
| **During** (weekly) | M1–M11 and R1 for the week; a 5-minute check-in: what got missed, what was confusing; volume and any unusual event |
| **After** | Final week's metrics; R2/R3 once their windows close (37 days after the last pilot week, §4.2); session 2; exit interview; willingness-to-pay question |

### 7.4 Customer value: kept separate from liking
- "Would you be disappointed?" is **perceived value** (interview evidence).
- **Willingness to pay** is asked with a concrete figure ("Would you pay $X a month more for this?"), or observed as an upgrade or renewal.
- Only that second kind counts toward Gate 4.

---

## 8. What would show the hypothesis is wrong (decided in advance)

| ID | Disconfirming outcome | How it shows up | What it would mean | What it would trigger |
|---|---|---|---|---|
| D1 | **Owners understand it but don't use it** | Gate 2 met; follow-ups set per enquiry flat; M8 unchanged | The problem isn't the interface | Rethink the workflow (e.g. where follow-ups surface), not more UI polish |
| D2 | **Follow-ups are routinely postponed** | M5 reschedule share rises; M1 rises while M2 doesn't | "Later" is used to make work disappear | Rethink Later (limits, prompts); treat M1 as unreliable |
| D3 | **Overdue improves while response gets worse** | M3 down, M10 up | Attention moved to follow-ups at the expense of new enquiries | Rebalance what the page prioritises |
| D4 | **Too few enquiries to judge** | Most weeks *low volume* (§3.6) | The segment doesn't feel this problem | Change target segment before concluding anything |
| D5 | **Liked but not paid for** | "Very disappointed", yet no willingness to pay | It's a nice-to-have | Question the value proposition; don't make commercial claims |
| D6 | **More admin, no recovery** | Follow-ups set rises; R1 flat; owners report extra work | The workflow costs more than it returns | Simplify or automate creation; reconsider the feature's role |
| D7 | **Cancellations replace completions** | M4 rises as M3 falls | Work is being dismissed, not done | Investigate why items are "not needed" |

Any of these is a valid finding, not a failed pilot. They tell us whether to improve the experience, rethink the workflow, change the segment, or question the value proposition.

---

## 9. Acceptance gates

| Gate | Evidence | Status |
|---|---|---|
| **1. Technical reliability** | Stage 7: full suite, browser walkthrough, counts, permissions (`docs/design/followups-implementation-checklist.md`, Stage 7 entry) | **Stage 7 evidence reported; pending independent review and owner acceptance.** This is the implementation agent's report and isn't independently verified |
| **2. Usability** | §6 thresholds met in session 2 | Pending pilot |
| **3. Behaviour change** | Per business, vs its own baseline (§5), rates not counts. Most pilot businesses (with enough volume) show M3 lower and M2 higher, **without** D2, D3 or D7. **Provisional:** the exact metric thresholds and comparison method are still to be reviewed by the owner | Pending pilot |
| **4. Commercial value** | R1–R3 sequence evidence over a longer period, **and** willingness-to-pay evidence (§7.4) | Pending longer-term evidence |

Rules:
- Gates are passed in order.
- **Gates 1–3** must be passed before claiming the redesign helps businesses handle enquiries and follow-ups more reliably.
- **Gate 4** must be passed before any claim about recovered sales or revenue.
- Gate 2 has two approved thresholds (U1 provisional, U8 safety gate) and two still proposed (§6).
- **Gate 3 is provisional** until its exact thresholds and comparison method are approved.

---

## 10. What may be said, and when

All wording follows the [messaging guide](../marketing/messaging.md). Nothing here changes it.

| Point reached | Allowed | Not allowed |
|---|---|---|
| Now (Gate 1 reported) | "The Follow-ups page brings waiting customers, due follow-ups and later ones into one list." (describes the product) | Any claim about outcomes |
| Gate 2 | "In testing, owners could find who needed attention without help." (with the number of owners) | "Businesses respond faster" |
| Gate 3 | Per-business results with their base: "At [business], overdue follow-ups fell from X% to Y% of those due, over N weeks." | Pooled percentages across businesses; "businesses lose fewer customers" |
| Gate 4 | "After recovering missed chats, [business] recorded N paid orders from those conversations." | "Follow-ups made $X"; any causal wording ("led to", "drove", "won back") |

Always:
- Illustrative numbers are labelled **illustrative**.
- Every figure states its period, its denominator, and whether it is approximate.
- Unattributed outcomes are left unattributed.

---

## 11. Reader's checklist

| Question | Answered in |
|---|---|
| What business problem are we testing? | §1 |
| What would improvement look like? | §4, §9 (Gate 3) |
| How exactly will we calculate it? | §3, §4 |
| Which existing records support the calculation? | §4 (Records column), §5 |
| What cannot currently be measured reliably? | §3.4, §4 (Approx and Gap), Appendix A |
| What evidence would make us conclude the hypothesis is wrong? | §8 |
| What claims are we allowed to make at each stage? | §10 |

---

## Appendix A: instrumentation gap register (not authorised)

Each item is the smallest change that would close a gap. **None is authorised.** Each needs a separate decision.

| ID | Gap | Why it matters | Smallest change | Status |
|---|---|---|---|---|
| A1 | **No durable record of follow-up actions** (created, done, later, not needed, give to, edit) | Can't measure behaviour over time, who acted, or how often Later is used. Today only a realtime notice is sent (`followups.py:555` `_announce`) | Write an `Event` row per action, using the existing `Event` model (`apps/conversations/models.py:783`) and `emit_event` (`apps/conversations/services.py:27`) | Not authorised |
| A2 | **Original due time overwritten** | M1 can't show whether the original commitment was met (§3.4) | Keep the first due time when a follow-up is created (field or A1 event payload) | Not authorised |
| A3 | **No reassignment history** | M12; ownership change over time | Covered by A1 (give-to events) | Not authorised |
| A4 | **No page-to-action timing** | Visibility (F3) only measurable in sessions | Record "opened Follow-ups" and "first action" times (e.g. the existing `UxEvent`, `apps/logs/models.py:260`) | Not authorised |
| A5 | **No follow-up → lead/order link** | R2/R3 rely on shared conversation and timing; follow-ups without a chat can't be traced | Already out of scope in the contract (§14) | Not authorised |
| A6 | **Reopen erases the previous close** | Closure history lost | Covered by A1 | Not authorised |
| A7 | **Origin page not stored** (chat, contact, lead, page) | M13 is approximate | A1 event payload | Not authorised |

## Appendix B: query definitions (not run)

Each metric in §4 is computed per account, per business-time week, with the rules in §3. When authorised, the queries:
- are read-only;
- run against a copy or a read replica;
- produce one row per business per week;
- include the denominators and the low-volume flag;
- produce no pooled figures.

Their output feeds the weekly pilot report (§7.3). They will be written and reviewed as their own step, with tests, once that step is authorised.
