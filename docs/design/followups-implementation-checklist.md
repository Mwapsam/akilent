# Follow-ups redesign: implementation checklist

Build plan · 31 October 2026 · Implements `docs/design/followups-interaction-contract.md` · Part of `docs/strategy/problem-workflow-framework.md`

> **Rules:**
> - The stages run in order.
> - **A stage isn't complete because its code is written.** It is complete when its evidence report (at the bottom of this file) records the commands run and their real output.
> - **No unresolved regression carries into the next stage.**
> - Stage 2 starts only after the owner approves Stage 1.
> - No commits (the owner commits).
> - No model or migration changes.
> - Run tests with `-n 0`, because parallel runs are flaky on this machine.

## Stage overview

| Stage | Work | Depends on | Exit condition | Required evidence |
|---|---|---|---|---|
| 1 | Framework, interaction contract, this checklist | — | The documents are written and linked; owner approval | Docs-only `git status`; `messaging.md` unchanged by this stage; owner approval |
| 2 | Shared vocabulary, service and count logic | 1 | Count, grouping, message and concurrency tests pass | Service, grouping, count, message and concurrency test output |
| 3 | Follow-ups page | 2 | Grouping, sections, actions, empty states and 360px all work | Page test output; 360px browser check |
| 4 | Composer, side panel, inbox, nav | 2, 3 | Creating and managing a follow-up behave the same everywhere | Interaction test output |
| 5 | Contacts, leads, dashboard, Business Health | 2 | Every entry point uses the shared behaviour and count | Integration test output |
| 6 | Automation step, email, help, onboarding, AI chip | 2 | All wording and controls match the contract | Vocabulary guard output |
| 7 | Full regression and browser check | 2–6 | Every acceptance criterion passes | Full suite output; browser check; GIF |

---

## Stage 2: Vocabulary, service and count logic

**Files:**
- **New `apps/conversations/followup_copy.py`.** It holds every label, why label, section name, empty-state text, disclosure and action message from contract §3, §7, §8, §11 and §13, as constants or small functions such as `moved_to(when_label)`.
- **`apps/conversations/followups.py`:**
  - `due_for(user, account)` and `due_for_team(account)`: open, due now or overdue, in the account time zone; counts customers (distinct `contact_id`); Mine covers assigned-to-user plus unassigned.
  - `rows_for(user, account, scope, contact=None)` builds the grouped rows: identity, eligibility versus contents, most urgent eligible item, section, main action and "+N more" (contract §2–§5).
  - `snooze`, `close`, `reassign` and `edit` lock the row (`select_for_update` in `transaction.atomic`), re-check that it's still open, and raise a clear error for closed items (contract §10).
- **`apps/conversations/actions.py`:** pass the closed-item error through as "This follow-up was already done."
- **`apps/conversations/views.py`:**
  - `followup_action` (~1066-1124) and the done view (~1060) use the `followup_copy` messages.
  - The create path (~569) shows "This chat already has a follow-up on {date}." when `created=False`.
- **`apps/conversations/models.py`:** fix the stale `FollowUp` docstring (`recovery` → `missed`), and nothing else.

**New tests** (`apps/conversations/tests/test_followups_contract.py`):
- Grouping:
  - one customer with several active conversations gives one row; the most urgent sets the section and action; the others appear in "+N more";
  - missed plus manual gives one row, in Waiting;
  - Mine eligibility: a customer qualifies through one assigned or unassigned item, and the row still shows a teammate's item with its owner;
  - Whole team includes everyone;
  - a `policy` item goes under Due, and a team reply still closes it;
  - a follow-up without a conversation links to the contact.
- Count:
  - two follow-ups for one customer count once;
  - future items are excluded;
  - cancelled and done items are excluded;
  - an unassigned item counts for a user with nothing assigned;
  - a reassigned item moves to the new owner's count;
  - with the account time zone set to, for example, Pacific/Auckland versus UTC, items are classified correctly at the day boundary.
- Concurrency and state:
  - Done and then Later leaves the follow-up closed with `due_at` unchanged, and Later shows "already done";
  - Not needed keeps the row with `closed_reason="cancelled"`.
- Repeated missed-chat runs don't create duplicates (`recovery.create_missed_followups` run twice).
- Messages: every action's message matches contract §7, including `created=False`.
- Security: cross-account action is 404; giving to a non-member is refused.

**Pinned tests to update here:**
- `test_followups.py`
- `test_followups_complete.py` (message strings only)
- `test_inbox_polish.py:148` ("Follow-up scheduled.")

## Stage 3: Follow-ups page

**Files:**
- `apps/conversations/views.py` (~994-1049): build the page from `rows_for`; add the `?contact=` filter; set the tab counts from `due_for` and `due_for_team`.
- `templates/conversations/followups_due.html`: header and disclosure; Mine and Whole team tabs; the three sections with their empty states; the email switch.
- `templates/conversations/_followup_row.html`: the row anatomy (contract §12) with the main action, Done, Later ▾ and ⋯ (Give to… / Edit note / Not needed with a confirm); "+N more"; the 360px stacking. "Pick a date" and "Edit note" become their own small forms.
- Rebuild `static/css/app.css` from `assets/app.css` if any new utility classes are added.

**New tests:**
- the sections render with the right customers;
- a missed customer appears once;
- each section's empty state shows;
- the main action links are "Reply now" (opens the chat with the composer focused) and "Open chat";
- no main action posts to done;
- the `?contact=` filter works;
- the disclosure is present.

**Browser check:** the page at 360px has no horizontal overflow, and every action is reachable.

## Stage 4: Composer, side panel, inbox, nav

**Files:**
- `templates/conversations/_conversation_pane.html`:
  - composer (~486-512): "Follow up tomorrow" with its hint; when set, "Follow-up {when} · Change".
  - panel (~825-882): quick choices, note and "Set follow-up"; the existing state with the why label, Done, Later and Not needed; the "wrote since" prompt.
- `templates/conversations/_inbox_body.html`: remove the "Reminders" tab; row text "Follow-up {day}".
- `templates/conversations/inbox.html` (~23-25): "Follow-ups (N)".
- `apps/conversations/views.py`: inbox tab handling (drop the reminders filter) and the nav count endpoint (~1129-1155) using `due_for`.

**Pinned tests to update:**
- `test_composer_decisions.py:67-101`
- `test_panel_actions.py:89,141`
- `test_inbox_rows.py` (Reminders tab)
- `test_inbox_automation_controls.py:164`
- `test_nav_tags.py:55-73`

**New tests:**
- the composer and panel set and show follow-ups with the disclosure;
- "Change" points to the panel;
- the Reminders tab is gone;
- the nav count equals `due_for`.

## Stage 5: Contacts, leads, dashboard, Business Health

**Files:**
- `templates/contacts/detail.html` (~118-140) and `apps/contacts/views.py` (~272-285): rows link to the chat, with Done and Later; "See all for {name}" goes to `?contact=`.
- `templates/crm/lead_detail.html` and the CRM view: a "Set a follow-up" control using the same quick choices and existing services, with the disclosure.
- `apps/accounts/views.py` (~1001-1004) and `templates/accounts/_dashboard_work_queue.html`: the tile shows "N follow-ups due for you" from `due_for`.
- `apps/conversations/reporting.py` (~846-854) and `templates/insights/_at_risk.html`: use `due_for`.
- `apps/insights/rules.py` (~77-120): change the button copy to "See N customers waiting". The behaviour is unchanged.

**Pinned tests to update:**
- `test_dashboard.py:48-50,106`
- `test_insights_ui_quality.py:249`
- `test_performance.py:147` (check only)

**New tests:**
- the nav, dashboard tile and at-risk panel show the same number for the same user;
- the contact card filter link works;
- a follow-up can be created from the lead page;
- the disclosure is on the lead and contact forms.

## Stage 6: Automation, email, help, onboarding, AI chip

**Files:**
- `templates/automation/workflow_editor.html` (~853-872), `apps/automation/labels.py:53`, `apps/automation/builder.py:58`:
  - "Set a follow-up for your team".
  - "Whose list" becomes a select (Whoever is assigned to the chat · Whole team · a teammate), mapped to the existing `assignee` / email values.
  - "Follow up in" with presets and a custom value.
  - The disclosure.
- `apps/conversations/followups.py` (~318-349): email copy says "on your Follow-ups page", plus the footer line from contract §11.
- `templates/help/articles/follow-ups.html`: rewritten to the contract.
- `apps/accounts/onboarding.py` (~1087-1095): wording.
- `apps/ai/api.py` (~391-394): "Suggested follow-up" on the chip.

**Pinned tests to update:**
- `test_next_action.py`
- `test_phase7_ai_automation.py`
- `apps/ai/tests/test_phase_b.py:256`

**New test:** a vocabulary guard (`apps/conversations/tests/test_followups_vocabulary.py`) that scans every follow-up template and partial, the email code, JS message strings, the help article, the onboarding step, the automation-step labels and the AI chip for the banned words (contract §13).

## Stage 7: Full regression and browser check

- `poetry run pytest apps/conversations apps/accounts apps/insights apps/automation apps/crm apps/contacts apps/ai -n 0`, plus `apps/accounts/tests/test_landing_claims.py`; then `ruff check` and `ruff format --check` on the touched files.
- Browser check (Claude in Chrome) on the seeded demo workspace, at desktop width and 360px:
  1. Set a follow-up from the composer, the panel and a lead, and see the disclosure each time.
  2. Use Later, Done and Not needed, each with its exact message.
  3. A missed chat appears once, under Waiting.
  4. The counts match across nav, dashboard and Business Health.
  5. The "+N more" row works.
  6. There is no horizontal scroll at 360px.
  7. Record a GIF (`followups_redesign.gif`).

## Acceptance criteria (all slices)

- [ ] One customer with several active conversations gives one row; the most urgent sets the action; the others stay reachable
- [ ] Missed plus manual gives one row, in Waiting
- [ ] Repeated missed-chat events create no duplicates
- [ ] Unassigned work is counted for a user with nothing assigned
- [ ] The account time zone sets due versus coming up
- [ ] Done and Later together leave a consistent saved state; a closed item can't be rescheduled
- [ ] A cancelled follow-up isn't counted, and its row is kept as cancelled
- [ ] The customer replies after scheduling: missed closes, manual stays with the "wrote since" prompt
- [ ] A reassigned follow-up moves ownership and counts
- [ ] Due emails go only to the internal assignee; unassigned items send none
- [ ] Cross-account access is 404; giving to a non-member is refused
- [ ] Nav, dashboard and Business Health show the same `due_for` number
- [ ] The `?contact=` filter works; creating from the lead page works
- [ ] The vocabulary guard passes across every listed surface
- [ ] The disclosure is on every creation surface
- [ ] No horizontal overflow at 360px (browser-verified)
- [ ] The main action never completes a follow-up

---

## Evidence reports

### Stage 1 (2026-10-31)

- Documents written:
  - `docs/strategy/problem-workflow-framework.md`
  - `docs/design/followups-interaction-contract.md`
  - this checklist.
- Pointers added to `docs/strategy/global-positioning.md` and `docs/ROADMAP-GAPS.md`.
- `messaging.md` is unchanged by this stage. Its SHA-1 before and after is `15ca6f5e9e87bcb5d09e19e55c72131a4b2d727e`; the file already carried the earlier 2026-10-31 blocker fixes.
- No application code changed. The working tree was clean before the stage (HEAD `f6b8d7a`). Afterwards `git status` shows only:
  - `M docs/ROADMAP-GAPS.md`
  - `M docs/strategy/global-positioning.md`
  - `?? docs/design/followups-implementation-checklist.md`
  - `?? docs/design/followups-interaction-contract.md`
  - `?? docs/strategy/problem-workflow-framework.md`
- The framework has 10 numbered sections and 8 principles (counted with grep).
- **Owner approval: given 2026-10-31.** That covers the `policy`-under-Due placement.

### Stage 2 (2026-10-31)

**Built:**
- New `apps/conversations/followup_copy.py`: every label, section name, empty state, disclosure and action message.
- `apps/conversations/followups.py`:
  - `due_for`, `due_for_team` (counts customers, not rows; hides switched-off channels the same way the page does);
  - `section_for`, `FollowUpRow`, `rows_for` (one row per customer; Mine and Whole team eligibility separate from row contents; the most urgent eligible item leads; "+N more"; the contact filter);
  - `_write_open` (locks the row with `select_for_update` and re-checks it's open), used by `snooze`, `reassign`, `edit` and `close`. `close(strict=True)` raises `FollowUpClosed`.
- `actions.py`: complete, snooze and reassign return the contract messages.
- `views.py`: create shows "set for" or "kept existing". Done, Later, Give to…, Edit note, Not needed and reopen use the copy module. Done on a closed item now shows an error instead of a false success.
- `models.py`: docstring only.

**Contract refinement:** a missed follow-up moved Later sits in Coming up (contract §3 updated).

**Commands and results:**
- `pytest apps/conversations/tests/test_followups_contract.py -n 0`: **22 passed**.
- `pytest apps/conversations -n 0`: **558 passed, 11 skipped**. This includes the updated pin in `test_inbox_polish.py` (create toast becomes "Follow-up set for ").
- `pytest apps/insights apps/automation apps/ai apps/accounts apps/crm apps/contacts -n 0`: **1409 passed, 5 skipped**.
- Follow-up related tests (contract, followups, followups_complete, inbox_polish, recovery): **81 passed**.
- `ruff check` on the touched files: clean. `ruff format` applied.
- `manage.py makemigrations --check --dry-run conversations`: "No changes detected". No model or migration change.

**Pinned tests:**
- `test_followups.py` and `test_followups_complete.py` needed no change, because they don't assert the old message strings.
- The duplicate guard for missed chats is already covered by `test_recovery.py::test_second_run_does_not_duplicate`; a `create()`-level test was added.

**Unresolved regressions:** none.

**Next:** Stage 3 (the Follow-ups page) starts after owner review. The new strings and counts aren't visible on any page yet (only in action messages), because the templates change in Stages 3–5.

### Stage 3 (2026-10-31)

**Changed in this stage.** Stage 2 is still uncommitted, so the working-tree diff covers both stages.
- `apps/conversations/views.py`: `followups_due` rewritten.
  - Built on `rows_for`, worded by `followup_copy`.
  - `?contact=` filter, scoped to the account; another business's contact returns 404.
  - Tab counts come from `due_for` / `due_for_team`.
  - Later choices: In 1 hour · Tomorrow morning · Next week.
  - Pagination removed: rows are now customers, not rows of a list.
- `apps/conversations/followups.py`: a per-follow-up `target_url()`. Reply now uses `?reply=1`; the earlier `#reply` was lost by boosted navigation.
- `apps/conversations/followup_copy.py`:
  - `when_label`: "for 5 days", "3h late", "due now", "today 14:21", "tomorrow 09:00", or a date.
  - `channel_label`: "WhatsApp chat", or "no chat yet: opens their customer page".
  - `note_for`: the missed-chat job's own note isn't shown.
- `templates/conversations/followups_due.html`:
  - Header with the internal-only promise.
  - Mine / Whole team tabs.
  - Contact-filter banner with "Show everyone".
  - Three sections, each with an empty state.
  - Footer: time zone, help link, email switch.
  - A scoped style: 44px buttons on phones, a 2x2 action grid under 640px, and menus kept on screen.
- `templates/conversations/_followup_row.html`:
  - Who / why / when / note / owner / which chat.
  - "+ other follow-ups" links.
  - Main button (a link; never completes).
  - Done, Later ▾ (with Pick a date), More ▾ (Give to… / Edit note / Not needed, with confirm).
  - Each small form opens on its own.
- `templates/conversations/_conversation_pane.html`: **one attribute only.** With `?reply=1` the reply box focuses itself (needed for Reply now). The rest of the composer is Stage 4.
- Tests:
  - New: `test_followups_page.py` (13).
  - Updated: `test_followups.py` (asserts sections instead of the old `due` / `upcoming` context), `test_followups_complete.py` ("Snooze" becomes "Later"), `test_followups_contract.py` (`?reply=1`).
- `docs/design/followups-interaction-contract.md` §3/§12: `?reply=1`; "More" written as a word; the 2x2 phone grid; missed-chat note hidden.

**Owner-perspective fixes made while testing in the browser:**
- "Waiting for your reply · waiting 5 days" (said twice) became "… for 5 days".
- The system note "No reply sent - customer has been waiting" is hidden, because it repeated the label. Edit note starts empty for missed chats.
- At 360px the More menu opened 114px off the left edge. It is now a 2x2 grid with menus measured inside the screen (Later 33–257px, More 91–315px, viewport 358px).

**Commands and results:**
- Follow-up tests (page, contract, followups, followups_complete): **60 passed**; then 35 after the reply change (page + contract).
- `pytest apps/conversations apps/core apps/accounts -n 0` (excluding the known `test_phase4_load`): **1962 passed, 14 skipped**.
- `pytest apps/insights apps/automation apps/ai apps/crm apps/contacts` plus the page tests: **878 passed, 2 skipped**.
- `ruff check apps/conversations`: clean. `ruff format` applied.

**Browser evidence:** a throwaway server on :8077 with its own SQLite file in the scratchpad, the seeded demo workspace, and 7 scenario follow-ups. The owner's dev database was not touched.
- **Desktop, 1536px:** Waiting / Due / Coming up render. One row for a customer with two chats, with the other as "+ … Instagram chat, tomorrow …". The automation label, the no-chat "Open customer" row, and Mine versus Whole team all show correctly.
- **Later → Tomorrow morning:** the row moved from Due to Coming up ("tomorrow 09:00"); the counts went from 3 to 2; saved `snoozed_count=1`.
- **More → Not needed:** the in-page confirm "Cancel this follow-up? It stays in the history."; saved `closed_reason=cancelled` with the row kept; flash "Follow-up cancelled."; the Due empty state shown.
- **More → Give to… → Me:** flash "Follow-up assigned to Demo Owner."; the row reads "for you".
- **Later → Pick a date:** the form opens and Cancel closes it (real clicks).
- **Reply now:** opens `/inbox/<chat>/?reply=1` with the reply box focused (`activeElement` is aria-label "Reply").
- **360px:** the page rendered at 360px via `srcdoc` (the window can't shrink below its desktop size). `scrollWidth` 348 ≤ viewport 358; no element overflows; all menus open inside the screen.
- **Desktop menus:** they stay inside 1536px.
- GIF `followups_stage3.gif` downloaded (19 frames).

**Not in this stage (as planned):**
- The nav badge still uses its old count (it showed 4 while Mine showed 3); it moves to `due_for` in Stage 4.
- The composer still reads "Reminder set for…" (Stage 4).

**Unrelated changes:** none. The only template outside the page is the one-attribute composer hook above.

**Unresolved regressions:** none.

## Stage 4 evidence (2026-10-31): composer, side panel, inbox, nav count

**Scope:** the two remaining consistency problems (the nav count and the composer's wording) and the surfaces they touch. No model or migration changes, no new follow-up features, and nothing committed.

**What changed:**
- `apps/conversations/followups.py`:
  - `due_customers(account, user=None)` is the one set of due customers; `due_for` and `due_for_team` count it;
  - `for_chat(conversation)` returns the customer's follow-ups, this chat's first, in the page's urgency order;
  - `LATER_CHOICES` is shared by the page and the panel.
- `apps/conversations/followup_copy.py`: `CHANGE`, `PICK_A_DATE`, `composer_label`, `inbox_tag`, `others_label`, `all_for`, `wrote_since`.
- `apps/conversations/views.py`:
  - `followup_card()` moved to module level, so the page, composer and panel build the same card;
  - the chat context uses `for_chat` and the card;
  - `followups_badge` uses `due_for`, with the label and toast from `due_count` ("3 follow-ups due for you");
  - the inbox's Reminders view is removed, and `/inbox/?view=followups` redirects to the Follow-ups page;
  - inbox rows carry `followup_tag` (missed-chat follow-ups excluded).
- `templates/conversations/_conversation_pane.html`:
  - composer: "Follow up tomorrow" + "Only your team sees this"; once set, "Follow-up {when}" + Change, which opens the panel and focuses `#cv-followup`;
  - panel: the card, Done / Later (with Pick a date) / Not needed (confirm), "+N more for this customer · All follow-ups for {name}" (filtered page), and a set form with the same choices, a note and the promise.
- `templates/conversations/_inbox_body.html`: Reminders tab and its empty state removed; row tag "Follow-up {day}".
- `templates/conversations/inbox.html`: the header link "Follow-ups" with the badge endpoint's number.
- `templates/components/_nav_item.html` + `static/js/app.js`: badges also refresh on `followup-changed`, which fires after any chat follow-up form (`data-followup-form`) succeeds.
- `assets/app.css` + `static/css/app.css`: one rule for 44px panel controls on phones. The compiled file is the committed one plus exactly that rule. A full local rebuild also changed unrelated base styles, so it was not used.
- Tests:
  - new: `test_followups_surfaces.py` (14);
  - updated pins: `test_composer_decisions.py` (new wording), `test_panel_actions.py` ("Set follow-up"), `test_inbox_polish.py` (6 tabs, not 7).

**Same definition, not just the same number:** `test_the_count_is_the_same_customers_as_the_page` builds every case (mine, unassigned, two follow-ups for one customer, a teammate's, coming up, missed, done, cancelled). It asserts that `due_customers` equals the set of customers in Waiting and Due on the page, for Mine and for Whole team, and that the badge, the Mine tab and `due_for` agree. The old badge counted follow-up rows and ignored switched-off channels: on this fixture it would have said 4 against the page's 3.

**Commands and results:**
- `pytest apps/conversations apps/core apps/accounts -n 0 -k "not test_phase4_load"` (final code): **1976 passed, 14 skipped**.
  - One regression found on the first run and fixed: `test_inbox_confidence::test_the_pane_carries_no_inline_stylesheet`. The 44px rule had been a `<style>` block in the pane; it moved to `app.css`.
- `pytest apps/insights apps/automation apps/ai apps/crm apps/contacts -n 0`: **865 passed, 2 skipped** (run before the CSS move, which touches only the chat pane).
- Follow-up, pane and nav tests after the move: **75 passed**.
- `ruff check apps/conversations`: clean; `ruff format --check`: clean.

**Browser evidence** (throwaway server on :8077, scratch SQLite, seeded demo workspace; the dev database was not touched):
- **Count:** the nav badge read 3 = Mine (3); in Stage 3 it read 4. The inbox header link also read 3.
- **Later from the panel** (Joseph): the composer changed from "Follow-up 3h late" to "Follow-up tomorrow 09:00"; the server count went to 2.
  - **Found:** the nav badge stayed at 3. In-place navigation only swaps `<main>`, and live updates were off.
  - **Fixed** with `followup-changed`. A first attempt didn't fire, because the form is swapped out before `afterRequest`; the request is now marked at `beforeRequest`.
- **Done from the panel** (Grace, an automation follow-up): the composer went back to "Follow up tomorrow"; the count went 2 → 1.
- **Thandi (missed + manual):**
  - the composer read "Waiting for your reply · for 5 days";
  - first Done: the manual one now leads ("You set this · 1h late · 'Confirm delivery address' · for you"), and the badge **stays 1** (the customer still has work);
  - second Done: every badge cleared at once, and the server agrees (0).
- **Change:**
  - **Found:** at first it opened the drawer at the top, because the drawer is hidden until open.
  - **Fixed:** it now scrolls to and focuses `#cv-followup` (desktop and 360px).
- **Inbox:** the Reminders tab is gone (6 tabs). Rows read "Follow-up tomorrow" / "Follow-up Fri 6 Nov", and a missed chat shows only Missed.
- **Follow-ups page afterwards:** Mine and Whole team show no counts, Waiting and Due are empty, Coming up (4), and there is no badge. Everything agrees.
- **360px:**
  - chat: `scrollWidth` 350 ≤ 360, nothing overflows; Change opens the drawer with the section in view and focused;
  - **found:** Done (30px) and Not needed (28px) were under 44px; **fixed:** now 44 / 46 / 44px;
  - inbox: `scrollWidth` 352 ≤ 358, no overflow.
- GIF `followups_stage4.gif` downloaded (19 frames).

**Not in this stage (as planned):**
- Contact page "No reminders…" (Stage 5).
- The page footer's email-toggle messages still say "reminders", and the help article and emails too (Stage 6).
- Dashboard tile and Business Health on `due_for` (Stage 5).

**Unrelated changes:** none. Every file above is a follow-up surface named in the Stage 4 plan, plus the badge refresh (`app.js`, `_nav_item.html`) and the one CSS rule needed to meet the acceptance criteria.

**Unresolved regressions:** none.

## Stage 5 evidence (2026-10-31): contacts, leads, dashboard, Business Health

**Scope:** the four customer- and business-level surfaces, on the shared definition and wording. No model or migration changes, nothing committed, Stage 6 not started.

**Meaning check (owner's review boundary):**
- The contact card was a list, not a count. It was capped at 5 (follow-ups could be hidden), ordered by due time, and included switched-off channels.
- The dashboard tile and Business Health both counted follow-up **rows** for the **whole business**. They are two separate queries that happened to match. Business Health also feeds the weekly email, which has no viewer.
- Decision documented before changing (contract §15): keep both business-wide, on `due_for_team`, labelled "whole team", opening the Whole team tab. The contract's original "for you" is recorded as a departure pending review.

**What changed:**
- `apps/conversations/followups.py`:
  - `card()` (moved from views; the page, composer, panel, contact and lead all use it);
  - `for_customer()` (`for_chat` now wraps it);
  - `latest_chat()` (switched-on channels; never-messaged chats sort last on Postgres too);
  - `customer_panel()`.
- `apps/conversations/followup_copy.py`: `no_followups_for`, `NO_CHAT_NOTE`.
- `apps/conversations/views.py` + `urls.py`: `followup_create` at `/inbox/followups/new/`.
  - The contact must be this business's.
  - The chat must be that contact's and on a switched-on channel, else 404.
  - With a chat it uses the `create_followup` action; without one it uses `followups.create`.
  - Same messages as the chat.
- `templates/conversations/_customer_followups.html` (new): the shared card.
- `apps/contacts/views.py` + `templates/contacts/detail.html`: the old card is replaced by the shared one; a new follow-up belongs to the latest chat.
- `apps/crm/views.py` + `templates/crm/lead_detail.html`: the card sits under the lead details. A new follow-up belongs to the lead's chat, else the latest chat, else none.
- `apps/accounts/views.py` + `templates/accounts/_dashboard_work_queue.html`: the tile uses `due_for_team`, reads "follow-ups due · whole team" and links to `?list=team`.
- `apps/conversations/reporting.py`: `followups_due` uses `due_for_team`; the line reads `due_count(n, team=True)` and links to `?list=team`.
- `apps/insights/rules.py`: the button reads "See N waiting conversations" (same destination).
- `assets/app.css` + `static/css/app.css`: the 44px phone rule now also covers the card (an edit to the existing one-rule insertion; nothing else in the compiled file changed).
- Tests: new `test_followups_customer_surfaces.py` (11).

**Acceptance coverage (`test_followups_customer_surfaces.py`):** one fixture covers:
- several follow-ups for one customer;
- unassigned items and a teammate's;
- coming up and overdue;
- done and cancelled;
- a customer with no chat.

The tests assert:
- Mine = nav badge = `due_for` = 3, and Whole team = dashboard tile = Business Health = `due_for_team` = 4;
- Done, Later and Not needed move all of these together (3/4 → 3/3 → 2/2 → 1/1);
- the contact card reads like the page row and hides nothing;
- done and cancelled never show;
- the no-chat customer is understandable and can be given a follow-up;
- a new follow-up joins the latest chat or keeps the existing one;
- another business's customer or a mismatched chat gets a 404;
- the lead page sets a follow-up on the lead's chat;
- the insight label is honest.

**Commands and results (final code, after the last CSS change):**
- `pytest apps/conversations apps/core apps/accounts -n 0 -k "not test_phase4_load"`: **1988 passed, 14 skipped**.
- `pytest apps/insights apps/automation apps/ai apps/crm apps/contacts -n 0`: **865 passed, 2 skipped**.
- `ruff check` / `ruff format --check` on the touched apps: clean.

**Browser evidence** (throwaway server on :8077, scratch SQLite, seeded demo; one overdue follow-up added for the teammate so Mine and Whole team differ; the dev database was not touched):
- **Dashboard:** nav badge 3 (Mine); tile "4 · follow-ups due · whole team". The tile opens the Whole team tab, which shows **Whole team (4)** and **Mine (3)**.
- **Contact page (Joseph):** both follow-ups, the most urgent first, in the page's words, each linking to its own chat.
  - Later → Tomorrow morning: the card changed to "tomorrow 09:00" and both badges went 3 → 2 **without a full page reload** (a window marker survived).
  - The first read came during the swap, because the scratch SQLite was retrying a lock from session tracking.
- **Lead page (Joseph):** the card sits under the lead details.
  - Set follow-up with "In 1 hour" kept the chat's existing follow-up and moved it earlier (tomorrow 09:00 → today 13:06), per the contract's "kept an existing one" rule.
  - **Observed:** the note typed with it is dropped in that case. This is existing Stage 2 behaviour, flagged for review.
- **Contact without a chat (Grace Mulenga):** "for you · no chat yet: opens their customer page", plus the no-chat note on the set form. Not needed shows the page's confirm, then "Follow-up cancelled." and the empty state.
- **Business Health:** "3 follow-ups due for the whole team" (after the actions above) = tile 3 = Whole team (3); badge 2 = Mine (2).
- **360px:** dashboard, lead and contact pages each `scrollWidth` 348 ≤ 358, nothing overflows, and every follow-up control is at least 44px.
- GIF `followups_stage5.gif` downloaded (12 frames).

**Existing issue found, not caused by this stage:** on the contact page, the header's **More** menu (Merge / Delete customer) is left open after any form on the page posts and returns to the same page.
- It reproduced with the page's own "Add note" form after temporarily removing the new card. The card was restored unchanged afterwards.
- Not fixed here (out of scope); reported for a decision.

**Not in this stage (as planned):** the email-toggle messages, help article, emails, automation step and onboarding wording (Stage 6).

**Unrelated changes:** none. Every file is a Stage 5 surface or the shared helpers they use.

**Unresolved regressions:** none.

## Stage 6 evidence (2026-10-31): automation step, emails, help, onboarding, AI chip, note rule

**Scope:** the remaining surfaces' wording and controls, plus the owner's note decision. No model or migration changes, nothing committed, nothing deployed.

**Owner decisions recorded (contract §7, §15, §16):** the three Stage 5 departures are approved; notes are appended, never discarded or overwritten; the contact page More menu is an unrelated observation.

**What changed:**
- **Note rule:**
  - `followups.append_note` + `create()`: the kept follow-up gets the new note on its own line. Blank notes change nothing, an identical line isn't repeated, and a note too long to fit is left out and reported (`NOTE_TOO_LONG`), never cut.
  - The `create_followup` action returns `note`, and `kept_existing(when, note_result)` says what happened (a warning when it didn't fit).
  - `edit()` stores `\r\n` as `\n`.
  - Notes show their line breaks (`whitespace-pre-line`) on the page row, the side panel and the customer card. Edit note is a two-line textarea, because a one-line input would join the lines.
- **Email switch and emails:**
  - `EMAILS_TOGGLE_LABEL`, `EMAILS_ON`, `EMAILS_OFF`, `EMAIL_INTRO` and `EMAIL_REASON` in `followup_copy`. They state the real rule: emails only for follow-ups given to you, at most hourly, never whole-team or waiting customers, never the customer.
  - The due email lists note (or why), due time and chat.
  - `CHOOSE_WHEN` replaces "Choose when to be reminded."
- **Follow-ups page footer:** the email switch moved to the left with room below.
  - **Found in the browser:** Stage 3 had moved it to the bottom-right corner, where the floating setup launcher covered it (`elementFromPoint` hit the launcher).
  - **Fixed:** it is clickable on desktop and at 360px.
- **Automation step:**
  - Wording updated in `labels.py`, `builder.py`, `compose.py`, `explain.py` (both sentences), `simulate.py` and the `workflow_engine.py` checks, all reading "Set a follow-up for your team".
  - Editor: "Due in (hours)", "Note for your team", and "Who it's for" as a select (assignee / Whole team / each teammate, from `_followup_owners`). An address saved for someone no longer on the team shows "(not on your team)".
- **AI chip (`apps/ai/api.py`, `views.ai_apply`, `chat.js`):**
  - the label is "Suggested follow-up …", with the internal-only hint as title and accessible label;
  - the apply message is the shared set/kept message;
  - the response carries `followup_label`, so the composer shows "Follow-up …" at once, and the badges get `followup-changed`.
- **Onboarding:** the `set_followup` and `hours` descriptions.
- **Help:** the article is rewritten; title "Follow-ups for your team", summary updated, old search words kept.
- Tests:
  - new: `test_followups_stage6.py` (22, including 5 parametrised note cases);
  - updated: `apps/ai/tests/test_phase_b.py` (chip label, hint, `followup_label`, shared message).

**Commands and results (final code):**
- `ruff check apps`: clean; `ruff format --check` on the touched apps: clean.
- `pytest apps/conversations apps/core apps/accounts -n 0 -k "not test_phase4_load"`: **2003 passed, 14 skipped**.
- `pytest apps/insights apps/automation apps/ai apps/crm apps/contacts -n 0`: **865 passed, 2 skipped**.
- Regressions found and fixed during the stage:
  - 8 automation editor tests failed because the new helper had been placed under `workflow_editor`'s decorators (fixed);
  - `test_phase_b` was pinned to the old chip label (updated deliberately).

**Browser evidence** (throwaway server on :8077, scratch SQLite, seeded demo plus a scratch workflow; the dev database was not touched):
- **Note append (contact page, Joseph, existing follow-up "Send photos of the blue dress"):** Set follow-up with "Ask if Saturday delivery works" kept the same follow-up, still 3h late (next week is later). The card shows both lines.
- **Follow-ups page:** the row shows both lines, and Edit note (real clicks: More → Edit note) shows both. Saving unchanged keeps them (`"…dress\nAsk if…"`).
- **Email switch** (real clicks):
  - off: "Follow-up emails are off. Yours still show here, with the count beside Follow-ups in the menu.";
  - on: "We'll email you when follow-ups given to you are due, at most once an hour. Whole-team follow-ups and customers waiting for a reply aren't emailed."
- **Automation editor:**
  - the step reads "Set a follow-up for your team" with "Only your team sees it; nothing is sent to the customer.";
  - Who it's for offers assignee / Whole team / Demo Owner / Tendai Demo, and the editor reports Valid;
  - with `gone@example.com` saved, the select shows "gone@example.com (not on your team)" and keeps the value.
  - **Found:** before a `:selected` binding was added, it displayed "Whoever is assigned to the chat" for that address. **Fixed.**
- **Help:** "Follow-ups for your team" renders; help search for "reminder" and "snooze" finds it.
- **360px:** help article, Follow-ups page (switch not covered) and automation editor each `scrollWidth` 348 ≤ 358, nothing overflows.
- **AI chip: not exercised in the browser.** The scratch setup has no AI provider to draft a suggestion. It is covered by `test_phase_b` (label, hint, shared message, `followup_label`).
- GIF `followups_stage6.gif` downloaded (23 frames).

**Not changed (outside the slice, reported):**
- the automations plan feature text "Reminders, follow-ups and replies that run on their own" (`apps/billing/features.py`);
- landing-page claims ("follow-up reminders"), which are governed by `docs/marketing/messaging.md`;
- internal names (`snooze` URLs and actions, `snoozed_count`) and code comments.
- Two observations are recorded in contract §16.

**Unrelated changes:** none beyond the Stage 3 footer fix above, which corrects a regression this redesign introduced.

**Unresolved regressions:** none.

## Stage 7 evidence (2026-10-31): full regression and browser walkthrough

**Scope:** the owner's Stage 7 list. Nothing committed or deployed.

**Defects found and fixed in this stage:**
1. **`due_customers()` could list a customer twice.**
   - Cause: the model's default `ordering = ["due_at"]` was carried into `DISTINCT`.
   - Every count was right, because `.count()` drops ordering, and the Stage 4 test compared sets, which hid the duplicate.
   - Found by comparing the live page's customers with `due_customers()` during the browser walkthrough.
   - Fixed with `.order_by()`; `test_the_count_is_the_same_customers_as_the_page` now compares sorted lists.
2. **An automation step set to "Whole team" gave the follow-up to the chat's assignee.**
   - Cause: `followups.create()` defaults a missing assignee to the creator, else the chat's assignee.
   - This predates the redesign. The old editor placeholder, the engine's own check and the contract all said "whole team".
   - Fixed with `create(..., for_team=True)`, passed by the step when "Who it's for" is Whole team. Other callers are unchanged.
3. **A teammate who has left: the run history said "The teammate it should notify is no longer on your team".**
   - That is the notify step's wording.
   - It now says "The teammate this follow-up was for is no longer on your team, so no follow-up was set. Choose someone else in Who it's for."
   - The run stops as failed, and no follow-up is created.

**Commands and results (final code):**
- `ruff check apps` and `ruff format --check apps`: clean.
- `pytest -n 0 -k "not test_phase4_load"` (the whole suite, every app): **5286 passed, 25 skipped, 0 failed** (10m11s).
- `pytest apps/core/tests/test_phase4_load.py`: **5 passed**. It was excluded from earlier runs as a known failure on HEAD; run alone it passes today.
- New: `test_followups_stage7.py` (7):
  - assignment for Whole team / assignee / teammate;
  - a departed teammate: no follow-up, run failed, accurate message;
  - email eligibility (unassigned, waiting-customer, opted-out, given-to-you);
  - the hourly limit;
  - old links and help words.

**Compiled CSS:** `static/css/app.css` equals HEAD's file plus exactly one rule (249 bytes), the follow-up controls' 44px phone rule. `assets/app.css` adds exactly the matching block. Every class used in the changed templates exists in the compiled file. No rebuild, and no unrelated generated changes.

**Browser walkthrough** (scratch server on :8077 with its own SQLite file; the dev database was not touched):
- **AI chip:**
  - Fixture: AI provider name set in the scratch settings, `ai_assistant` granted to the demo business in the scratch database, and a ready AI suggestion stored with a follow-up extra. No model was called.
  - The chip reads "Suggested follow-up tomorrow: Check Saturday delivery is booked", with the title and accessible label "Only your team sees this. Nothing is sent to the customer."
  - The card shows only while the 24-hour window is open, and the scratch chat's window was closed. The chat's `windowOpen` flag was set in the browser to show it, then the chip was clicked.
  - The real `ai/apply` call returned 200. The composer changed from "Follow up tomorrow" to "Follow-up tomorrow 13:20", the chip showed applied, and the badges re-read the count at once (three badge requests in the same second).
  - The count stayed 3, correctly, because the new follow-up is due tomorrow. It then appears on the Follow-ups page as "You set this · tomorrow 13:20 · 'Check Saturday delivery is booked' · for you".
- **Notes (real contact-page form, Joseph, existing "Send photos of the blue dress"):**
  - duplicate line: unchanged, "It already has that note.";
  - blank: unchanged, plain kept-existing message;
  - new: appended on its own line, "Your note was added to it.";
  - 240 more characters: unchanged, "Your note wasn't added: a note holds 255 characters. Use Edit note to shorten it.";
  - Edit note afterwards still saves both lines.
  - **Chipo** (no manual follow-up): a blank set created an empty note; a later note filled it as exactly "Ask about the red one". One follow-up, no duplicate.
- **Shared counts as sets:** the live page's Waiting + Due customers for Mine and for Whole team equal `due_customers()` exactly (Grace Phiri, Joseph Mwale, Thandi Banda, each once after fix 1). Badge 3 = Mine (3) = tile 3 = Business Health 3.
- **Old links:** `/inbox/?view=followups` lands on Follow-ups. Old snooze URLs still work (test). Help search for "reminder", "snooze" and "remind me" finds "Follow-ups for your team".
- **Automation assignment:** active teammates are listed and Whole team is the default (Stage 6 browser). A departed address stays shown as "(not on your team)". Execution with a departed teammate is covered by fix 3 and its test.
- **360px:**
  - Follow-ups page: `scrollWidth` 348 ≤ 358; four menus open inside the screen (33–257 / 91–315); action buttons ≥ 44px; the email switch is clickable.
  - Chat: composer "Follow-up 3h late · Change"; Change opens the drawer focused on the section; controls 44 / 46 / 44px.
  - Inbox 352, contact 348, lead 348, dashboard 348, help 348, automation editor 348: no overflow; card controls ≥ 44px.
- GIF `followups_stage7.gif` downloaded.

**Dev-only observation:** the unhashed dev server let the browser keep a cached `chat.js` once, which hid the composer button until a hard reload. Production uses hashed static names (`CompressedManifestStaticFilesStorage`), so a deploy can't be served stale like this.

**Kept outside this implementation (documented, separate approval needed):** the billing plan text "Reminders, follow-ups and replies…"; landing-page "follow-up reminders"; the automation editor accepting any "@" address without checking the teammate is still active (execution now fails safely and says why).

**Remaining small gaps (not fixed, for review):**
- After an AI chip is applied, the composer updates in place, but the side panel shows the change only on the next load. The in-place composer line has no "Change" button.
- An AI "tomorrow" means 24 hours from now, not 9:00 (existing timing).

**Status:** Stages 1–7 complete. Unresolved regressions: none. Awaiting the owner's review before any commit or deploy.
