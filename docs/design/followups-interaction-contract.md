# Follow-ups: interaction contract

UX specification · 31 October 2026 · Approved first build slice of `docs/strategy/problem-workflow-framework.md` (transition T3)

> **Authority:** this is the single source for follow-up behaviour, wording and layout. Developers don't invent other labels. Templates never define follow-up words themselves: labels and messages come from `apps/conversations/followup_copy.py` (Stage 2). If a decision isn't in this contract, it's settled here first, and only then built. The build is staged in `docs/design/followups-implementation-checklist.md`.
>
> **Status:** approved 2026-10-31 (Stage 1); amended after each stage's review. Recorded changes to the original contract are listed in §15.

---

## 1. Purpose

> **Follow-ups are the work your team needs to do to get back to a customer.**

A follow-up isn't a notification and isn't another place to monitor. A business owner opens Follow-ups and should see straight away:
- which customers need attention;
- why each one is there;
- what to do next.

## 2. What a row is (identity rule)

**One row per customer.**

- **Identity is `FollowUp.contact_id`.** The `contact` foreign key is required on the model (`apps/conversations/models.py`, `FollowUp.contact`, non-null), so every follow-up has one. Only as a defensive fallback, a follow-up without a contact is grouped by its conversation id. It is never grouped with anything else.
- Rows are **never** merged by name or phone number. An Instagram contact and a WhatsApp contact for the same person are two rows until someone merges the contacts.
- **Eligibility is separate from contents:**
  - **Mine:** a customer qualifies when at least one of their open follow-ups is assigned to me or is unassigned.
  - **Whole team:** a customer qualifies when they have any open follow-up.
  - Once a customer qualifies, the row shows **all** of their open follow-ups, across all their conversations, each with its real owner ("for you", "for Joseph", "for the team").
- The row's **section, why label, due time and main action** come from its **most urgent eligible** follow-up. Urgency order: Waiting for your reply, then Due (oldest first), then Coming up.
- Other open follow-ups for that customer stay reachable inside the row, for example "+1 more: Instagram chat, due tomorrow · for Joseph", each with its own link.
- A follow-up with no conversation (made from a lead or a contact) links to the contact page.

## 3. Sections

The sections don't overlap: each row sits in exactly one, decided by its most urgent eligible follow-up.

| Situation | Section |
|---|---|
| The customer has an open, due `missed` follow-up (they messaged; no reply recorded). If someone moved a missed follow-up **Later**, it waits in Coming up like any other | **Waiting for your reply** |
| A follow-up is due now or overdue, and the customer isn't already in Waiting | **Due** |
| Follow-ups scheduled for later only | **Coming up** |
| A missed and a manual follow-up exist together | **Waiting for your reply**, once |

- The second section is called **Due**, not "Due today", because it includes overdue items. Late items come first and say "2h late" in plain words.
- "Now" and "today" are worked out in the **account's business time zone**, the zone `followups.local_label` already uses.
- **`policy` follow-ups** (created by a Business Health automation) go under **Due**, labelled "From an automation", **not** under Waiting. (approved 2026-10-31)
  - Why: "Waiting for your reply" means exactly what the missed-chat job detects, a customer who messaged and got no reply. A policy follow-up's meaning depends on the rule that made it.
  - Their behaviour stays **unchanged**: a team reply still closes them (`followups.WAITING_SOURCES = ("missed", "policy")`).
- Every section has an empty state (`templates/components/empty_state.html`):

| Section | Empty-state copy |
|---|---|
| Waiting | "No customers waiting for a reply." |
| Due | "Nothing due. You're all caught up." |
| Coming up | "Nothing scheduled yet." |

## 4. The count

The count is one definition, one query: `followups.due_for(user, account)`. The nav badge, the dashboard tile and the Business Health "Opportunities at risk" panel all use it.

**One set, not just one number.** `followups.due_customers(account, user=…)` returns the customers; `due_for` and `due_for_team` only count them. They are exactly the customers in **Waiting for your reply** and **Due** on the page (Mine or Whole team), and a test compares the two sets of customers, not their totals.

- It counts **open** follow-ups: not done, not cancelled.
- Only follow-ups that are **due now or overdue**, in the account's time zone. Future ones are excluded.
- **Assigned to the user, or unassigned.**
- It counts **customers, not rows**. Two follow-ups for one customer count as one, and a Waiting customer counts once.
- The label always says whose count it is: "**3 follow-ups due for you**".
- The **Whole team** tab and any team figure use `due_for_team(account)`, and the label always says "for the whole team". The shared count never quietly changes meaning.

## 5. Main action

The main action **never completes anything**. Completing is always the separate **Done** button.

| Section | Main action | Result |
|---|---|---|
| Waiting for your reply | **Reply now** | Opens the right chat with the reply box focused (`?reply=1`) |
| Due | **Open chat** | Opens the relevant conversation, or the contact if there's no conversation |
| Coming up | **Open chat** | Same as Due |

## 6. Actions and what each one means

Each action has one meaning, everywhere:

| Action | Meaning | Stored as |
|---|---|---|
| **Done** | The work has been handled | `closed_reason="done"`, `done_by`, `done_at` |
| **Later ▾** | Reschedule: In 1 hour · Tomorrow morning · Next week · Pick a date | new `due_at`; `snoozed_count` + 1; `notified_at` cleared |
| **Not needed** | Cancelled on purpose. It does **not** claim the customer was contacted | `closed_reason="cancelled"`. **The row is kept**, never deleted |
| **Give to…** | Change who is responsible: a teammate or the whole team (team members only) | `assigned_to` |
| **Edit note** | Change the internal context | `note` (255 characters) |

"Not needed" and "Done" are never the same thing. This is how Akilent tells a handled opportunity apart from one that was deliberately dropped.

## 7. Every action says what happened

Messages come from `followup_copy.py`:

| Event | Message |
|---|---|
| Done | "Follow-up marked as done." |
| Later | "Follow-up moved to {Tomorrow at 09:00}." |
| Not needed | "Follow-up cancelled." |
| Give to… | "Follow-up assigned to {Joseph}." / "Follow-up assigned to the whole team." |
| Edit note | "Note updated." |
| Created | "Follow-up set for {Tomorrow at 09:00}." |
| An existing one was kept (`create()` returned `created=False`) | "This chat already has a follow-up on {date}." plus what became of the note given with it: "Your note was added to it." / "It already has that note." / "Your note wasn't added: a note holds 255 characters. Use Edit note to shorten it." (the last as a warning) |
| Action on an item that's already closed | "This follow-up was already done." |

**A new note is added, never dropped (owner decision, 2026-10-31).** When a chat already has an open follow-up of the same kind, that follow-up is kept (same row, same identity; no duplicate) and the note given with the new one goes through `followups.append_note`:

| Existing note | New note | Result |
|---|---|---|
| "Send the quote" | "Ask about size" | "Send the quote" + new line + "Ask about size" |
| "Send the quote" | blank | unchanged |
| empty | "Ask about size" | "Ask about size" |
| empty | blank | empty |
| contains the same line already | that line | unchanged ("It already has that note."); an automation repeating itself never grows the note |
| either | would exceed 255 characters together | unchanged, and the message says the note wasn't added (no silent cut) |

Notes show their line breaks everywhere (page row, side panel, customer card), Edit note is a two-line box so editing keeps them, and the due email joins lines with " / ".

There are no generic messages. The current "Follow-up scheduled." (shown even when an existing follow-up was kept) and the "Reminder …" messages are retired.

## 8. Why labels

| `source` | Label shown |
|---|---|
| `missed` | "**Waiting for your reply** · 26h" |
| `manual` | "**You set this**" / "**{name} set this**" |
| `workflow` | "**From an automation**" |
| `policy` | "**From an automation**" |
| AI suggestion chip (saved as `manual`) | "**Suggested follow-up**" on the chip before saving |

## 9. Notifications

Today's behaviour (`followups.notify_due`) is **unchanged** and written down here:
- A live in-app toast goes to the business's team: "N follow-ups due for you".
- An email goes **only to the assigned teammate**, and only if they have "Email me when follow-ups given to me are due" on. It is sent at most once an hour.
- The switch's messages say exactly that: on, "We'll email you when follow-ups given to you are due, at most once an hour. Whole-team follow-ups and customers waiting for a reply aren't emailed."; off, "Follow-up emails are off. Yours still show here, with the count beside Follow-ups in the menu." (The old label "when mine are due" was wrong: Mine includes whole-team follow-ups, which are never emailed.)
- The email: "These follow-ups given to you are due now:", one line per customer (note, or why it's there, due time, which chat), and the footer "Only your team gets these emails; customers never do."
- **Unassigned** follow-ups send **no email**. They appear in every teammate's Mine list and count.
- `missed` follow-ups send no follow-up email. The existing overdue alert already emails whoever is looking after that conversation.
- **Nothing is ever sent to the customer.**

## 10. Concurrency

- Every state change (Done, Later, Not needed, Give to…, Edit note) **locks the row** (`select_for_update` inside a transaction) and **re-checks that it's still open** before writing.
- Acting on a closed item changes nothing and shows "This follow-up was already done."
- Gap found on 2026-10-31: `followups.snooze()` neither locks nor checks whether the item is open, so a closed follow-up can be rescheduled. **Stage 2 fixes this.**
- Tests check the **final saved state**, not only the message. For example, Done and then Later arriving together leaves the follow-up closed, with its `due_at` unchanged.

## 11. The internal-only promise

> **Only your team sees this. Nothing is sent to the customer.**

It appears on **every surface where a follow-up is created or scheduled**:
- the Follow-ups page header;
- the composer's "Follow up tomorrow" button (as hint text);
- the side panel's set form;
- the lead and contact set forms;
- the automation step.

The help article and the email footer add: "Your team may get an email about it; the customer never does."

## 12. Layout

**Page header:** "Follow-ups". Sub-line: "Customers your team needs to get back to. Only your team sees this. Nothing is sent to the customer." Tabs: **Mine** · **Whole team**. The "Email me when mine are due" switch stays.

**Row anatomy** (top to bottom on phones; two columns on desktop):
```
Thandi Banda                         [Reply now]
Waiting for your reply · 26h         Done   Later ▾   ⋯
"asked about the blue dress price"
for you · WhatsApp
+1 more: Instagram chat, due tomorrow · for Joseph
```
- **More** (written out as a word rather than "⋯", so it reads clearly) opens: Give to… · Edit note · Not needed (with a confirm: "Cancel this follow-up? It stays in the history.").
- **At 360px:** actions sit below the text in a 2×2 grid (main + Done, then Later + More); buttons are at least 44px tall; every menu opens inside the screen; there is no horizontal scroll. "Pick a date", "Give to…" and "Edit note" each open their own small form.
- A missed-chat follow-up shows no note: the system's own note only repeats "Waiting for your reply".
- **Reuse:**
  - `.badge` for why labels;
  - `components/dropdown.html` for Later and ⋯;
  - `components/confirm.html` for Not needed;
  - `components/empty_state.html`.
  - UI spec references: `docs/design/akilent-ui-spec.md` §2, §7, §9, §18.

**Filter:** `?contact=<public_id>` shows one customer's follow-ups. The contact card's "See all" uses it.

**Surfaces outside the page:**
- **Composer:**
  - With no follow-up set, it shows **Follow up tomorrow** (hint: "Only your team sees this").
  - Once one is set, it shows "**Follow-up tomorrow 09:00 · Change**" (the page's own time words: "Follow-up 3h late", "Follow-up due now"). For a customer still waiting it reads "**Waiting for your reply · for 26h**", which a reply clears. Change opens the side panel (a drawer on phones) and moves focus to its Follow-up section.
  - It describes the customer's leading follow-up: this chat's own first, then the rest in the page's order of urgency (`followups.for_chat`).
- **Side panel:**
  - With no follow-up, it offers the same choices as Later on the page (In 1 hour · Tomorrow morning · Next week · Pick a date; *the earlier "In 3 days" was dropped so the two never differ*), an optional note for the team, **Set follow-up** and the full promise.
  - With a follow-up, it shows the row's own card (why badge, when, note, for whom, and the chat when it's another one), **Done**, **Later ▾** (with Pick a date) and **Not needed** (same confirm as the page).
  - Underneath: "+N more for this customer · All follow-ups for {name}", which opens the page filtered to that customer.
  - If the customer wrote again, it asks: "{name} wrote since this was set. Handled? Mark it done."
  - Controls are 44px tall on phones.
- **Inbox:**
  - The "Reminders" tab is removed (approved; no other tab changes). Old `?view=followups` links land on the Follow-ups page.
  - The row tag reads "Follow-up due / today / tomorrow / Tue 4 Nov". A missed-chat follow-up gets no tag, because the row's **Missed** badge already says it.
  - The header link reads "Follow-ups" with the nav badge's own number beside it (the same endpoint, hidden at 0), rather than a separately computed "(N)".
- **Keeping counts current:** the nav badge and the inbox link refresh on load, every 120s, on live updates, and at once after any follow-up form in a chat succeeds (`followup-changed`), because in-place navigation doesn't redraw the nav and live updates may be off.
- **Contact page and lead page** share one card (`conversations/_customer_followups.html`, built by `followups.customer_panel`):
  - every open follow-up of that customer (none hidden; done and cancelled never shown), the most urgent first, each worded exactly as its page row (why, when, note, for whom), with a link to its own chat, or "no chat yet: opens their customer page";
  - **Done**, **Later ▾** (with Pick a date) and **Not needed** (same confirm), posting to the page's own actions;
  - "All follow-ups for {name}" opens the page's Whole team tab filtered to them;
  - **Set follow-up** with the same choices as Later, a note for the team and the full promise. It belongs to the lead's own chat (lead page), else the customer's latest chat on a switched-on channel, else no chat, and then the card says "They have no chat yet, so it will open their customer page.";
  - empty state: "No follow-ups for {name}."; controls are 44px on phones.
- **Dashboard tile and Business Health "Opportunities at risk":** business-wide, as they always were, now on the shared definition: `due_for_team` (customers, switched-off channels excluded). Labels say so ("3 follow-ups due · whole team" / "3 follow-ups due for the whole team") and both open the **Whole team** tab, whose count is the same number. The weekly email uses the same Business Health line.
- **Business Health insight button:** "Follow up with N waiting conversations" (it only opens the inbox; it never created follow-ups) becomes "**See N waiting conversations**". Destination unchanged.
- **Automation step:**
  - "Remind your team to follow up" becomes "**Set a follow-up for your team**" everywhere it's named (step picker, step card, plain-words summary, test run, run history, AI-built automations), with "Only your team sees it; nothing is sent to the customer."
  - "Whose list" (free text) becomes "**Who it's for**" (Whole team means nobody in particular, never the chat's assignee), a select: Whoever is assigned to the chat · Whole team · each teammate. An address saved earlier for someone no longer on the team stays selected, marked "(not on your team)", rather than silently changing.
  - "Remind in (hours)" becomes "**Due in (hours)**" (see §15); "Note" becomes "Note for your team".
- **Email:** "on your Follow-ups page", not "in the bell".
- **AI chip:** "**Suggested follow-up** tomorrow: {note}", with "Only your team sees this. Nothing is sent to the customer." as its hint. Nothing happens until someone presses it. When pressed, the message is the shared one ("Follow-up set for …" or the kept-existing message), and the composer shows the follow-up at once in the page's words.
- **Onboarding:** "A follow-up makes sure your team gets back to them. Nothing is sent to the customer."; business hours are used for "follow-up times".
- **Help article "Follow-ups for your team":** written in these words. It says once that older versions called them "reminders" (and "reminder", "snooze" still find it in help search), and that a follow-up never sends anything to the customer.

## 13. Vocabulary

**Allowed:** Follow-up(s) · Waiting for your reply · Due · Coming up · Done · Later · Not needed · Give to… · Edit note · Reply now · Open chat · You set this · From an automation · Suggested follow-up.

**Banned in follow-up UI:** Reminder(s) · Remind me · Snooze(d) · Remove · Move · From Insights · Insights rule · policy · source · list ("whose list", "on X's list").

The vocabulary guard test covers every follow-up template and partial, the follow-up emails, JS message strings, the help article, the onboarding step, the automation-step labels and the AI chip.

## 14. Out of scope for this slice

These are documented in the framework and **not authorised**:
- follow-up → conversion tracking;
- lost-lead reasons;
- inbox tab changes beyond removing "Reminders";
- Business Health drill-downs;
- recommendation and experiment engines;
- broader CRM restructuring.

There are no model or migration changes.

## 15. Approved departures from the original contract

| Date | Stage | Original | Now | Why |
|---|---|---|---|---|
| 2026-10-31 | 4 | Inbox header link "Follow-ups (N)" with its own number | "Follow-ups" with the nav badge beside it, from the same endpoint (`followups_badge` → `due_for`), hidden at 0 | One count source, so the two can never disagree |
| 2026-10-31 | 4 | Side panel quick choices: Tomorrow morning · **In 3 days** · Next week · Pick a date | The page's Later choices: In 1 hour · Tomorrow morning · Next week · Pick a date (`followups.LATER_CHOICES`). **"In 3 days" removed** | The panel and the page never offer different times |
| 2026-10-31 | 4 | Changes limited to follow-up templates and views | Also `static/js/app.js` (the `followup-changed` signal after a successful chat follow-up form) and `templates/components/_nav_item.html` (badges listen for it) | In-place navigation redraws only `<main>` and live updates may be off, so the badge must be told to re-read the shared count |
| 2026-10-31 | 5 | Dashboard tile and Business Health: "N follow-ups due for **you**" (`due_for`) | **Whole team** (`due_for_team`), labelled "whole team", opening the Whole team tab. *Approved 2026-10-31.* | Both were already business-wide (every assignee; Business Health also feeds the weekly email, which has no viewer). Switching them to "you" would have silently changed their meaning. Only the unit changed: follow-up rows → customers, switched-off channels excluded, the shared definition. The nav badge remains the "for you" number |
| 2026-10-31 | 5 | Insight button "See N **customers** waiting" | "See N **waiting conversations**". *Approved 2026-10-31.* | The rule counts conversations, not customers; one customer with two chats would make "customers" wrong |
| 2026-10-31 | 5 | Contact page: rows with Done and Later only | The same card as the lead page: also **Not needed** and a **Set follow-up** form (§11 already lists contact set forms as creation surfaces). *Approved 2026-10-31.* | One card, one set of actions everywhere; a customer without a chat or lead can still be given a follow-up |
| 2026-10-31 | 6 | Automation step "Remind in (hours)" → "**Follow up in**", with presets and a custom value | "**Due in (hours)**", the existing number field (15 minutes to 60 days), no presets. *Approved 2026-10-31.* | "Follow up in" can read as an instruction to contact the customer; "Due in" matches the page's "due" and the step's own validation. Presets were left out to keep Stage 6 to wording |

## 16. Owner decisions and observations

- **Personal versus whole-team counts (2026-10-31):** the nav badge is personal (Mine, `due_for`); the dashboard tile and Business Health are whole-team (`due_for_team`), labelled as such. Preserve this distinction.
- **Notes (2026-10-31):** append, never discard or overwrite (§7). Approved at the combined review: an identical line is skipped (automations must not grow notes), and a combined note over 255 characters leaves the original unchanged with an actionable warning; the existing note stays editable.
- **Combined review of Stages 2–6 (2026-10-31):** approved to proceed to Stage 7.
- **Stage 7 (2026-10-31):** complete; results in the checklist. Fixed during Stage 7:
  - `due_customers()` listing a customer twice (counts were unaffected);
  - an automation step's "Whole team" giving the follow-up to the chat's assignee (`create(for_team=True)`);
  - the departed-teammate run message.

  Uncommitted and undeployed pending the owner's review.
- **Kept outside this slice (need separate approval):** the billing plan text "Reminders, follow-ups and replies…"; landing-page "follow-up reminders" (messaging guide); the automation editor accepting any address with "@" without checking the teammate is still active (Stage 7 verifies execution handles it safely).
- **Unrelated observation (not changed):** on the contact page, the header's **More** menu (Merge / Delete customer) stays open after any form on that page posts and returns to it. It reproduces with the page's own Add note form without the Follow-ups card. Recorded per the owner's decision; outside this slice.
- **Unrelated observation (not changed):** the automation editor's check accepts any address containing "@" for "Who it's for"; a teammate who has left is only caught when the step runs. The select marks such an address "(not on your team)".
