# Content Studio: make it feel like an app (plan of 2026-11-03)

## Context
Two walkthroughs of Content Studio on akilent.com found that it works but feels like an old multi-page site. The causes:
- Clicks don't respond until the next page arrives (50 to 780 ms).
- Every edit opens a separate page that loses your place.
- Saves reload the page.
- Filters behave inconsistently.
- There are no confirmations or undo.
- The controls are browser defaults.
- The month calendar has gaps.

The user chose **Content Studio first, built from reusable pieces**, and **targeted control swaps** (not a custom dropdown for every select).

**Most of the plumbing already exists. Reuse it, don't rebuild it:**
- **Content-only swaps:** `ShellMiddleware` in `apps/core/htmx.py`, plus `static/js/app.js` L1114 `htmx:beforeSwap`, which sets the target to `#main`. View transitions use `.ak-page-vt`, and `#nav-progress` shows progress.
- **Overlays:** `{% modal %}` and `{% drawer %}` (`apps/core/templatetags/ui.py`, `templates/components/_modal.html` and `_drawer.html`). `window.akDialog` (app.js L302–410) handles the stack, Esc, inert siblings and focus return.
- **Confirmations:** `$store.confirm` and `data-confirm` / `hx-confirm` (app.js L635–707).
- **Toasts:** `window.toast(type, msg, {action})` (app.js L110). The `X-Toast` header comes from `hx_toast()` in `apps/accounts/utils.py`, and Django messages already become toasts (base.html L365).
- **Helpers:** `is_background(request)` in `apps/core/htmx.py` L40. Skeleton styles are in `assets/app.css` L1073. Motion tokens: `--duration-fast` (150ms) and `--ease-emphasized`. Radius tokens: `--radius-control` and `--radius-surface`.
- **Preload extension:** already loaded on `<body>`.

**Found while exploring (the real cause of the slowness):**
- `media_library.html`, `media_edit.html` and `post_new.html` load `<script src>` tags that aren't marked `data-shell-safe`.
- `_needs_full_load` therefore turns every visit to Media and the composer into a **full page reload** (`HX-Refresh`/`HX-Redirect`).
- The month calendar only places posts by their scheduled time. A post sent with **Publish now** has none, so posts published on Oct 26 and 27 don't appear.

## Rules that stay binding
- These must keep passing:
  - the UI quality programme's guards (drift ratchet at 0, `test_alpine_fields`, no `x-init="init()"`);
  - `test_shell_swap`, `test_htmx_forms` and the comment rules;
  - dark-mode tokens, the 16px phone inputs (`sm:` prefix), and the 360px crawl.
- **Every URL stays real:** opening it directly or refreshing gives the full page, and forms still post without JavaScript.
- **No fake states:** a toast says "Saved" only after the server saved it, and Undo is offered only where the server can undo.
- **Gates:** each phase is built, verified (tests, then the browser harness `tools/qa/publishing_qa.mjs` extended), and reported. Approving this plan authorises Phases 0 and 1; each later phase needs its own go-ahead. No commits.
- **Responses that vary by htmx headers.** Every view that returns different HTML for the same URL (overlay partials, results regions) sets `Vary: HX-Request, HX-Target` and `Cache-Control: no-store` on the partial, so neither the browser nor CloudFront can serve a bare fragment on refresh or Back. A small `partial_response()` helper in `apps/core/htmx.py` does both; a test checks the headers.
- **Reduced motion:** under `prefers-reduced-motion`, no view transitions (already set in app.js), no skeleton shimmer, and no drawer slide.
- **Touch and small screens:**
  - hover-only controls stay visible under `@media (hover: none)`;
  - drawers open full width below `sm` (the existing `_drawer.html` already does);
  - popovers become bottom sheets on phones.
- **Analytics keep counting.** A boosted navigation or an overlay opening sends a virtual pageview to gtag and `session-tracker.js`, with the same path the full page would report. Tested by checking the tracker call in the harness.
- **First step:** move the older roadmap below into `docs/plans/content-studio-competitive-roadmap.md` (memory updated to point there), so this plan stays short.

## Phase 0: bugs found in the walkthrough
1. **Published posts on the calendar.**
   - `_calendar_entries` (`apps/publishing/views.py`) also takes channels with no `scheduled_at` whose `published_at` falls in range, placed at `published_at`. The post-level fallback does the same.
   - Week, month and list views all use it.
2. **Today stands out** in the week and month views: a tinted cell and a "Today" pill, using tokens only.
3. **The plan editor's back link returns to where you came from.**
   - The plan-edit and plan-new views accept `?next=`, checked with `url_has_allowed_host_and_scheme`.
   - Calendar and planning links pass it.
   - Saving returns there.
4. **The picture editor has room.** The crop area gets an explicit height (`min(70vh, 40rem)`), so Cropper never sizes itself to a 100px box. The CORS message from the last round stays.

## Phase 1: navigation that responds instantly
1. **Stop the forced full reloads.** Making `media_library.js`, `cropper.min.js` and `composer.js` safe to run more than once means all of:
   - **Alpine:** the factories register with `Alpine.data` whether or not Alpine has already started (registered on `alpine:init`, and called directly when `window.Alpine` is already running), and never twice.
   - **Listeners:** `document`/`window` listeners are added once, behind a module flag, or removed in the component's `destroy()`.
   - **Cropper:** loaded once (`window.Cropper` guard), and created only once its image is visible and loaded (`$nextTick` after open, plus the image `load` event). A hidden container measures 0×0, which is the same cause as the 100px box.
   - Then the scripts are marked `data-shell-safe`.
   - **Tests:**
     - boosted navigation to Media, the media editor and the composer swaps `#main` without `HX-Refresh`;
     - the harness visits each page twice in a row with no duplicate listeners (a counter on a test hook) and no console errors.
2. **Instant tab feedback in `_subnav.html`.**
   - Add an Alpine `@click` that moves `aria-current` and the active classes to the clicked tab right away. It reverts to the server's tab on **any** unfinished navigation:
     - `htmx:responseError`, `htmx:sendError`, `htmx:timeout` and `htmx:abort`;
     - a newer navigation starting;
     - `popstate`.
     - The server-rendered subnav in the swapped page is always the source of truth.
   - **Dropped (2026-11-04):** `preload="mouseover"` on the tabs. A click that landed while a hover preload was in flight was dropped by htmx, so the tab never opened.
   - **Dropped (2026-11-04):** a `view-transition-name: cs-tab` underline. The tab is already active before the page transition starts, so there is nothing left to slide.
3. **Skeletons for slow loads.**
   - Add `data-skeleton="grid|table|calendar|form"` on Content Studio links.
   - In app.js, if a boosted request takes more than 150 ms, replace `#main`'s content with the matching `<template id="skeleton-…">` from base.html, built from the existing `.skeleton-*` classes.
   - Never show one for fast loads, so there's no flash.
4. **A steady header.** Every Content Studio page uses one header layout (title row with an optional action slot of fixed height), so the tab row no longer jumps.
5. **Server speed, not only skeletons.**
   - Budget: each Content Studio tab renders in under 200 ms on the server with realistic data.
   - Add `django_assert_max_num_queries` tests for Overview, Library, Calendar (week and month), Media and Planning, at a count that doesn't grow with the number of rows.
   - Find the ~600 ms first-load time-to-first-byte with a timing run on the dev server, and fix what's slow (N+1s, the per-request `reconciling_count`, the worklist's loop over posts).
   - (No preload on the tabs: see item 2.)

## Phase 2: drawers and modals instead of separate pages
**One overlay host, one server helper, used everywhere.**
- **base.html:** one `#overlay` host, an Alpine `akOverlay` built on `akDialog`. It shows a right-hand drawer or a centred modal, chosen by the loaded content's `data-overlay="drawer|modal"` (sizes `md`/`lg`/`xl`).
  - It loads with `hx-get` into `#overlay-body`, and app.js's `beforeSwap` must leave that target alone.
  - It pushes its own history entry (`history.pushState({akOverlay: url})`; htmx ignores states without its own key). Back closes it, and closing pops it. Esc and backdrop clicks also close it.
  - **History:** `#main` already carries `hx-history-elt`, and the overlay host sits outside it, so a snapshot can't capture an open drawer. As a second guard, `htmx:beforeHistorySave` closes and empties the overlay.
  - **A boosted navigation started while the overlay is open** closes it with `history.replaceState` instead of `history.back()`, so no stray entry is left behind.
  - **Swaps:** app.js `beforeSwap` leaves `#overlay-body` as the target instead of retargeting to `#main`. It keeps the existing rule that 400 and 422 responses swap, so validation errors render inside the overlay. A test covers a 422 in the overlay.
  - **Focus** returns to the row or button that opened the overlay (akDialog's focus return, given the opener).
- **`apps/core/htmx.py`: `overlay_or_page(request, partial, page, context)`.**
  - When the request targets `#overlay-body`, it returns the partial wrapped in `components/_overlay_body.html` (title, body, footer slot). Otherwise it returns the full page, which includes the same partial.
  - Each converted page is split into `_x_body.html` plus its page wrapper.
- **Saving inside an overlay:**
  - Forms `hx-post` into `#overlay-body`.
  - A validation error re-renders the partial (422).
  - Success returns `204` with `HX-Trigger: {"overlay-close": true, "cs-changed": "<what>"}` and a toast through `hx_toast`.
  - **One refresh request, not one per section.** The save response carries the updated regions out of band (`hx-swap-oob`) for the page that opened the overlay (calendar grid, backlog, Planning tiles, Library table, Media grid). The view knows which page that is from an `X-Akilent-From` header. When that header is missing, a single `hx-get` of the current page swaps every region with `hx-select-oob`.
- **Order of work:**
  1. Build the host with **one drawer (post detail) and one modal (Add idea)**.
  2. Verify history, caching headers, focus, 422 handling, analytics and phone layout with those two.
  3. Only then convert the rest.
- **J/K order:** the drawer's previous and next items come from the `[data-row]` rows in the current filtered list on screen, never from a server query, so it follows what the user is looking at.
- **Converted:**
  - **Drawers:**
    - post detail: from Library, Overview's "Needs you", Sales, Channels and calendar entries. It shows the summary, per-channel status and the main actions (Edit and fix, Retry, Approve), plus "Open full page" for everything else; J/K step through rows.
    - plan item edit;
    - posting slots;
    - caption templates and hashtags, also opened from the composer's caption box.
  - **Modals:**
    - Add idea / Plan an idea, with the date filled in from the calendar;
    - Manage pillars and Manage themes;
    - CSV import, as steps (upload, check, confirm) in one modal;
    - the media editor, as an almost full-screen modal;
    - the "How posts are credited" help on the Sales page.
- **Stay full pages:** the composer and the main tabs. A calendar day's "+" opens Add idea; a quick post dialog is left out of this plan.

## Phase 3: live filters, edits in place, toasts with undo (built 2026-11-05, Entry 20)
1. **One live-filter behaviour (`data-live-filter` on a GET form, in app.js).**
   - Text fields search after 300 ms; selects and checkboxes apply on change.
   - Results are swapped with `hx-get`/`hx-select` on the results region, and the URL is updated with `hx-replace-url`.
   - The Filter and Show buttons go. A visually hidden submit stays for Enter and for use without JavaScript.
   - Active filters show as removable chips (`publishing/_filter_chips.html`).
   - Used in Library (which also merges Status and "Where it stands" into one control), Media, Calendar and Sales.
   - **Old links keep working.** `?bucket=failed` and every other bucket from Overview tiles, alerts and emails are accepted as aliases of the merged control. A test opens each `bucket_url()` key.
   - **Focus** stays in the search box while the results region is replaced. The harness checks it.
2. **Media cards save in place.**
   - Save (tags and name) and Remove become `hx-post`, returning `_media_card.html` swapped `outerHTML`.
   - Remove first shows a confirm dialog that states what will happen (blocked, archived or deleted, from a dry run of `media_library.remove`).
   - An archive gets an **Undo** toast, which restores it through the existing `restore` action. A real delete is confirm-only, because the file is gone.
3. **Toasts with actions.** app.js listens for `HX-Trigger` `{"toast": {"type", "message", "undo": url}}` and calls `window.toast(..., {action: Undo → hx-post url})`. Add a Python helper `hx_trigger_toast(response, type, message, undo_url=None)` next to `hx_toast`.
4. **Destructive actions get a confirm dialog with the outcome spelled out:** Delete drafts, Cancel scheduled, and Channels → Disconnect. Single-item Library actions get an undo toast where the backend can undo (cancel can be undone by reopening).
5. **Undo is a server rule, not a toast timer.**
   - **Media restore** stays available from the Archived view for as long as the file exists. The toast is only a shortcut to it.
   - **Undoing a cancelled post:**
     - if every channel's time is still in the future, it goes back to scheduled, through readiness and approval as usual;
     - if any time has passed, the post comes back as a **draft** and the toast says so.
   - Tests cover both cases.

## Phase 4: targeted control swaps (built 2026-11-05, Entry 19)
- **Segmented control** (`components/_segmented.html`: a radio group, keyboard accessible) for: composer Format, Media Type, Calendar Week/Month/List, and Sales period. The composer's 6 formats wrap to a 3×2 grid below `sm`, so nothing scrolls sideways at 360px.
- **Toggle switch** (`.switch`, a checkbox with `role="switch"`) for: Require review, the weekly email, Bio "Use my own colours", and Repeat on/off. CSS goes in `assets/app.css` and Tailwind is rebuilt.
- **Date-range popover** for the Library dates: presets (7, 30 or 90 days, this month) plus two native date fields inside, which stay accessible on phones.
- **Long lists keep a styled native `<select>`.**
- **Media cards:** Edit and Remove appear as icon buttons over the picture on hover and focus. The ▶ Details forms go; tags and name are edited in the editor modal. The grid never changes height.

## Phase 5: calendar, media grid, shortcuts, finish (built 2026-11-05 with the Media gallery redesign, Entry 19; empty states and radii swept on Media only)
1. **Calendar:**
   - A "+" on a hovered or focused day opens Add idea with that date.
   - Plan items can be dragged to another day, through a new `plan_move` endpoint, the same native drag-and-drop and Move-form pattern posts already use. No Sortable.js.
   - Adjacent-month days show their items, which comes from the Phase 0 fix.
2. **Media grid:** multi-select with a bulk bar (tag, archive or remove, with the outcome confirm), a lightbox preview (a modal with `<img>`/`<video>`), and uploads as placeholder cards with progress in place.
3. **Shortcuts (app.js, ignored while typing):**
   - `N` new post (Content Studio pages);
   - `/` focuses `[data-search]`;
   - `J`/`K` move between `[data-row]` items and Enter opens them;
   - `?` lists the shortcuts in the command palette;
   - Esc already closes overlays.
4. **Visual finish across Content Studio:**
   - underlined text links become ghost buttons or arrow links;
   - lighter table row borders with row hover;
   - radii from the tokens only;
   - 150 ms transitions;
   - empty states use `components/empty_state.html` with an icon and a primary action.

## Critical files
- **Shared JS:** `static/js/app.js` (beforeSwap exception for the overlay, skeletons, live filter, toast trigger, shortcuts).
- **Shared templates:** `templates/base.html` (overlay host, skeleton templates), new `templates/components/_overlay_body.html` and `_segmented.html`.
- **Shared Python:** `apps/core/htmx.py` (`overlay_or_page`), `apps/accounts/utils.py` (toast trigger helper).
- **Styles:** `assets/app.css`, then the Tailwind rebuild.
- **Content Studio views:** `apps/publishing/views.py` (calendar entries, plan views with `next`, post detail partial), `media_views.py`, `scheduling_views.py`, `import_views.py`, `sales_views.py`.
- **Content Studio templates:** `templates/publishing/_subnav.html`, `calendar.html`, `_cal_month.html`, `library.html`, `media_library.html`, `planning.html`, `post_detail.html` (split into body plus page), `plan_item_form.html`.
- **Content Studio JS:** `static/js/media_library.js`, `static/js/composer.js`.

## Verification
- **Unit tests, per phase:**
  - calendar entries for posts sent with Publish now;
  - `next` handling;
  - `overlay_or_page` returning the partial only for `#overlay-body`, and the full page otherwise;
  - an overlay save returning 204 with `HX-Trigger`;
  - live filters returning the results region;
  - a media card save returning the card;
  - remove → archive → undo restoring it;
  - boosted Media and composer navigation without `HX-Refresh`.
- **Full suite:** `poetry run pytest -n 4`, `ruff`, `ruff format --check`, `mypy .`, `makemigrations --check`.
- **Browser harness** (`tools/qa/publishing_qa.mjs`, extended, against the dev server):
  - the tab is marked active on click: measured in the page with `performance.mark` from the click to the class change (under 50 ms), not wall-clock time around a screenshot;
  - a skeleton shows only on slow loads (simulated with a delayed response);
  - the active tab reverts after a failed or aborted navigation;
  - focus returns to the opener after an overlay closes, and stays in search while results update;
  - a virtual pageview is recorded for each boosted navigation and overlay;
  - a drawer opens from Library, J/K step through rows, Back closes it, and the scroll position is kept;
  - opening the drawer's URL directly gives the full page;
  - Add idea from a calendar day puts the card on that day without a reload;
  - live search updates without a click;
  - Undo after archiving restores the card;
  - no console errors;
  - dark mode, and the 360px crawl with 0 issues.
- **Finally:** a check on akilent.com with the test workspace after each deploy.

---

The older competitive roadmap (Phases stopgap to 5) now lives in `docs/plans/content-studio-competitive-roadmap.md`.
