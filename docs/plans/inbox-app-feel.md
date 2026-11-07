# Inbox: the same treatment as Content Studio (plan of 2026-11-06)

## Context
A walkthrough of Inbox on akilent.com found its structure sound (split view, pane loads without a reload, contact panel, date dividers, resend and window-closed paths) but not yet at Content Studio's level:
- **Slow:** about 1.5 s to first byte for the list and 1.2 s to open a conversation.
- **Little room for messages:** at a 631 px window height, the big title and toolbars leave about 260 px.
- **Controls:** 4 native selects, a ▾ `<details>`, and an underlined link.
- **No shortcuts:** J/K do nothing.
- **Two "window closed" prompts.**
- **A horizontal scrollbar** under the list.
- **The contact panel** has its own drawer code.

The reviewer's conditions:
- reuse Content Studio's system rather than copy it;
- new pieces (bubbles, composer, live updates, a docked third pane) are built as shared components;
- speed is part of done for each phase.

**A second live walkthrough (2026-11-06) set the order: speed, then conversation state, then layout, then look and feel.** It found:
- **Speed:** the Inbox page runs 60 queries (113 ms db, 477 ms app, about 1.5 s total), against 15–25 for Content Studio tabs. Three background checks run (the follow-up badge, up to 1.1 s; the list feed; the thread).
- **State:**
  - a list row whose last messages failed shows "You: download_app" with no failure mark;
  - the thread shows a raw "(error 131042)";
  - a closed conversation has no Closed badge in its row;
  - the ⋯ menu offers "Close as…" on a closed conversation and has no Reopen.
- **Views and filters:**
  - only Unassigned and All show counts (zeros hidden);
  - All includes closed conversations;
  - the channel filter is a native list offering channels the workspace hasn't connected (SMS, Website Chat);
  - no Inbox search.
- **Layout:**
  - the list is 19 px wider than its column;
  - the filter bar shifts 12 px when a conversation opens;
  - the ⓘ panel covers the thread;
  - the template banner is cut off;
  - after the window closes, the reply box and a blue mic still look active (a blue used nowhere else).
- **Look:**
  - no Content Studio title, type or card pass;
  - a native template dropdown;
  - the yellow (main action) and navy (the rest) button rule isn't applied ("Send template" is yellow inside a dialog);
  - no row or menu fade-in, and no consistent hover.

**What the code shows for these:**
- `friendly_errors.py` already maps 131042, so the raw code must come from a sentence stored before that mapping existed, or from a code in a different shape. The fix works the text out when the thread is shown.
- Closed rows: `derive_state` only gives `closed_reason` once the conversation has messages that count, so a closed chat with only failed sends gets no badge.
- The ⋯ menu's "Close as" is hidden with Alpine `x-show="open"`, and the reopen form sits elsewhere in the pane.
- `visible_choices` keeps channels that aren't in the registry (SMS).

**What exploring found (reuse, don't rebuild):**
- **The system is already global, not Content Studio-only:**
  - `base.html` loads `overlay.css`, `studio.css` and `controls.css` for every signed-in page, and includes `overlay_host.html` and `page_skeletons.html`.
  - In `app.js`: overlay host (`#overlay-body`, J/K in drawers), `data-live-filter`, the shortcut handler (`/`, `?`, J/K over `#main [data-row]`) and toasts with Undo.
  - `controls.css` has `.seg` and `.switch`; menus are `cvMenu` (`static/js/chat.js`) with `.cv-menu*` (`assets/app.css`).
  - So "pull the system out first" is mostly done. What remains: the `/publishing/` check in the N shortcut becomes a per-page `data-shortcut-new`, and `studio.css` pieces Inbox needs (`.cs-head`, empty states) move into a shared `layout.css` section.
- **The setup bubble is already in the header** (`.ob-launch` in `base.html`, from the look-and-feel pass, not yet on prod). Phase 1 only checks the open checklist and the welcome tour don't cover the composer.
- **The pane works:** `static/js/inbox_pane.js` handles the pane (latest click wins, history, epochs, error state). `chat.js` `chat()` handles the thread: polling with backoff, a "new below" pill, delivery labels, presence, unconfirmed sends, and the SSE hook. Keep all of it.
- **Why it's slow** (`apps/conversations/views.py`):
  - **The pane:** `conversation_detail` loads the **whole thread** with no limit. It builds the side panel on every open: CRM lead/deal, order, conversation count, automation activity, forms, follow-ups, notes, team, every approved template and every saved reply.
  - **The list:** `_inbox_context` runs **six separate count queries** plus `next_action_card`. A full page load of a conversation does the list and the pane together.
- **Live updates already exist but are polled:** the list every 8 s (hash, 204 when unchanged), the thread with backoff. SSE (`REALTIME_SSE_ENABLED`, the `events` service) is built but off, and turning it on is an infrastructure step, outside this plan.

## Rules that stay binding
- **The same as the Content Studio plan:**
  - UI quality guards (drift ratchet 0, `test_alpine_fields`, no `x-init="init()"`, comment rules);
  - every URL real and forms working without JavaScript;
  - no fake states;
  - `Vary`/`no-store` on partials;
  - reduced motion;
  - touch and 360 px;
  - analytics virtual pageviews;
  - dark-mode tokens.
- **The existing inbox tests keep passing** (`apps/conversations/tests/test_inbox_*.py`, `test_panel_actions.py`), and so do `tools/qa/inbox_qa.mjs` and `sse_qa.mjs`.
- **Nothing sends or changes data on prod** during checks: there, only open conversations and the panel, nothing else (no close, assign, tag or read-state changes on purpose).
- **Gates:** approving this plan authorises Phases 0 and 1. Each later phase needs its own go-ahead. No commits.
- **Speed is part of done:** every phase keeps the query budgets from Phase 0 green.
- **First step:** copy the Content Studio plan (kept below the line in this file) to `docs/plans/content-studio-app-feel.md`, and point its memory there.

## Phase 0: speed (measured first)
1. **Measure.** `ServerTimingMiddleware` already reports `db`/`app` time.
   - Record a baseline TTFB and query count for `/inbox/`, a pane load, a panel load, the badge, the feed, and a full page load of `/inbox/conv_…/`.
   - Use realistic data: 200 conversations, a 500-message thread.
   - **Under load as well,** with a small script against gunicorn at prod's settings (2 workers):
     - three Inbox tabs, each running its three checks;
     - one person opening conversations at the same time.
   - It reports whether the badge's 1.1 s is slow code or waiting for a free worker, and whether more workers or threads are needed. Changing `GUNICORN_WORKERS` is an infrastructure step left to you.
2. **The pane loads the recent messages only.**
   - It opens from whichever is older: the last 50 messages, or the first unread one (so Phase 5's "New messages" divider always has its message loaded). The cap is 200, beyond which the divider says how many more are unread above, with "Show earlier".
   - A "Show earlier messages" control sits at the top of the thread.
   - It loads the next 50 through `messages_feed?before=<id>`, a new parameter beside the existing `after`. The scroll position is kept by measuring `scrollHeight` before and after.
   - `lastId`, unconfirmed-send matching and day dividers keep working. Older messages a send must match are always newer than the window, so nothing is lost.
3. **The side panel loads after the thread.**
   - `#chat-info`'s body moves to `_conversation_panel.html`, served by a new `conversations:panel` URL with `hx-trigger="load"` (on a phone or narrow pane, the first time the drawer opens).
   - Its actions (assign, close, tag, pause and the rest) keep posting to `detail_url`. Their answer is the panel, plus the conversation header's status and assignee swapped out of band (`hx-swap-oob`, as Content Studio's overlay saves do), plus `HX-Trigger: inbox-refresh` so the list row updates. Nothing on screen is left stale. Tested per action.
   - The heavy panel queries (CRM, orders, automations, forms, count, notes) leave the pane's first response.
   - The page without JavaScript still includes it, through `{% include %}` on the full page.
4. **Load on demand:**
   - approved templates only when the window is closed or the picker opens;
   - saved replies when their menu opens (`hx-get` into the menu), not on every pane.
5. **The list:**
   - one aggregate query for the four simple tab counts (`Count(..., filter=Q(...))`);
   - the needs-attention and missed counts kept as two queries;
   - check `snapshot_of` and presence add no query per row.
6. **The request memo:** the Content Studio memo (`apps.core.request_context.remembered`, `RequestMemoMiddleware`) is applied to what the inbox reads more than once per request. Find those with a query log of the 60: billing state, site settings, the account, team members, channel state, onboarding.
7. **Background checks:**
   - The follow-up badge goes from every 120 s plus events to every 300 s plus events, and its view gets its own budget (aiming under 50 ms).
   - The list feed keeps its exact check (render, hash, 204 when unchanged). A cheap fingerprint can't be trusted here: `Message` has no change time, so a send that fails later moves nothing a fingerprint could read. Instead the render itself gets cheaper through items 5–6. A test checks that a message turning `failed` after the fact makes the next feed answer 200 with "Not sent" on the row.
   - The thread poll is unchanged.
8. **Budgets (`apps/conversations/tests/test_inbox_speed.py`):** `django_assert_max_num_queries` for the list, the pane, the panel, the feed and the badge, at counts that don't grow with rows or thread length.
   - **Targets:**

     | Request | Queries |
     |---|---|
     | the list `/inbox/` | ≤ 25 (from 60) |
     | a pane load | ≤ 20 |
     | the panel | ≤ 20 |
     | a full page `/inbox/conv_…/` (list and pane together) | ≤ 40 |
     | the feed | ≤ 15 |
     | the badge | ≤ 5 |

   - Any target the baseline shows can't be met without changing behaviour is reported, not loosened quietly.
   - **Server time:** under 300 ms on the dev box for the list, the pane and the panel, and under 450 ms for the full page.

## Phase 1: conversation state, views and filters
1. **Failed sends show in the list.** The row annotation also reads the latest message's `status`. A failed or held last outbound message gets a red "Not sent" mark and the reason on hover, in place of the plain "You: …" preview.
2. **Plain error text, worked out when shown:**
   - the thread's `failureReason` comes from `friendly_send_error(<stored code>)` when the message is rendered, falling back to the stored sentence only when no code was kept, so old messages pick up new wording;
   - 131042 (and other account-level codes: 131037, 190) also get a "Fix it" link: Meta payment settings, or Akilent's channel page for a reconnect;
   - a test checks every `_GRAPH_ERRORS` code renders without "(error ".
3. **One meaning of "closed": the stored status** (your choice).
   - **Closed** means someone closed it (or STOP closed it). Its row says Closed, from `status == CLOSED`, whether or not it has messages that count.
   - **An open chat where the customer hasn't written for 24 hours is not "Closed".**
     - Its row says **Missed** when we owe a reply (as today).
     - It says **Quiet** when we spoke last, with the hover text "No message from the customer in over 24 hours".
     - It stays in the open views.
   - `derive_state`'s `inactive_24h` keeps its meaning for reports and automations. Only the inbox's label and view membership change. I'll check its other callers (recovery, metrics) aren't affected.
4. **Reopen in the ⋯ menu.** The ⋯ menu is rendered for the real status on the server, not hidden with `x-show` (which can show a stale state after an action):
   - closed: "Reopen";
   - open: "Close as…".
5. **Every view shows its count, 0 included,** in the menu and the tabs, and Missed stays red only when above 0.
6. **The channel filter lists only connected channels.** It drops non-registry channels the account has never used (SMS, Website Chat), keeping any channel with existing conversations so old chats stay reachable.
7. **Open views show open chats only** (your choice), so the counts add up:
   - All, Unassigned, Assigned to me, Needs attention and Missed all filter `status=OPEN`. Unassigned and the two waiting views already do; All and Assigned to me change.
   - Closed shows `status=CLOSED`.
   - A test checks that All = open total, and All + Closed = every visible conversation.
   - Old `?view=all` links keep working.
   - **A closed chat with a follow-up due** (your choice) stays in Closed with its follow-up tag, and the Follow-ups page lists it when due. The open views don't show it. Tested.
8. **Inbox search** (approved):
   - a `data-search` box in the compact header, searching contact name, phone and message text;
   - a live filter on the list (`data-live-filter`, 300 ms) that keeps the current view and channel;
   - message text matched through an `EXISTS` subquery;
   - **timed on a large set:** 2,000 conversations and 200,000 messages, which is where `EXISTS … icontains` slows down. If it's over 300 ms, the fix is a trigram index (`pg_trgm`, a migration). That is reported to you before it's added, and the plan doesn't add it on its own;
   - within the list budget;
   - `/` focuses it.

## Phase 2: room for messages and layout fixes
1. **A compact Inbox header.**
   - One row of about 44 px: a small "Inbox" label, the view menu, the channel filter, Follow-ups and Set-up.
   - It replaces the large title row and the separate toolbar.
   - `next_action_card` moves into the empty pane state (shown when no conversation is open), not above the list.
   - Target: at least 420 px of thread at a 631 px window, measured by the harness.
2. **The conversation header is one row,** with the pane's actions in it. The status badges move beside the name.
3. **One window-closed state.** When the WhatsApp window is closed:
   - the composer itself becomes the template picker: the text box is replaced by "Send a template" with the template list and its fields, in the same place;
   - the separate bar and the second prompt go;
   - the `<details class="cv-window">` explanation becomes a one-line note with a "Why?" help tip;
   - **an account-level problem comes first.** If the last send failed with an account-level code (131042 billing, 131037 display name, 190 reconnect), any template would fail too. The composer then shows that problem and its "Fix it" link in place of the picker, with the picker still reachable under "Try a template anyway";
   - **no approved templates:** a real empty state saying templates must be approved by Meta first, with a link to Akilent's WhatsApp templates page.
4. **No horizontal scrollbar:** find the element 19 px too wide in `.inbox-list-col` (`min-width: 0` on the grid children and long previews), and add a harness check.
5. **No shift on open:** the filter bar moves 12 px because `.inbox-chrome` changes height when `has-conversation` is set. The compact header has one fixed height whether or not a conversation is open, and the harness checks the bar's position before and after.
6. **The template banner isn't cut off:** it wraps to two lines, or is replaced entirely by item 3.
7. **A closed window looks closed:**
   - the reply box is disabled, with its placeholder saying why;
   - the mic and attach buttons are hidden;
   - the mic uses the theme's tokens (the stray blue goes) in every state.
8. **The setup checklist and the welcome tour** never cover the composer: check on the conversation page at 1280 and 390 px.
9. **The ⓘ panel sits beside the thread** (moved up from the old Phase 3):
   - **≥1280 px:** a docked third column, collapsible from the header button, its width draggable and remembered;
   - **narrower:** the shared drawer (`components/_drawer.html` with `akDialog`), replacing `chat.js`'s own drawer code (`infoDrawer`, `_onResizeInfo`);
   - **both** show Phase 0's lazy panel partial.

## Phase 3: look and feel, and control swaps (same rules as Content Studio's look-and-feel pass and Phase 4)
- **Title:** a breadcrumb-style "Inbox › <view>" title from the shared `_cs_title.html` pattern, and the Content Studio type scale and card surfaces in the list, pane and panel.
- **Buttons:** yellow (`btn-primary`) only for each screen's one main action (Send in the composer). Everything else is navy or tonal (`.btn-tonal`), including "Send template" inside its dialog.
- **Motion:** rows, menus and the panel fade in with the htmx-added motion already in `studio.css`, at 150 ms. One hover rule for rows and menu items. None of it under reduced motion.
- **Template picker:** a styled list (name, category, a preview of the body, then its fields) in the composer, replacing `<select id="template-select">`.
- **Channel filter:** a `cvMenu` popover with radio items, the current channel on the button, replacing the `<select>`. It stays a GET form underneath (hidden submit) for use without JavaScript.
- **Panel selects become menus that post:**
  - "Pause automations for…", "Run an automation", "Start a form", "Remind me later".
  - Each item is a submit button in a `cvMenu`. "Custom time" opens a small inline form.
- **Disclosures and links:**
  - the ▾ `<details>` "What automations did" becomes a styled disclosure button with a count;
  - "Why didn't it reply?" becomes an arrow link;
  - Repeat-style on/off choices use `.switch`.
- **Saved replies** stay the existing composer menu, restyled to the `cv-menu` look.

## Phase 4: keyboard
- Rows get `data-row`, so the global J/K works. **In Inbox, J/K open the next or previous conversation in the pane**, as mail clients do; Enter on a focused row opens it.
- **Read state with J/K:**
  - a pane load from J/K passes `peek=1`, which doesn't mark the conversation read;
  - it's marked read (`action=mark_read`) only after the conversation has stayed open 1.5 s;
  - loads you've moved past are cancelled (`inbox_pane.js` already aborts them, and the timer is cleared);
  - pressing J five times quickly marks only the one you stop on. Tested in the harness;
  - a click still marks read at once, as today.
- **Inbox keys, ignored while typing:**
  - R focuses the reply box;
  - E closes the conversation, with an Undo toast. **Undo puts back the state from before the close:** open status, the earlier assignee and the earlier close reason (none if it had none), recorded at close time and sent back by the undo action, not just a plain reopen. The test compares the conversation before E and after Undo;
  - A assigns it to you;
  - Esc returns to the list;
  - all listed under `?`.
- **The N shortcut** becomes `data-shortcut-new="<url>"` on a page, so Content Studio keeps it and Inbox doesn't take it.

## Phase 5: bubbles, composer, live feel (new shared components)
- **Shared chat components:** a bubble with media, voice and attachment states, the delivery line, the AI draft card and the template picker.
  - They live in `templates/components/chat/` and a `chat.css` section, and the Comments inbox uses the bubble too.
  - The `chat()` behaviour stays in `chat.js`.
- **Live feel:**
  - new messages animate in without moving a reader who has scrolled up (the existing `newBelow` pill);
  - a "New messages" divider at the first unread message when a conversation opens, from `is_unread` and the last read message (always loaded, from Phase 0 item 2);
  - a changed list row gets a brief highlight.
- **Polling stays:** the list every 8 s, the thread with backoff. Turning on SSE (the `events` service and nginx) is left to you.

## Phase 5, revised after the prod review of 2026-11-06 (replaces the Phase 5 above; needs a go-ahead)
The review found Phases 0–4 mostly live. The order follows its advice: what you notice in the first ten seconds comes first. Each step is reported on its own.

### 5A: opening a chat feels instant (review item 1)
1. **Split the ~900 ms gap before fixing it.** On prod, opening the inbox only, read `performance.getEntriesByType("navigation")` and the `Server-Timing` header: DNS/TLS/connect, waiting (TTFB), and download, against the server's own `db`/`app` time.
   - The Phase 0 load simulation already showed that 2 workers are 86–91 % busy with three tabs open, so most of the waiting is probably queueing for a worker. If so, the real fix is `GUNICORN_WORKERS` or threads (your decision); the steps below only make it feel faster.
2. **Fewer background requests per tab:**
   - the follow-up badge stops polling on its own and is updated by the list feed's answer, sent out of band;
   - no polling while the tab is hidden (`visibilitychange`); one catch-up check when it comes back.
3. **Instant open:**
   - on click or J, the pane shows the header straight away from the row's data (name, channel, status) plus a thread skeleton;
   - a pane is prefetched when you hover a row for 150 ms, or for the next row after J. Prefetched pages are `?peek=1`, so they never mark read;
   - the last 5 panes opened are kept for 60 s and shown again at once, then refreshed by the thread's normal poll.
   - Harness: from click to header under 50 ms; from click to thread with a prefetch under 100 ms.

### 5B: one look for bubbles, less error noise (review items 2–3)
- **Shared bubble component** (`templates/components/chat/_bubble.html` plus a `chat.css` section), also used by the Comments inbox:
  - one outgoing colour from the brand tokens on every channel; the channel is shown by its icon in the header and on rows;
  - messages sent in a row are grouped (no repeated tail or time), and the time shows on hover or focus;
  - "Sent by AI" becomes a small chip;
  - bubble width is capped at about 640 px on wide screens.
- **Errors:**
  - an account-level problem (billing 131042, display name 131037, reconnect 190) shows once, as a banner above the composer with its "Fix it" link;
  - each failed bubble shows a small "Not sent ⓘ" whose tip gives the plain reason;
  - message-level errors (not account-level) stay on their bubble.

### 5C: template picker (review item 4)
- It opens as a popover over the composer, a bottom sheet below 768 px, so the thread keeps its height.
- Search by name and body text.
- Readable names, worked out when shown ("Download app" from `download_app`). The stored name is unchanged.
- `{{1}}`-style parameters become labelled fields with a live preview of the message.
- Cancel; Send is disabled until a template is chosen and its fields are filled, and is then the screen's one yellow button.
- Works without JavaScript: the existing form underneath stays.

### 5D: rows, empty pane, wording (review items 5–7)
- **Rows:** the channel icon sits on the avatar's corner, so each row is two lines (about 64 px). Unread shows as bold text plus a dot. Close, assign to me and snooze appear on hover or focus, through the same POSTs as the pane, with the Undo from Phase 4. Swipe is left out: a tap target is safer than a gesture on a list that refreshes every 8 s.
- **"Quiet":** keep the word with a help tip, or rename it ("Waiting on them"). Your choice.
- **Empty pane:** counts that need you today (needs attention, missed, follow-ups due), each a link to its view, and one line of shortcut hints.
- **Wording:** "24-hour window closed" on every channel; "Why?" becomes a `help_tip`; tooltips on the filter, follow-ups and setup icons; the bell becomes a clock-check icon labelled "Follow-ups".

### 5E: live feel (review item 8, the old Phase 5)
- New messages slide in without moving a reader who has scrolled up (the existing `newBelow` pill).
- A "New messages" divider sits at the first unread message.
- A changed list row gets a brief highlight.
- A polite `aria-live` announcement for a new inbound message.
- None of it under reduced motion.
- Typing indicator: Meta sends no customer-typing webhook on WhatsApp, Instagram or Messenger, so there is nothing to show for customers. Teammate presence already exists.
- SSE stays your infrastructure step.

### 5F: responsive spec, checked in the harness
- **Under 768 px:** one pane at a time.
  - The thread slides in over the list and has a back button.
  - The composer stays above the keyboard (`visualViewport`) and respects safe areas.
  - Details open as a full-screen sheet.
- **768–1279 px:** two panes, with details as a drawer (as now).
- **1280 px and up:** two panes; the docked third column from about 1440 (the Phase 2 deviation, kept so the chat isn't squeezed to 268 px).
- **Harness `QA_RESP`:**
  - sizes 360, 390, 768, 1024, 1280 and 1440 px, in light and dark;
  - tap targets at least 44 px (24 px with spacing on desktop pointers);
  - no sideways scroll;
  - thread height at each size, with targets per size;
  - thread height with the error banner and with the template picker open.

## Phase 6: the details panel answers who, where, what next (proposed 2026-11-06; needs a go-ahead)
The panel (`templates/conversations/_conversation_panel.html`, `views._panel_context`) is a list of tools in no order.
- **Missing:** status, assignee, channel, the reply window and an account problem are elsewhere or absent.
- **Repeated:**
  - the name shows twice (header and Name row);
  - "Only your team sees this" shows three times.
- **Unclear:** "Automations: Pause here ▾" doesn't say whether automations are on, and "Why didn't it reply?" sits under "Nothing yet".
- **Out of order:** the follow-up and notes sit between tags and the interested state.
- **Always open:** the follow-up form is expanded even when none is set.

The new order follows what someone opening a chat wants to know. Sections keep that order on every chat and only get shorter when empty.

### The sections
1. **Header (who):** avatar with the channel mark, name, phone with copy, email only when there is one. Under it: "Customer since Oct 2026 · 2 conversations · View profile". The Name, Phone and Email rows go.
2. **Status (where it stands):** one-line rows of label, value and action.
   - **Status:** Open / Waiting on them / Closed, with Close or Reopen. It posts the existing actions, with Close's Undo.
   - **Assigned:** the person or "Unassigned", with "Assign to me" (or "Unassign").
   - **Reply window (WhatsApp, Instagram, Messenger):** "Open · closes in 6h" or "Closed 4 days ago". When closed, "Send a template" opens the composer's picker (Phase 5C).
   - **Automations:** "On", with a "Pause for…" menu. "Paused until 14:00", with Resume. "Paused" with no end, with Resume. Run one stays as a menu here.
   - **Account problem:** shown only when there is one, as a red line ("WhatsApp billing problem · Fix it"), from chat.js `accountProblem`, so it's visible with the thread scrolled anywhere.
3. **Next step (what to do):** one small label for the section, "Only your team sees these", said once.
   - **Follow-up:**
     - none set: one "Set a follow-up" button that opens the chips, note and Save in place;
     - one set: "Due tomorrow 09:00 · note", with Done, Change and Remove.
   - **Notes:** the latest note, with "2 more" expanding the rest, and "Add note" opening the box.
4. **Customer value (sales):**
   - **Where they stand:** Interested / Lead / Customer (open lead or deal), or "Not tracked · Track as interested".
   - **Orders:** open orders and the paid total, linking to the orders.
   - **Tags:** chips with "+", which opens the input; "No tags · Add" when there are none.
5. **Activity (history), collapsed by default, with counts:**
   - what automations did;
   - forms (in progress, with Stop waiting; Start a form);
   - earlier conversations (up to 5, linked);
   - "Why didn't it reply?" only when automations did nothing on the customer's last message.
6. **Danger zone:** at the bottom, set apart, "Delete conversation" with its explanation, behind the confirm it has now.

### Rules
- **Section layout:** each has a small uppercase heading, then label · value · action rows. A form shows only after its action is clicked, and Esc or Cancel folds it back.
- **Empty sections:** one line with their action ("No tags · Add", "No follow-up · Set one").
- **Without JavaScript:** every action is still a real form. The folded forms are `<details>` underneath, so the panel page (`panel_page.html`) still works.
- **After an action:** the panel and the header swap as today (out of band), with `inbox-refresh` for the row.
- **Speed:** the panel keeps its budget.
  - Currently 21 queries, against a target of 20.
  - The paid total and earlier conversations add at most one aggregate query and one 5-row query. If that goes over budget, the open order and the paid total merge into one aggregate.
- **Docking:** where the details are docked (about 1440 px and wider), Status and Next step sit above the fold at 900 px tall. Docking stays at about 1440, the Phase 2 decision, unless you want it at 1280; there the chat would be about 270 px.

### Tests
- **Unit, the sections and their order:**
  - the sections in order, on a chat with everything and on an empty one, where the empty ones collapse to one line;
  - the name once;
  - "Only your team sees these" once;
  - email only when present.
- **Unit, each state:**
  - Status for open, waiting on them and closed, with the right action;
  - the reply window open and closed with times, and "Send a template" only when closed;
  - automations on, paused until, and paused with no end, with Pause or Resume;
  - Follow-up none and set;
  - notes: the latest, and the count of the rest;
  - "Why didn't it reply?" only when automations did nothing.
- **Unit, the rest:**
  - the paid total and earlier conversations;
  - the account problem line bound to `accountProblem`;
  - every action still a form on the no-JS panel page;
  - the query budget for the panel.
- **Harness (`QA_PANEL`):**
  - at 1440 and 900 tall, Status and Next step are visible without scrolling;
  - each folded form opens in place and Esc folds it;
  - "Send a template" opens the picker;
  - Pause, then Resume;
  - light and dark at 390 (full-screen sheet) and 1440 (docked), with contrast and 44 px touch targets.

## Critical files
- **Views:** `apps/conversations/views.py` (`conversation_detail`, `_inbox_context`, `messages_feed`, a new `panel` view) and `apps/conversations/urls.py`.
- **Templates:** `templates/conversations/inbox.html`, `_inbox_body.html`, `_conversation_pane.html` (split into pane plus `_conversation_panel.html`), and the new `templates/components/chat/*`.
- **JS:** `static/js/chat.js` (earlier messages, drawer swap, keys), `static/js/inbox_pane.js` (J/K open, resizable columns), `static/js/app.js` (`data-shortcut-new`).
- **CSS:** `assets/app.css` (the `.inbox-*` and `.cv-*` sections), then the Tailwind rebuild; `static/css/controls.css`.

## Verification
- **Unit tests:**
  - `test_inbox_speed.py` budgets for each request in the Phase 0 table;
  - a send that fails after the fact makes the feed answer 200 with "Not sent";
  - panel actions also update the header and the list row;
  - the pane opens from the first unread message when it's older than the last 50;
  - All = open total, All + Closed = everything, a closed chat with a follow-up stays in Closed, and a quiet open chat says Quiet, not Closed;
  - a failed last send marks the row;
  - every mapped error code renders plain text (no "(error ");
  - a closed chat shows Closed in its row and Reopen in ⋯;
  - every view count shows 0;
  - the channel filter has no unconnected channels;
  - `messages_feed?before=`;
  - the panel partial and its actions re-rendering the panel;
  - templates only when the window is closed;
  - one composer state when the window is closed;
  - the channel menu filters with and without JavaScript;
  - close then undo restores the status, the assignee and the close reason;
  - J/K peeks don't mark read, and only a 1.5 s stay does;
  - the account-level error and the no-templates empty state in the closed composer;
  - the existing inbox tests.
- **Full suite:** `poetry run pytest -n 4`, `ruff`, `ruff format --check`, `mypy .`, `makemigrations --check`.
- **`tools/qa/inbox_qa.mjs`, extended:**
  - thread height at 631 px;
  - no horizontal scroll;
  - no filter bar shift on open;
  - "Show earlier" keeps position;
  - J/K open conversations;
  - the panel docks at 1280 and is a drawer at 1024;
  - widths remembered;
  - no console errors;
  - light and dark mode;
  - `area_scan /inbox` at 360 px with 0 issues.
- **Load and search timing:** the Phase 0 load script (badge and checks under 2 workers), and the search timing on 200,000 messages, both reported with numbers.
- **Speed:** before and after TTFB on the dev server for the list, the pane and the full page, reported with the query counts.
- **After your deploy:** a read-only check on akilent.com: open conversations and the panel, nothing else.
