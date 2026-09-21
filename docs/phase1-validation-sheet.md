# Phase 1 validation sheet — one per pilot business

Purpose: find out whether the truthful-conversation inbox (needs-attention / missed, waiting age,
delivery status) becomes part of how a real business works, and whether the measurements expose a
real problem. **No numeric targets.** Phase 2 is not approved by default; this sheet feeds the
decision gate.

Evidence levels (record which one each finding reaches):
**Capability** (Akilent measures and surfaces it) → **Product outcome** (people change what they do) →
**Business outcome** (response / recovery / conversion improves). Faster replies are *not* a pass
criterion on their own: one agent facing 500 enquiries is a staffing problem, not an inbox failure.

## 0. Before the pilot starts (once per business)

| Check | Done |
|---|---|
| Real business number, display name **approved** (Graph `name_status` not pending/declined) | ☐ |
| Number connected in Akilent; a test message from a phone that has messaged **this number** gets a delivered reply | ☐ |
| Baseline taken **before** they use the inbox: `baseline_conversations --account <slug> --git-commit <deployed HEAD> --out /app/baselines/<slug>-<date>.json --operator <name>`; sha256 + baseline_id recorded; files copied out of the container | ☐ |
| Business told what is being observed and agrees | ☐ |
| Start date, agents who will use it, expected enquiries/day noted | ☐ |

Do not start a pilot on a Meta test number. A baseline with zero delivered replies is a record, not a "before".

## 1. Usage — do they use it?  (check at day 3, day 7, day 14)

- Agents who opened **Needs attention** at least once today: ___ of ___
- Times the **Missed** tab was opened this week: ___
- Replies sent from the inbox vs. from the phone / WhatsApp Business app: ___ / ___ (ask; Akilent can't see the latter)
- Conversations they closed explicitly: ___
- Anything they stopped using, and why: ___

## 2. Behavior — does it change what they do?  (ask, then watch one working session)

- Can they tell **who is waiting** without leaving Akilent? ☐ yes ☐ sometimes ☐ no
- Do they use waiting age to decide what to answer first? ___
- Did the **Missed** tab surface conversations they had forgotten? Count ___; did they follow up? ___
- Did any state look **wrong** to them (waiting when they'd replied, closed when open)? Write the conversation id: ___
- What do they still do **outside** Akilent to keep track (notes, spreadsheet, memory, phone)? ___
- Was a failed send ever unclear to them? What did they think happened? ___

## 3. Outcome — what is technically measurable  (after 2 weeks, never before)

- Re-run `baseline_conversations` for the account into a **new** file; record commit, hash, id.
- Compare to the "before" file: enquiries, answered / unanswered / indeterminate, response buckets,
  `outbound_failed`. State `n`; medians appear only at n ≥ 5. Different commits between the two runs
  must be noted.
- `conversation_quality --account <slug>`: count of indeterminate / missing-outbound / invalid-ordering.
- Conversations that were missed or waiting long **and** were later answered via Akilent: ___
- Leads / deals / orders after enquiries: descriptive only, **not attribution**.

Caveats to write next to every number: calendar time (no business hours); no author on `MessageLog`
so automated and human replies are indistinguishable; delivered ≠ seen; pilot size is tiny.

## 4. Failures and surprises (log as they happen)

| When (UTC) | What happened | Meta/Akilent error shown | Did the business understand it? |
|---|---|---|---|
| | | | |

A failure whose cause is only visible in the database or a raw webhook is evidence for the
delivery-failure-reason display; note it here rather than building it speculatively.

## 5. Decision gate (fill after all pilots)

| Question | Answer (with evidence level) |
|---|---|
| Did it become part of the workflow, i.e. still used at day 14 without prompting? | |
| Did the measurements expose a meaningful problem (missed / slow / failed enquiries)? | |
| Does waiting time change agent behavior? | |
| Which businesses benefit most, and what do they have in common? | |
| What still falls through the cracks? | |
| Is missing customer context a real, observed problem (would Phase 2 be justified)? | |
| What did they do outside Akilent to compensate? | |

Pass = the feature became part of the workflow **and** the measurements exposed a problem that
justifies continuing. Otherwise change the plan. Record: **decision**, **date**, **who decided**,
and **which evidence level each conclusion rests on**. Only then does Phase 2 get designed.
