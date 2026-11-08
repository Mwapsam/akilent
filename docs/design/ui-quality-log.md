# UI quality log

Evidence for the UI/UX quality programme ("confidence in the platform"). The plan is at
`~/.claude/plans/audit-the-ui-ux-of-lazy-thompson.md`.

Each entry records:
- the exact code that was tested
- what was run and how
- which results are automated and which are manual
- the three gate sign-offs: implementation, verification, acceptance

An automated scan is supporting evidence. It never certifies WCAG conformance.

---

## Entry 0: Baseline (before any Phase 0 change), 2026-10-25

### Tested code

| | |
|---|---|
| HEAD | `555ce868fac5397a29e0cc2e47b0f37e45b613ef` |
| Tracked changes | none (`git diff HEAD` SHA-256 `e3b0c44298fc1c14…`, the empty diff) |
| Untracked | `tools/qa/ui_scan.js` (SHA-256 `8ef04567a9874ea4…`, the scanner) and `docs/design/qa/` (evidence) |

### Environment

- **Server:** local `runserver` with `DEBUG`, SQLite `db.sqlite3`. Migrations were applied first: 13 were pending on the local DB. A backup is outside the repo at `../automator-db-backups/db.sqlite3.bak-2026-10-25-pre-uiqa`.
- **Redis:** a throwaway container, `akilent-uiqa-redis` on `127.0.0.1:6379`, passed in through inline env vars. `.env` still points at the compose host `redis`.
- **Login:** local QA user `ui-qa@akilent-qa.invalid`, an admin of the seeded *Demo Boutique (demo workspace)*. Credentials are in the gitignored `.env` (`QA_UI_*`).
- **Browser:** Chrome, maximized window 1536×639 CSS px, DPR 1.25. Themes were forced with `data-theme`, and transitions were disabled during each scan.

### Automated results (`tools/qa/ui_scan.js`, desktop 1536 px)

Contrast is shown as failing text elements / text elements checked. "<24px" counts interactive targets under the WCAG 2.2 AA 24×24 floor. Horizontal overflow was **0 on every screen** at desktop width.

| Screen | Light contrast fail | Dark contrast fail | <24px targets |
|---|---|---|---|
| `/dashboard/` | 5 / 51 | 7 / 79 | 1 |
| `/inbox/` | 7 / 63 | 11 / 63 | 1 |
| `/contacts/` | 18 / 67 | 4 / 67 | 13 |
| `/publishing/` | 21 / 70 | 5 / 70 | 1 |
| `/automations/` | 8 / 110 | **57 / 110** | 13 |
| `/insights/` | 18 / 160 | **44 / 160** | 3 |
| `/orders/` | 5 / 48 | 7 / 48 | 1 |
| `/channels/` | 3 / 41 | 5 / 41 | 0 |
| `/billing/plans/` | **44 / 217** | **59 / 217** | 1 |
| `/logs/messages/` | 3 / 44 | 4 / 44 | 1 |
| `/sales/` | 7 / 58 | 4 / 58 | 4 |

### Failures that repeat on every screen (shared shell, so they are fixed once in Phase 0)

| Element | Light | Dark | Finding |
|---|---|---|---|
| Topbar search placeholder "Search…" | 1.95:1 | | F3 |
| Topbar workspace name | 3.02:1 | | F3 |
| `+ Send` primary button text | | **1.77:1** | F1 (confirmed in render) |
| Page-header description `text-gray-600` | | 3.67:1 | F3 (hard-coded gray doesn't flip) |
| Muted meta `text-gray-500` (dates, phone numbers, stat labels) | 3.30:1 | 3.30:1 | F3 |
| Empty/placeholder `text-gray-400` ("Nothing is waiting on you.", "—") | 2.14:1 | | F3 |

### Area-specific failures

- **Automations (dark):** warning lines `text-red-700` at 2.59:1, and inline links at 15px height.
- **Insights (dark):** `text-gray-700` body at 2.07:1, `.stat-label` at 3.30:1.
- **Billing:** "CURRENT PLAN" `text-gray-400` at 2.14:1, "· Trialing" at 2.52:1, inactive billing-period tabs at 3.30:1, and `a.text-blue-600` at 2.69:1 in dark (stock palette, the M6/B finding).
- **Orders:** count badges `opacity-75` at 1.52:1 in dark.
- **Contacts:** 13 sub-24px targets (16px row checkboxes, 20px-tall sort-header links).

### New finding from browser QA

**B1, brand inconsistency (Medium, confidence):**
- `SiteSettings.app_name` defaults to `"Automator"` (`apps/core/models.py:41`), and 68 templates fall back to `default:"Automator"`.
- With the row unset, the sidebar and login page say "Automator" while page copy and titles say "Akilent". See the screenshots below.
- Fix in Phase 3 (accounts/auth). The model default needs a migration and goes through the normal review.

### Mobile baseline: 390×658 (iPhone frame), same tree

**How it was measured.**
- The user enabled a mobile-simulator Chrome extension that renders the page in an iframe.
- The app correctly refuses framing, so a **local-only QA proxy** (`127.0.0.1:8001 → :8000`, outside the repo, in the session scratchpad) strips only `X-Frame-Options` and CSP `frame-ancestors` in transit. **The app's security settings are unchanged.**
- The scanner runs against the same-origin frame (`__uiScan({win})`).
- **Limits:** this is desktop Chrome rendering at phone width. It does not emulate a coarse pointer, iOS input zoom, the virtual keyboard, safe areas or Safari's toolbar. Those still need the real-device pass.
- Numbers are taken after the welcome tour was dismissed. With the tour open, every page also gets its overlay. Skip persisted across a reload (checked).

| Screen | Overflow | Light contrast | Dark contrast | <24px targets | Inputs <16px |
|---|---|---|---|---|---|
| `/dashboard/` | 0 | 2 / 49 | 6 / 49 | 3 | 0 |
| `/inbox/` | 0 | 3 / 31 | 9 / 31 | 0 | 0 |
| `/contacts/` | 0 | 15 / 38 | 3 / 38 | 13 | 2 |
| `/publishing/` | 0 | 18 / 41 | 4 / 41 | 1 | 0 |
| `/automations/` | 0 | 5 / 81 | **56 / 81** | 5 | 1 |
| `/insights/` | 0 | 15 / 131 | **43 / 131** | 2 | 3 |
| `/orders/` | 0 | 2 / 19 | 6 / 19 | 1 | 0 |
| `/channels/` | 0 | 0 / 12 | 4 / 12 | 0 | 0 |
| `/billing/plans/` | **page 440px wide** | 41 / 188 | 58 / 188 | 0 | 0 |
| `/logs/messages/` | 0 | 0 / 15 | 3 / 15 | 1 | 2 |
| `/sales/` | 0 | 4 / 29 | 3 / 29 | 4 | 0 |

### Mobile findings

**M6 confirmed (High for billing):** the `/billing/plans/` comparison `table.table` has no wrapper.
- The page scrolls sideways.
- Feature descriptions wrap one or two words per line.
- The **Business** column is cut off.
- Plan names scroll out of view, so ticks lose their meaning.

The page where people decide what to pay for is hard to read on a phone. See `billing-plan-table-390.png`.

**M1 confirmed:** 14px controls on phone:
- Customers: search and status filter
- Insights: period, goal metric and goal target
- Message history: search and status filter
- Automations: the pause-hours select
- the inbox reply `textarea`

Each of these triggers iOS zoom on focus.

**Inbox thread on phone works well:**
- the back button is present
- the reply box is pinned at the bottom
- the send/mic button is 44×44
- the closed 24-hour window is explained in plain words

Still open there:
- (a) the textarea is 14px (M1)
- (b) the floating onboarding "1/6" bubble overlaps the thread in the thumb zone beside the mic button, on every screen
- (c) the reply box stays typeable while the window is closed. Phase 1 checks whether sending is truly blocked (truthfulness).

**First-run:** the welcome tour title reads "Welcome to Automator" (B1 again, on the first screen a new user sees).

**Scanner caveat found:** `sr-only` text is counted in contrast (for example "Finish setup" at 1.77:1). The scanner fix will skip clipped 1px elements, so a few counts above are overstated by 1.

### Screenshots

- `docs/design/qa/baseline/automations-dark-1536.jpg`
- `docs/design/qa/baseline/billing-plans-dark-1536.jpg`
- `docs/design/qa/baseline/billing-welcome-tour-390.png`
- `docs/design/qa/baseline/billing-plan-table-390.png`
- `docs/design/qa/baseline/inbox-thread-390.png`

### Not measured yet

- 360px and 768px widths (the simulator was set to an iPhone 390 frame)
- landscape
- 200% zoom
- the real-device pass

### Manual review

None yet. This entry is automated measurement plus a visual check of the two screenshots.

### Gates

Baseline only; no gates apply.

---

## Entry 1: Phase 0 (foundation), 2026-10-25

### Tested code

| | |
|---|---|
| HEAD | `555ce868fac5397a29e0cc2e47b0f37e45b613ef` (unchanged; nothing committed) |
| Tracked changes | 38 files, +1149 −188; `git diff HEAD --binary` SHA-256 `f26af484ebc18bae…` |
| Untracked files (SHA-256 prefix) | `apps/core/template_drift.py` `0be7d937…` · `apps/core/tests/drift_baseline.json` `f321192e…` · `apps/core/tests/drift_exceptions.toml` `50e59739…` · `apps/core/tests/test_template_drift.py` `cace85fa…` · `templates/components/_drawer.html` `a7209151…` · `templates/components/error_state.html` `ce673c8e…` · `tools/qa/` `472c6fa9…` · `docs/design/qa/` `82c5c8a6…` |

This log file is excluded from its own hash.

### What changed

| Plan step | Change | Files |
|---|---|---|
| 0.1 Tokens | Added `--color-focus-ring` (dark: brand-400), `--color-on-amber`, duration, radius, `--shadow-tooltip`, `--z-banner` and `--safe-*` tokens. Every focus outline uses the ring token. | `assets/app.css` |
| 0.2 Contrast | `.btn-primary` text uses on-amber. Light `--tone-coral-fg` moved from coral-600 to coral-700: 4.31→5.9:1 on canvas, found by the new token test. Component greys (`.inbox-tab`, `.stat-label`, chart legends and others) now use tokens. **Dark-theme text bridge:** hard-coded gray, red, blue, green and amber text utilities map to theme tokens in dark mode only. It's a single class in specificity, so `hover:` variants still win. It's temporary: the ratchet removes its users. | `assets/app.css` |
| 0.3 States | `:active` on secondary, ghost and danger-ghost buttons. Inputs get hover, disabled (dashed, muted), read-only and `aria-invalid` states. | `assets/app.css` |
| 0.4 Nav | `exclude=` on `nav_active`/`is_nav_active`, mirrored in JS (`data-nav-exclude`). Inbox no longer lights up on Follow-ups and Forms. | `nav.py`, `_nav.html`, `_nav_item.html`, `app.js` |
| 0.5 Dialogs | One dialog stack (`window.akDialog`): initial focus on the first control, topmost-only Esc, background `inert` except `[data-dialog-exempt]`, focus returned to the opener (or the page heading if the opener is gone), cleanup after swaps. Modal and confirm are bottom sheets below sm. The confirm is `role="alertdialog"` with `aria-describedby`, and focus starts on Cancel. New `{% drawer %}`. | `app.js`, `_modal.html`, `confirm.html`, `_drawer.html`, `ui.py` |
| 0.6 Errors | Opt-in `data-error-region` plus `components/error_state.html`. Failures are classified (offline, timeout, network, 403, 404/410, 409, 429, 5xx). Retry is offered only for loads. Saves keep the form, and their copy states only what is known ("may or may not have saved" when the connection dropped). Stale and superseded responses are dropped. There is an offline/online notice. | `app.js`, `error_state.html`, `base.html` |
| 0.7 Toasts | Errors persist until dismissed (success and info 5s, warning 8s). Timers pause on hover and focus. Each message is announced once through hidden polite/assertive regions. Actions are same-origin only, can expire, and are single-use when not idempotent. Dismiss targets are 44px. | `app.js`, `toast.html` |
| 0.8 Unsaved work | `akDirty` registry: a boosted navigation is held and the confirm dialog asks first. The composer, email builder and workflow editor are registered. Generic `x-data="dirtyGuard"` (snapshot-based; stays dirty after a failed save). | `app.js`, `composer.js`, `email-blocks.js`, `workflow_editor.html` |
| 0.9 Shell | OS-aware Ctrl K / ⌘K label. Palette `aria-expanded` bound to results. Stat value `text-2xl sm:text-3xl` (spec updated). Cookie notice uses a real button, the banner z-layer and safe-area padding. | `base.html`, `command_palette.html`, `app.css`, spec |
| 0.10 Drift guard | Classified detector: 8 rules, comments, scripts and expressions stripped. Pinned by 12 must-catch and 18 must-ignore cases. 11 justified exceptions (email bodies, embed sample, channel previews, docs/help/legal prose tables). Strict per-directory ratchet. **Baseline: 1,252 unapproved findings** (1,077 `gray-text`). | `template_drift.py`, `test_template_drift.py`, `drift_*.{json,toml}` |
| 0.11 Contrast tests | Token-layer tests: on-amber on amber 300/400/500, focus ring ≥3:1 on all surfaces, tone foregrounds ≥4.5:1 (purple excluded, with reason). The rendered scanner is `tools/qa/ui_scan.js`. | `test_contrast.py`, `tools/qa/ui_scan.js` |
| 0.12 Docs | Styleguide: input states, stacked dialog demo, drawer, toast types and actions, live error-state demo, action bar. | `styleguide.html` |
| 0.12a Mobile | Viewport meta: `viewport-fit=cover, interactive-widget=resizes-content`. **Controls are 16px below sm** (no iOS zoom). `min-h-viewport` (svh) replaces `min-h-screen` in 13 shells, and `100svh` was added to the public and docs CSS. Safe-area padding on topbar, drawer, cookie notice, toasts and sheets. `--kb-inset` keyboard helper on the inbox panes. `.action-bar`. `render_field` emits `inputmode`, `autocomplete` and `enterkeyhint`. Hover-only reveals in Content Studio planning hide only for `pointer-fine`. | many (see diff) |

### Automated results

- **Full suite:** 4,645 tests, 4,644 passed, 25 skipped.
  - 1 failure: `test_phase4_load.py::test_import_5000_contacts` (67.3s against a 60s budget) while running in parallel with the dev server and browser.
  - It **passes in isolation (30.2s)**. It's a timing test of the contact importer, which this work doesn't touch.
- **New and changed tests:**
  - nav: 8, including the rendered-sidebar "exactly one current link" route table
  - contrast: 30 with related files
  - ui tags: 15 (modal contract, drawer, mobile hints)
  - drift: 33
  - toast accessibility contract
- **Rendered contrast** (`ui_scan.js`). For comparability, the after-scan used **the same scanner version as Entry 0**, stored per origin:

| Screen (desktop 1536) | Light, before → after | Dark, before → after |
|---|---|---|
| `/dashboard/` | 5 → 3 | 7 → **0** |
| `/inbox/` | 7 → 3 | 11 → **0** |
| `/contacts/` | 18 → 15 | 4 → **0** |
| `/publishing/` | 21 → 18 | 5 → 1 |
| `/automations/` | 8 → 5 | **57 → 0** |
| `/insights/` | 18 → 15 | **44 → 0** |
| `/orders/` | 5 → 2 | 7 → 1 |
| `/channels/` | 3 → 0 | 5 → **0** |
| `/billing/plans/` | 44 → 41 | **59 → 0** |
| `/logs/messages/` | 3 → 0 | 4 → **0** |
| `/sales/` | 7 → 4 | 4 → **0** |
| **Total** | **139 → 106** | **207 → 2** |

- At 390px, dark failures fell the same way, and **controls under 16px fell from 8 to 0** on every screen.
- The light-mode remainder is mostly `text-gray-400/500` in page templates. That's area-sweep work, now counted by the ratchet.
- The two dark leftovers:
  - Content Studio "Overview" sub-tab, 2.27:1 → Phase 2
  - Orders count badge `opacity-75` → Phase 8

### Browser task verification (live, in the 390px simulator frame; manual-driven scripted checks)

| Check | Result |
|---|---|
| Modal opens | Focus lands on the first field ("First name"), not the heading. `main` is inert; the toast region is not. |
| Confirm stacked over modal | Focus goes to **Cancel**; the modal behind is inert. |
| Esc #1 | Closes only the confirm (answer "no"); focus returns to the field in the modal. |
| Toast while modal open | Announced (polite region = "Saved"); region not inert. |
| Esc #2 | Closes the modal; focus returns to the opener; nothing left inert. |
| Repeated close; dialog removed mid-open by a swap | Stack depth 0, nothing inert after settle. |
| Failed load into an opted-in region | Error state "This isn't here any more / It may have been deleted or moved." with Try again. |
| Two requests to one region, older one fails | Stale failure dropped; newer content shown. |
| Failed save | Form and typed text intact; persistent assertive toast "This no longer exists. It may have been deleted." |
| Offline ×2 → online | One offline notice, cleared on reconnect, plus "Back online." |
| Error vs success toast | Error still present after 900ms; success (600ms) gone. |
| Composer dirty → sidebar click | Held; "Leave without saving?"; Stay keeps the text. |
| `dirtyGuard` | Clean, then dirty, then clean when the edit is undone. Leave navigates once, with no second prompt. |
| Nav current link | `/inbox/`, `/inbox/followups/` and `/inbox/forms/` each mark exactly one link. |
| Phone bottom sheet | See `qa/phase0-modal-bottom-sheet-390.png`; dark automations after: `qa/phase0-automations-dark-1536.jpg`. |

### Corrections to the audit baseline

- **F7 was overstated.** Alpine's `x-trap` already returned focus on close. The real gaps were: no fallback when the opener had been swapped away, focus going to a non-focusable `h2`, Esc on `window`, and no inertness. All are fixed.
- **F8 was overstated.** The composer, email builder and workflow editor already had `beforeunload` guards and autosave. The real gap was that a boosted sidebar navigation bypassed all of them. Fixed with `akDirty`.
- **M6 / billing overflow (Entry 0) was wrong.**
  - The comparison table is inside `.table-wrap` and scrolls within it; the page is 390px wide.
  - The 440px reading came from the first run, with the welcome tour open, and could not be reproduced. The scanner's overflow list also counted content clipped by a scroll container; that's fixed in `ui_scan.js`.
  - The **real** issue stands for Phase 5: on a phone, the table squeezes feature text and hides the Business column behind a sideways scroll with no visible cue.
- **Process issue:** with `runserver --noreload`, stopping the background task left a second Python listener on :8000 serving old templates. That process was found and stopped, and both were restarted directly. Every check above ran after confirming that new markup was being served.

### Not verified yet (honest gaps)

- **360px, 768px, landscape and 200% zoom.** The simulator frame is fixed at 390, the window is maximized, and desktop was measured at 1536, not 1280.
- **Real devices:**
  - iOS input zoom
  - the `--kb-inset` keyboard behaviour
  - safe areas
  - Android Back
- **Screen-reader spot check** (VoiceOver/NVDA). Live-region contents were verified, not the spoken output.
- **The md icon rail** (optional) is not built, pending the 768 measurement.
- **Home/End in the palette:** deliberately not added. ARIA's combobox pattern leaves those keys to the caret in the search box.

### New findings for later phases

- **B1** "Automator" vs "Akilent": the site-settings default and 68 template fallbacks. Also the first-run tour ("Welcome to Automator") → Phase 3.
- **The `dark:` variant** (7× `dark:text-gray-300`) follows only the OS preference, not the in-app theme toggle → fix in the area sweeps (or add a `@custom-variant dark` keyed to `data-theme`).
- **`email/unsubscribe_confirmed.html`** uses Bootstrap stock colours (`#28a745`, `#721c24`) on a public page → Phase 9.
- **Inbox reply box while the 24-hour window is closed:** typeable. Whether sending is blocked → Phase 1 truthfulness check.

### Gates

| Gate | Status |
|---|---|
| Implementation | **Done** for 0.1–0.12a. The optional md rail is deferred, with reason. |
| Verification | **Done** for automated tests and desktop/390 browser checks. **Open:** 360/768/landscape/zoom, real devices, screen reader. |
| Acceptance | **Pending your review.** No unresolved critical defect is known. |

*(Superseded by Entry 2 for the responsive, regression and sign-off status.)*

---

## Entry 2: Phase 0 verification (responsive, regression, QA infrastructure), 2026-10-25

### Tested code

| | |
|---|---|
| HEAD | `555ce868` (nothing committed) |
| Tracked changes | 38 files, +1220 −188; `git diff HEAD --binary` SHA-256 `5826da7900639f66…` |
| Untracked (SHA-256 prefix) | same as Entry 1: `template_drift.py` `0be7d937`, `drift_baseline.json` `f321192e`, `drift_exceptions.toml` `50e59739`, `test_template_drift.py` `cace85fa`, `_drawer.html` `a7209151`, `error_state.html` `ce673c8e`, `tools/qa/` `472c6fa9` |

Changes since Entry 1 (both found by this verification):
- `assets/app.css`: touch screens (`pointer: coarse`) get 16px controls at every width; the new `.dialog-offset` class.
- `_modal.html`: uses `.dialog-offset` and drops the fixed `sm:my-24`.

### How responsive was measured

- **Browser:** Google Chrome 155.0.8059.85, headless, driven over the DevTools protocol by a dependency-free Node 24 harness (session scratchpad, not in the repo). It signs in with the local QA login and runs `tools/qa/ui_scan.js`.
- **Emulation:** device metrics with `mobile: true` and touch for phones and tablets. This is real mobile layout-viewport behaviour, **unlike** the Chrome-extension simulator used in Entries 0 and 1. That simulator renders a plain 390px iframe and missed the billing defect below.
- **Coverage:** 13 screens × 7 viewports × light and dark, plus a dialog check per viewport (98 rows).
- **The 200% zoom row** is emulated: a 1280×800 window at 200% lays out as a 640×400 CSS viewport at 2×. It's equivalent for layout, but it isn't a literal browser-zoom keypress.

| Viewport | Page overflow | Clipped controls | Contrast fails, light / dark (13 screens) | Touch inputs < 16px | Edit-customer dialog |
|---|---|---|---|---|---|
| Phone 360×740 | **`/billing/plans/` 440px** | Billing: "Finish setup" launcher | 104 / 2 | 2 | Bottom sheet, fits, Save on screen, focus inside, page inert |
| Phone 390×844 | **`/billing/plans/` 441px** | Billing: same | 104 / 2 | 2 | Same |
| Phone 844×390 (landscape) | none | none | 105 / 2 | 12 → **2** after fix | Fits width; Save needs scrolling (414px dialog on a 390px screen); reachable |
| Tablet 768×1024 | none | none | 105 / 2 | 12 → **2** after fix | Fits, Save on screen |
| Tablet 1024×768 | none | none | 106 / 2 | 12 → **2** after fix | Fits, Save on screen |
| Desktop 1280×800 | none | none | 106 / 2 | 12 (mouse: no zoom on focus; expected) | Fits, Save on screen |
| 200% zoom (640×400 @2×) | none | none | 105 / 2 | 12 (mouse) | Save off screen → **on screen** after fix |

Screenshots for every viewport (inbox, billing, composer and the open dialog) are in `docs/design/qa/responsive/` (28 files).

### Defects found by this verification

| # | Severity | Finding | Status |
|---|---|---|---|
| R1 | **High (billing)** | On 360 and 390 phones the plan-comparison table (461px, inside `.table-wrap`) widens Chrome's mobile **layout viewport** to 440px: `innerWidth` 440 against a visual 360. The page pans about 80px sideways and the fixed "Finish setup" launcher sits off-screen. The wrapper itself scrolls correctly (328px wide, 461px content). Clipping `html`, `body` or `#main` and `contain` don't undo it; hiding the table does. **This reverses the Entry 1 "billing doesn't overflow" correction**, which came from the iframe simulator. | **Open → Phase 5**: give the comparison a phone layout (per-plan cards) below sm, then re-verify with the harness. |
| R2 | Medium | Controls were sized 16px by **width** only, so a landscape phone (844px, "sm") and tablets got 14px and would zoom on focus. | **Fixed** (`pointer: coarse` rule). Re-measured 12 → 2. |
| R3 | Medium | On short screens the modal's fixed 6rem top margin pushed its actions below the fold at 200% zoom. | **Fixed** (`.dialog-offset`: the large margin only when ≥720px tall). Re-measured: Save on screen at 200%. |
| R4 | Medium | The Content Studio composer overrides the 16px rule: the caption `textarea` is 15px (`.cmp-textarea`) and the media-type `select` is `!text-sm`, 14px. These are the 2 remaining touch controls under 16px on every touch viewport. | **Open → Phase 2** (first item). |
| R5 | Medium (confidence) | The welcome tour's dismissal is stored in `localStorage` (`welcomeTourSeen`), so a user who skipped it sees it again on every new browser or device. It also appeared mid sign-in in the headless run. | **Open → Phase 3** (store per user, server-side). |

Light-mode contrast (about 105 per viewport, mostly `text-gray-400/500` in page templates) and sub-24px targets are unchanged by design: they're area-sweep work, counted by the drift ratchet. Dark mode leaves the same 2 known items on every viewport.

### Final regression run (normal conditions: suite alone, browser and harness idle)

| Run | Result |
|---|---|
| Full suite | **4,645 tests: 4,620 passed, 25 skipped, 0 failed, 0 errors** (165s, exit 0) |
| Shared-component tests (nav, ui tags, contrast, accessibility, drift, styleguide, dark tokens, z-layers, responsive tables, docs tables, CSS build) | 95 passed |
| `test_phase4_load.py::test_import_5000_contacts` (recorded separately) | Passed inside the full run this time. **Passed in isolation: 23.75s** (budget 60s). The Entry 1 failure (67.3s) was load contention from running the suite alongside the dev server and browser. |

### Not done here (left to you, per your checklist)

- **Real devices** (iPhone and Android): keyboard and the `--kb-inset` reply box, safe areas, form focus/zoom, inbox reply, scrolling, dialog open/close.
  - The dev server listens on `127.0.0.1` only. For a phone, run it on `0.0.0.0` (with the LAN IP in `ALLOWED_HOSTS`) or use staging.
- **Screen readers** (VoiceOver and NVDA):
  - dialog names and descriptions
  - focus movement and restoration
  - error and toast announcements
  - toast dismissal
  - region error updates
  - `aria-current` in the nav

  What *was* verified is the markup and the live-region contents, not spoken output.

### Temporary QA infrastructure (accounted for)

| Item | Where | State | Remove with |
|---|---|---|---|
| Local QA login `ui-qa@akilent-qa.invalid` | `db.sqlite3`, Admin of *Demo Boutique (demo workspace)*; password in `.env` (`QA_UI_*`, gitignored) | Kept for the remaining checks | Delete the user in Django admin; remove the `QA_UI_*` lines from `.env` |
| Migrations applied to local DB | `db.sqlite3` (13 from recent commits) | Permanent: the app needed them | Backup at `../automator-db-backups/db.sqlite3.bak-2026-10-25-pre-uiqa` |
| Redis container `akilent-uiqa-redis` | Docker, `127.0.0.1:6379` | Running | `docker rm -f akilent-uiqa-redis` |
| Dev server | `127.0.0.1:8000`, `--noreload`, inline `REDIS_*` env | Running | Stop the process (check that no second listener remains on :8000) |
| Framing-stripping proxy | `127.0.0.1:8001`, session scratchpad (`qa_frame_proxy.py`); local only; app headers unchanged | Running | Stop the process |
| Headless Chrome and profile for the harness | Port 9333; profile in the session scratchpad | **Not running** (harness exits and kills it) | Scratchpad is temporary |
| Responsive harness | `responsive_qa.mjs`, session scratchpad | Not in the repo | Ask to move it to `tools/qa/` if wanted |
| Evidence files | `docs/design/qa/` (2.4 MB of screenshots), `docs/design/ui-quality-log.md` | Screenshots are **not committed** (user decision, 2026-10-25); `docs/design/qa/` is in `.gitignore` and kept locally as evidence. The log itself is committed. | Delete the folder when no longer needed |

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** (0.1–0.12a, plus the R2/R3 fixes). Optional md rail not built. Tablets at 768/1024 showed no overflow or clipped controls, but navigation *usability* there (hamburger vs rail) was not assessed. Decide during the real-device pass. |
| Verification | **Done:** responsive matrix (7 viewports, emulated), full regression, shared-component tests, isolated load test. **Open:** real devices and screen readers (yours). |
| Acceptance | **Pending your sign-off.** Known open defects R1 (High, billing) and R4/R5 (Medium) are scheduled into Phases 5, 2 and 3. They come from page and area code, not from the foundation, so none blocks Phase 0. |

### Entry 2 addendum (2026-10-25): lint and format

- `mypy` reported one error (`apps/core/templatetags/ui.py:81`, the `hints` dict had no type annotation). Fixed by annotating it as `dict[str, str]`.
- `ruff format` reformatted 5 Phase 0 Python files. Layout only; no behaviour change.
- Checks now all pass: `ruff check .`, `ruff format --check .`, `mypy .` (620 files). `pytest apps/core` exits 0.
- `docs/design/qa/` was added to `.gitignore` because screenshots are not committed.
- New tracked-diff SHA-256 prefix: `a4ced581b4130025`. This replaces Entry 2's `5826da7900639f66` for review purposes.

## Entry 3: Phase 1 (conversations / inbox), 2026-10-25

Phase 0 acceptance: you committed it as `83f5262` and asked for the next phase, which this log takes as your acceptance sign-off for Phase 0. Real-device and screen-reader checks are still open for it.

### Tested code

| | |
|---|---|
| HEAD | `83f52625a5e3ba93184d822e3fb4145a5c833ecc` (Phase 0 committed by you; Phase 1 uncommitted) |
| Tracked changes | 26 files, +845 −520. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `167d69c700763e83…` |
| Untracked files (SHA-256 prefix) | `apps/conversations/tests/test_inbox_confidence.py` `ec5596dc…` · `tools/qa/inbox_qa.mjs` `11131806…` |

### What changed

| Area | Change | Files |
|---|---|---|
| Send outcomes (truthfulness) | A failed send used to remove the bubble and put the text back as if nothing had gone, even when the connection dropped mid-request or the server failed part-way. The message may already have reached the customer, so pressing Send again could send it twice. Sends now have three outcomes: *not sent* (a 4xx, a sign-in redirect, or offline before sending: the text goes back with the reason beside the box), *not confirmed* (network drop, 30s timeout, 5xx: the bubble stays with Check now / Put back in reply box / Dismiss, it survives a reload, and it is swapped for the real message when the thread shows it), and *sent*. Voice notes and files use the same rules. | `static/js/chat.js`, `_conversation_pane.html` |
| Connection status | After 2 failed polls in a row the thread says "Not connected…" (or "You're offline…"). It clears on the next good poll, and `online` triggers one immediately. | `chat.js`, pane |
| Automatic refreshes (shared JS) | Timed, load and SSE refreshes no longer raise toasts. Before, an unreachable server added an error toast every 8 seconds on the inbox, and since Phase 0 error toasts stay until dismissed. The region gets `data-stale` and `ak-refresh-failed` instead, and the inbox list shows one notice. This also applies to the other 5 polling regions. | `static/js/app.js`, `inbox.html` |
| Two-pane inbox (**existing bug**) | Since `f2d710e` turned on htmx View Transitions, the swap runs *after* `htmx.ajax` resolves. `inbox_pane.js` read the empty pane, treated it as a failure, and did a full page load on every click in Chrome, losing the list beside the conversation. It now waits for the pane to settle. | `static/js/inbox_pane.js` |
| Pane race safety | Clicking B then C opened B, because htmx queued C behind B on the shared source element. Now the pane is the source, an in-flight load is aborted first, and a late answer for anything but the latest click is dropped before it swaps. Verified with B's answer held back 1.5s. | `inbox_pane.js` |
| Pane load failure | A conversation that fails to load (offline, timeout, 5xx) now shows the error state in the pane with Try again. Before, it navigated away, which offline meant the browser's error page. 403/404 still go to the full page, which explains them. | `inbox_pane.js`, `inbox.html` |
| Details drawer | Below the column breakpoint the details panel now follows the dialog contract: `role="dialog"`, focus in and back, Esc for the topmost dialog only (it used a window-wide Esc), page behind inert, backdrop exempt so a tap closes it, and it is released if the window widens while it is open. | pane, `chat.js` |
| Styles | The pane's 358-line inline `<style>` (re-sent with every conversation opened) moved to `assets/app.css`, unlayered as before. The Instagram palette now uses `.cv[data-channel="instagram"]`. Failure text uses `--tone-coral-fg`. | `assets/app.css`, pane |
| Touch targets | On touch screens these are now 44px: the header's Back, Assign, More and Details buttons (were 36px), the composer's attach and saved-reply buttons (36px), and "Remind me tomorrow" / "Track as interested" (28px). The avatar hides while the pane is narrow, so the name keeps its width (118px at 360, was 108). | `assets/app.css` |
| Tokens | Conversations drift went from 112 to **0** (86 grey-text, 40 hex in the moved style block). "Suggested next" on `components/next_action.html` was 3.30:1 in light and is now `text-ink-subtle`, `text-xs`. The whole-site baseline is down from 1,250 to 1,134. | 18 templates, `drift_baseline.json` |
| Copy | The reply box placeholder follows the 24h window ("Window closed: send a template below" / "…a reply may not be delivered"), matching the banner. | pane |

### Automated results

| Command | Result |
|---|---|
| `poetry run ruff check .` / `ruff format --check .` / `mypy .` | Pass / 1,380 formatted / no issues in 620 files |
| `poetry run pytest -n 6 --junitxml …` (full suite, nothing else running) | **4,654 tests: 0 failures, 0 errors, 25 skipped** (110s) |
| Same, default `-n auto` while the dev server was up | 1 failure: `test_import_5000_contacts` at 68.1s against its 60s budget (the known timing test). It passes alone. |
| New: `apps/conversations/tests/test_inbox_confidence.py` | 9 tests (markup contract for send outcomes, connection notice, drawer, list notice, pane error region, placeholder) |

### Browser task verification (automated: `tools/qa/inbox_qa.mjs`, headless Chrome 155 with touch emulation; every POST intercepted, so nothing reached a customer)

**26 of 26 pass:**
- Sends: refused, server error is unconfirmed, unconfirmed survives a reload, it reconciles when the real message arrives, a network drop is unconfirmed, put back in reply box, offline keeps the text.
- Thread: offline notice, reconnects, server unreachable, recovers.
- Drawer: opens as a dialog, Esc closes and restores focus, backdrop closes.
- Phone: layout at 390 and 360 (reply box on screen, no sideways scroll, 16px input, back link); Back returns to the list; the draft survives list → thread.
- List: a failed refresh is quiet (0 toasts after 2 failed refreshes, 1 notice), and it recovers.
- Pane: a plain click loads in place, a load failure shows in place, Try again loads it, the last click wins with B delayed, and wide details are a column, not a dialog.

**Rendered contrast scan** (`QA_SCAN=1`, Phase 0 `ui_scan.js`). The forced states were an unconfirmed bubble, a failed bubble, a send error and the connection notice. Inbox at 390, thread at 390 and two-pane at 1280, light and dark: **0 contrast failures, 0 overflow**. Remaining "under 44px" items:
- the reply textarea, which sits inside a 44px+ box
- the "Overdue only" checkbox, whose label is the 75×36 target
- desktop (fine pointer) nav links

Screenshots are in `docs/design/qa/phase1/` (local only).

### Backend gaps (need separate authorization)

- **G1: inbox sends are not deduplicated.** `ReplyAction` already accepts `idempotency_key`, and WhatsApp and Instagram dedupe on it. But `conversations:detail` (`views.py`, action `reply`) doesn't pass one through, so a person who puts back an unconfirmed message and sends it again can send a duplicate. The UI therefore never offers a plain Retry and says "may or may not have been sent". The fix would be to forward a client key from the composer. It is small, but it changes a view, so it's left for you to authorize.

### Decisions recorded

- **M5, tablet (768–1023px):** the phone list↔thread layout stays. At 768, two panes would give about 380px each, narrower than a phone thread. The details panel is already a column only once the pane itself is wide enough. Revisit this on a real tablet.
- **24h window closed:** the reply box stays usable. The backend decides: Instagram refuses with a clear reason (shown as *not sent*), and WhatsApp marks it Failed with Meta's reason. The banner and placeholder say so beforehand.

### Observations (not fixed)

- On a 1280×800 laptop with the "Suggested next" card, the connection notice and a send error all showing, the thread is about 100px tall. That comes from the existing split-height calculation; the card can be dismissed. Revisit if pilots report it.
- `static/css/app.css` is served without a content hash in dev, so a browser can keep a stale copy (one QA run did). Production's static storage should be confirmed to hash file names.

### Not verified yet

- Real devices: iOS Safari keyboard (`--kb-inset` with the reply box), Android Back, safe areas.
- Screen reader: announcements for the "Not confirmed" line, the connection notice and the drawer.
- Live SSE (the dev server runs without it). The poll fallback is what was tested.

### Temporary QA infrastructure added

| Item | Remove with |
|---|---|
| 3 local conversations, "QA Phase1 Alpha/Bravo/Charlie" (contacts + 1 inbound message each, demo workspace, SMS, nothing sent) | Delete those contacts in Customers, or restore `../automator-db-backups/` |
| Chrome profile `chrome-profile-p1` in the session scratchpad | Goes with the scratchpad |

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** for the Phase 1 scope above. G1 is held as a backend gap. |
| Verification | **Done** (automated + 26 browser checks + rendered scan). **Open:** real devices, screen reader (yours). |
| Acceptance | **Pending your sign-off.** |

### Entry 3 addendum: code review fixes (10 findings), 2026-10-25

All 10 findings were valid. Each fix below has a browser check, except where noted.

| # | Finding | Fix | Verified by |
|---|---|---|---|
| 1 | The pane's stale-answer guard dropped in-pane forms that post elsewhere (tags, snooze), so their re-render was lost. | Pane loads now carry an `X-Inbox-Pane-Load` header, and only those are subject to the guard. Keying on the event's element wasn't enough, because htmx fires swap events on the target, as the first attempt showed. | `pane.in-pane-form-swaps` (real tag POST) |
| 2 | Any `load` trigger (and custom events on polled elements) was silenced, so the dashboard panels failed with nothing said. | Automatic means timed (the element's htmx `polling` flag, no event object) or `sse:…`, nothing else. | `load.one-off-failure-said` (dashboard 500 gives a toast); `list.refresh-failure-quiet` still passes |
| 3 | An unknown media send mutated a raw object, so Alpine never re-rendered it. | Replaced through `splice` on the reactive array. | `media.unknown-renders` |
| 4 | An unknown media bubble could never reconcile, because it only matched on text. | It pairs with the first outbound media message after it was attempted (±60s). | `media.unknown-reconciles` |
| 5 | `closePane()` left an in-flight load able to land and push history. | `closePane` bumps the load counter and aborts. The guard also drops pane loads once the pane is closed. | `pane.close-drops-late-load` |
| 6 | Restored unconfirmed sends were appended out of time order. | One `sortMessages()` is used on restore and on every poll. | `send.restored-in-time-order` |
| 7 | The settle match used the exact URL string (absolute vs relative), so a load could stall 4s. | Compared by pathname, and only for pane loads. | Covered by the pane checks (no 4s stall) |
| 8 | The silence flag lived on the element and could outlive a cancelled request. | The flag is on the request (`xhr._akAuto`), and the element-level marker is gone. | Code review (cancellation not reproducible headless) |
| 9 | The offline check was duplicated in `postSend`, which left dead branches. | `postSend` no longer checks; each caller checks once, and the dead branches are removed. | `send.offline-kept` |
| 10 | The pane retry patched the shared error state's markup from outside. | New region hook: `data-error-retry-event`. The pane listens for `inbox-pane-retry` and reloads through `openInPane` (spec §18). | `pane.retry-loads` |

Results:
- **Browser:** `tools/qa/inbox_qa.mjs` **32 of 32** (26 + 6 new).
- **Tests:** `pytest apps/conversations apps/core` exits 0.
- **Lint:** `ruff check` passes.
- **Tree:** tracked diff (excluding this log) SHA-256 `f07348834fd83d77…`, 27 files, +972 −526. Untracked: `test_inbox_confidence.py` `7788f81f…`, `tools/qa/inbox_qa.mjs` `66de50ad…`.
- **QA data:** a few `qa-tag-…` / `qa-dbg-tag` tags were added to the local QA contacts by these checks.

### Entry 3 addendum 2: second code review (10 findings), 2026-10-25

All 10 were valid. Finding 1 was a regression I had introduced.

| # | Finding | Fix | Verified by |
|---|---|---|---|
| 1 | **Regression:** the follow-ups-due toast (an `X-Toast` on an SSE refresh) was suppressed by the automatic-refresh silencing. | Automatic refreshes silence only their *failures*. Server-sent `X-Toast` messages always show. | `auto.server-toast-still-shown`, `auto.push-failure-quiet` |
| 2 | A row whose pane load failed couldn't be clicked again. | A click is ignored only when that conversation is already open *and loaded*. | `pane.failed-row-reclickable` |
| 3 | A "Not confirmed" reply could be matched by an AI reply (or a teammate's) with the same words. | Never an AI message, and only messages created after the attempt (±60s). A teammate's identical words at the same moment remain ambiguous: **backend gap G2** below. | `send.ai-reply-not-matched` |
| 4 | A file's caption was lost when the upload's outcome was unknown. | The caption stays on the bubble, "Put caption back in reply box" returns it, and it survives a reload (stored like a text reply). | `media.caption-kept-on-unknown`, `-survives-reload`, `-put-back` |
| 5 | Automatic detection read htmx internals (`htmx-internal-data.polling`). | Explicit opt-in, `data-auto-refresh` (value: extra event names), added to the 7 polling/SSE elements. No htmx internals are read. Limitation, documented in the spec: `htmx.ajax()` sends no event, so code must not use an opted-in element as the source of a user request. | `auto.code-request-not-silenced`; list checks still pass |
| 6 | The online refresh wasn't automatic: a failure toasted, and a success left the stale notice up. | `#inbox-body` lists `inbox-refresh` as one of its refresh events, so the online refresh (and the post-action refresh) clears the notice on success and stays quiet on failure. | `list.online-refresh-clears-notice` |
| 7 | A failing form inside the pane (snooze, assign, tags) during a pane load was read as the load failing. | The pane's error listener counts only its own load (pane-load header + path). | `pane.form-error-doesnt-fail-load` |
| 8 | Client-side exceptions in `apply()` showed as "Not connected". | Transport failures and client errors are now handled separately. A bad payload is logged to the console and doesn't count toward "Not connected". | `thread.client-bug-not-connection` |
| 9 | Sends had their own failure rules, which disagreed with `SAVE_COPY` (a 5xx was "may have sent" in one and "didn't save" in the other). | One classifier: `window.akFailure` (`describe`, `maybeSaved`, `SAVE_COPY`), used by `chat.js`. Requests aren't atomic (no `ATOMIC_REQUESTS`), so a 500 can follow a committed change. `SAVE_COPY.server` now says "may or may not have saved", and 502/503/504 are a new `gateway` kind. The broken styleguide demo button for this toast (unescaped apostrophe) is fixed. | Send checks; full suite |
| 10 | `PANE_HEADER` was duplicated as a literal. | It is built from the constant. | Pane checks |

**Older bug found while verifying (not from the review):** `akilent.onPage` ran a hook immediately when a deferred script registered it and again on `DOMContentLoaded`, leaving **two live copies** of each page script. The two-pane inbox loaded every conversation twice, with the first load aborted, and the DNS stale re-check ran twice. Hooks now run immediately only once the start-up pass has happened. Separately, `openInPane` no longer turns a cancelled load into a full page load.

**Backend gap G2:** human replies record no sender (`Message.metadata.sent_by` is set only for ai/automation/system). The feed therefore can't tell your send from a teammate's identical words, so an unconfirmed bubble can be cleared by a teammate's same-text message within a minute. Recording the sending user (and exposing it in the feed) would close this. It needs your authorization, like G1.

Results:
- **Browser:** `tools/qa/inbox_qa.mjs` **43 of 43** (32 + 11 new). The harness interceptor now handles empty bodies and custom response headers.
- **Full suite:** `pytest -n 6`: **4,654 tests, 0 failures, 0 errors, 25 skipped**.
- **Lint and types:** `ruff check`, `ruff format --check` and `mypy` (620 files) pass.
- **Tree:** tracked diff (excluding this log) SHA-256 `e6cd3b1dd88397e6…`, 33 files, +1068 −540. Untracked: `test_inbox_confidence.py` `7788f81f…`, `tools/qa/inbox_qa.mjs` `a50693f9…`.

### Entry 3 addendum 3: screen flicker (reported by you), 2026-10-25

- **Cause.** With `htmx.config.globalViewTransitions` on (older code, from `f2d710e`), every htmx swap ran a whole-document View Transition. That included the inbox list's 8-second self-refresh, which swaps whenever the content changes (the waiting times tick over every minute), so the whole screen cross-faded every few seconds. Measured with `QA_FLICKER=1 tools/qa/inbox_qa.mjs`: one full-page transition per 20 seconds on the inbox list and in the two-pane view, with no layout shift. The Phase 1 pane fix made the two-pane inbox actually stay on screen, which made this visible.
- **Fix** (`static/js/app.js`): `htmx:beforeTransition` is cancelled for anything but a boosted page navigation. Background refreshes, pane loads and form re-renders now swap in place without cross-fading the screen.
- **Verified:** 0 view transitions and 0 layout shift on the inbox list and in a conversation, at 1280 and 390. Harness still 43/43. Page-to-page navigation keeps its transition by design; that wasn't measured in this run.

## Entry 4: Phase 3 (accounts: sign-up, onboarding, settings, security), 2026-10-25

You asked for "phase 3" after Phase 1. Phase 2 (publishing) is **skipped for now, not done**, and its R4 (composer fields under 16px) stays open. Phase 1 acceptance is taken from your instruction to move on. Its real-device and screen-reader checks are still open.

### Tested code

| | |
|---|---|
| HEAD | `83f5262` (Phase 1 and Phase 3 both uncommitted, so this diff covers both) |
| Tracked changes | 115 files, +1477 −876. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `9ab2d74f184685f1…` |
| Untracked (SHA-256 prefix) | `apps/accounts/tests/test_accounts_ui_quality.py` `f8833f33…` · `tools/qa/accounts_qa.mjs` `2c36ad89…` · `tools/qa/inbox_qa.mjs` `5899d2e4…` (QA_FLICKER mode added) · `apps/conversations/tests/test_inbox_confidence.py` `7788f81f…` |

### What changed

| Area | Change |
|---|---|
| Tokens / drift | Accounts + auth went from **164 to 0** findings: grey text to ink tokens, 10/11px labels to `text-xs`, and the landing page's `:root` palette now points at the app's colour tokens instead of repeating the hex values. The three third-party logo colours (Instagram, Facebook, HubSpot) on Integrations are a documented exception. The whole-site baseline is down from 1,134 to 970. |
| Fixed ink | New `--color-ink-fixed` (#12182B, never flips with the theme). `--color-on-amber` points at it, and the landing page uses it for text on light bands. |
| Status colours | Stock `text-green/red/amber-*` in accounts and auth (green-600 and amber-600 measure about 3.3:1 on white, and the red-500 required-field asterisk about 3.8:1) became new `tone-teal-fg` / `tone-amber-fg` / `tone-coral-fg` utilities, which are theme-aware. The integrations Disconnect buttons use `btn-danger-ghost`. |
| Confirmation | Changing a member's role (Make admin / Make member) and revoking an invitation now ask first. Everything else destructive already did, and toggles and Sign out don't need to. |
| Outcome said | A refused save whose view answers JSON `{"error": …}` now shows that reason ("The workspace owner can't be removed") instead of the generic "You don't have permission" (`static/js/app.js`, `jsonError`, 4xx only). |
| Truthful copy | The sign-up toast said "We've emailed a link" at the moment the email was only queued (Celery). It now says "We're sending a link to verify your address." The email-confirmation pages keep their usual "we've sent" wording (delivery takes seconds). |
| Brand fallback (B1, template part) | All 74 template fallbacks `default:"Automator"` became `"Akilent"`. |

### Automated results

| Command | Result |
|---|---|
| `pytest -n 6 --junitxml …` (full suite) | **4,659 tests: 0 failures, 0 errors, 25 skipped** |
| `ruff check` / `ruff format --check` / `mypy .` | Pass / pass / no issues in 620 files |
| New: `apps/accounts/tests/test_accounts_ui_quality.py` | 5 tests: confirmations, no stock status colours, landing on tokens, fixed ink, no "Automator" in templates |

### Browser task verification (`tools/qa/accounts_qa.mjs`, headless Chrome 155)

Safety: requests to any host but the local server were blocked, billing POSTs were faked, the dev server ran with `EMAIL_HOST=127.0.0.1 EMAIL_PORT=9` (no mail can leave), and the verification email is queued to a Celery queue nothing consumes.

**12 of 12 pass** (the exit-gate tasks, all keyboard-only):
- **Sign-up at 390:** channel picked with Space, plan (Trial) picked with Space, every field reached with Tab and typed, then submitted with Enter. Lands on the first setup step with the "account is ready" toast.
- **Onboarding** loads at 390 with no sideways scroll.
- **Two-factor in dark at 1280:**
  - Skip link first (visible ring), then Esc closes the welcome tour.
  - The QR code has alt text.
  - The 6-digit code was computed from the shown key and submitted with Enter, which went to the recovery codes page.
  - The security overview then says "Configured".
- **Profile change:** a first-name edit saved with Enter, with the "Profile updated." toast, then restored.
- **Focus was visible at every Tab stop** in all of the above (0 stops without a ring).
- **Scan of 17 account screens:** 390 and 1280, light and dark, 68 page loads in all. **0 contrast failures, 0 overflow, 0 unlabelled fields, 0 fields under 16px on phones.**

Screenshots are in `docs/design/qa/phase3/` (local only).

### Backend gaps (need separate authorization)

- **G3, welcome tour (R5):** dismissal lives only in the browser's `localStorage`, so the tour returns on every new browser or device. The existing "Not now" store only accepts known suggestion keys and expires after a set number of days. Remembering it per user needs a small model/view change.
- **G4, product name (B1, server part):** `SiteSettings.app_name` defaults to "Automator" in the model and its migration. So do the Python fallbacks (`apps/accounts/settings_views.py`, `views.py`, `core/console/views.py`, `email/services/html_email.py`) and the SES "Source" tag. Your local database still holds "Automator", which is why the sidebar shows it. Production shows whatever is set in the console. Templates are fixed, but the rest is backend.

### Not done in this phase

- Settings forms were not moved onto `render_field`. They already have labels and 16px fields, and the scan found none unlabelled. A wholesale rewrite would be churn without a defect behind it.
- Real-device and screen-reader checks (yours).

### Temporary QA data added

- **Throwaway accounts** `qa-signup-…@akilent-qa.invalid` (one per harness run, about 6) in the local database. Each has a workspace on the Trial plan, and some have two-factor on. Delete them in the console or Django admin, or restore `../automator-db-backups/`.
- **The QA user's first name** was changed and restored by the test.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done**. G3 and G4 are held as backend gaps. |
| Verification | **Done** (full suite, 12/12 browser tasks, 68-load scan; inbox harness still 43/43). **Open:** real devices, screen reader. |
| Acceptance | **Pending your sign-off.** |

## Entry 5: Phase 2 (Content Studio) and the leftovers from Phases 0–3, 2026-10-25

Scope, as you asked: Phase 2 plus what remained from earlier phases. **Phases 4–9 are not started.** A site-wide crawl was begun and stopped; its partial results aren't used. Billing's R1 stays with Phase 5.

### Tested code

| | |
|---|---|
| HEAD | `83f5262` (Phases 1, 2 and 3 all uncommitted, so this diff covers all three) |
| Tracked changes | 147 files, +2557 −1140. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `33e4a549d844bdca…` |
| Untracked (SHA-256 prefix) | `tools/qa/publishing_qa.mjs` `e1027f8e…` · `tools/qa/area_scan.mjs` `868098d7…` · `apps/publishing/tests/test_publishing_ui_quality.py` `d8e844f1…` · `tools/qa/inbox_qa.mjs` `5899d2e4…` · `tools/qa/accounts_qa.mjs` `2c36ad89…` · plus the Phase 1/3 test files listed in Entry 4 |

### Phase 2: what changed

| Area | Change |
|---|---|
| R4 (open since Phase 0) | Composer caption boxes are 16px on touch screens, and the media-type select no longer forces 14px. Every field on `/publishing/new/` at 360 is now ≥16px. |
| Field sizing | 22 publishing fields had `text-sm`/`text-xs` overriding the shared field size (14px on phones). Those classes are removed, so the `.input` rules apply (16px on phones and touch screens, 14px from sm up). |
| Labels | Five library filters had labels not tied to their fields, as did all fields on "Plan a post" and "Themes". All are now linked by `for`/`id`, and the plan form's platform checkboxes are a `fieldset` with a `legend`. |
| Dialogs | The composer's publish/schedule confirm and the cropper join the shared dialog stack (`akDialog`): page inert, Esc for the topmost dialog only, focus in and back. The confirm had a window-wide Esc and reused the global confirm's `confirm-title` id; it now has its own. |
| Truthful errors | Per-channel failure notes were developer phrases, one of them false. A provider outage said "will retry" but is saved as FAILED with no automatic retry. They're now plain and true: a timeout says "may already be live, check it on Instagram before trying again", and an outage says "had a problem on its side, try again later" (`publishing_filters.variant_error_message`). |
| Empty states | The library told "No posts match your filters" even with no filter set. It now tells "No posts yet" (with "Create a post") from "No posts match these filters" (with "Clear filters"). The calendar, Overview and Planning empties point to the next step. |
| Colour | Publishing drift went from 155 to 0. Brand logo tiles and composer chrome that must match the platforms are documented exceptions. The active sub-nav tab, Copy buttons and today's date used fixed brand navy (2.1–2.3:1 in dark) and now use `text-accent`. Stock red/amber became `btn-danger-ghost`/tone colours. The "link not found" page now uses classes instead of inline styles. |

### Leftovers from earlier phases, fixed here

| From | Defect | Fix |
|---|---|---|
| Phase 0 | `hx-confirm` fell back to the browser's own `confirm()` if clicked before the page finished starting. | It now blocks with "Still loading. Try again in a moment.", the same as `data-confirm`. |
| Phase 0 | The email editor asked for a blank's name with the browser's `prompt()`. | The shared confirm dialog now takes an optional text field (`ask({ input })`, which resolves the text or null), and the editor uses it. |
| Phase 0 (dark bridge) | Brand navy text (`text-brand-500…800`, about 2.1:1 on the dark canvas) wasn't covered by the dark-theme text bridge. | It now maps to `--color-accent` in dark, app-wide. |
| Phase 0 (found by the Phase 2 harness) | Choosing "Stay on this page" in the unsaved-work guard left the loading bar stuck and the whole page dimmed for good. The bar started in `htmx:beforeRequest`, which the guard cancels, so `afterRequest` never came. | The bar now starts in `htmx:beforeSend` (only requests that actually go out). |
| Phase 0 (found by the Phase 2 harness) | `#main { view-transition-name }` made the page a stacking context, so overlays written inside a page (the cropper, the composer confirm, and others) sat **under the sticky header**. | The name is only set while a page-to-page transition runs (`.ak-page-vt`, added in `htmx:beforeTransition` and removed when the transition finishes). |

### Automated results

| Command | Result |
|---|---|
| `pytest -n 6 --junitxml …` (full suite, alone) | **4,664 tests: 0 failures, 0 errors, 25 skipped** |
| Same, while a browser harness ran against the dev server | 14 failures in unrelated areas (API quota, email codes, payments). They didn't reproduce when the suite ran alone, so they were caused by the concurrent load, not by the code. |
| `ruff check` / `ruff format --check` / `mypy .` | Pass / pass / no issues in 620 files |
| New: `apps/publishing/tests/test_publishing_ui_quality.py` | 5 tests (error wording, no false retry promise, failed channel on the post page, library empties) |

### Browser verification

- **`tools/qa/publishing_qa.mjs`: 12 of 12.**
  - Composer at 360: edit/preview tabs, no sideways scroll, every field ≥16px.
  - The unsaved-work guard asks; "stay" keeps the work and leaves the page usable.
  - The publish confirm is on the dialog stack: focus on "Not yet", page inert, Esc closes it alone.
  - The cropper is on the stack, fits a 360 screen and covers the header; Esc skips the crop.
  - No native `confirm`/`prompt`/`alert` is called.
  - A post shows per-channel status.
  - A page-to-page navigation leaves no transition name behind.
- **`tools/qa/area_scan.mjs /publishing/`: 12 pages × 360/1280 × light/dark: 0 issues** (contrast, overflow, clipped controls, unlabelled fields, phone fields under 16px).
- **Earlier phases rerun** against the final code: inbox 43/43, accounts 12/12.

### Not verified

- A *real* failed publish: the local data has none. The rendering is covered by the new test, which uses a failed variant.
- Real devices (touch dragging in the cropper, the iOS keyboard) and screen readers: yours.

### QA data added

One more throwaway `qa-signup-…@akilent-qa.invalid` account per accounts-harness run, about 9 in total now.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** (Phase 2 and the listed leftovers). |
| Verification | **Done** (full suite, 12/12 + 12-page scan, earlier harnesses green). **Open:** real devices, screen readers. |
| Acceptance | **Pending your sign-off.** |

### Entry 5 addendum: two code-review rounds (19 findings), 2026-10-26

| | |
|---|---|
| HEAD | `915ce04` (your commit since Entry 5; the remaining Phase 2/3 work and these fixes are on top of it, staged or modified) |
| Tracked changes | 132 files, +1777 −646. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `6bc4d5262caba0f9…` (the QA harnesses and the new test files are staged, so they're inside this hash) |

**Round 1 (9 findings), all fixed:**
- Cropper with several images queued: the next crop opened before the last one had left the dialog stack, so it had no inert page and no Esc. The dialog now closes fully before the callback opens the next one (`composer.js closeCrop`).
- A browser clock running fast left a duplicate reply stuck on "Sending…". The page and every feed poll now carry `serverNow`. The browser works out its offset (from the poll's midpoint, within half the round trip) and stamps sends in server time.
- A late answer from a form inside the pane (assign, tags) could replace the conversation opened after it. A pane epoch is bumped when a load starts and again when it swaps in; answers from an older epoch are dropped (`inbox_pane.js`).
- A poll answered by the sign-in page now says "You've been signed out…" instead of "Not connected… Reconnecting…".
- Channel names in error notes come from `platform_display_name`, not a third label map. The fallback is "the channel", capitalised only at the start of a sentence.
- The 4-second settle wait and its full-page fallback are gone from pane loads. A pane load never runs a View Transition, so the pane is filled when `htmx.ajax` resolves.
- "Add a blank" before the page finished loading now says "Still loading" instead of doing nothing.
- Stale comments corrected: the progress bar counts `htmx:beforeSend`, and unconfirmed media *is* kept across a reload. Putting back media with no caption is now a no-op (the button was already hidden).

**Round 2 (10 findings), all fixed:**
- A file's "Sending…" placeholder now pairs with its real message if that arrives first, so a late upload failure can't leave a "Not confirmed" bubble beside the sent file.
- The send timeout now covers reading the response body too. A stalled body ends as "Not confirmed" and the reply box works again.
- `serverNow` is read as late as possible (the last key in the feed), and polls refine it (see round 1).
- Only reads (GET) take a turn in an error region's sequence (`app.js`). A form posted inside the pane can no longer hide a load's failure, and a mutation's own failure is never dropped as stale.
- The details-drawer watcher checks that `akDialog` exists.
- Pressing Send while offline no longer dismisses the "someone else just replied" warning.
- With overlapping page navigations, only the newest View Transition removes `.ak-page-vt`.
- Provider errors ending in "!" or "?" no longer get a "." added after them.
- A voice note or file that wasn't sent (offline, refused, or while another send was in progress) is kept, with "Send voice note" / "Send file" and "Discard" buttons. Before, the recording was lost.
- Dead code `ev === el` removed from the `data-auto-refresh` check.

**Automated results:** full suite **4,641 passed, 0 failures, 25 skipped**. The first run had 1 failure in `apps/automation` (`test_after_hours_messages_recommend_replying_when_closed`); it passed alone and on a clean rerun, so it was a one-off under parallel load and is not related to this work. `ruff check`, `ruff format` and `mypy` are clean on the changed Python. `node --check` passes on every changed script. New tests: channel naming for any platform, sentence-start capitals, and the provider's own punctuation.

**Browser results:**
- **Inbox: 56/56.** The 13 new checks:
  - the server clock is known
  - a fast clock still reconciles
  - signed out is said, and clears
  - putting back a captionless file does nothing
  - stale form answers are dropped, whether sent before the click or during the load (the request log confirms both late answers were sent and fulfilled)
  - the real file message arriving during the upload reconciles
  - a stalled body times out
  - an offline voice note is kept, then sent once back online
  - the collision warning survives an offline Send
  - a load failure is still said despite a form post
- **Content Studio: 14/14.** New: two queued crops. The second crop is a full dialog, and the queue ends cleanly.
- **Accounts: 12/12.** The harness now clears cookies at start, because a session left over from the last run made `/signup/` redirect.

**Not verified:** overlapping page transitions (can't be observed headless; the logic is a single latest-transition check). Real devices and screen readers remain yours.

**QA data:** one more throwaway `qa-signup-…@akilent-qa.invalid` account (about 10 now) and one more `qa-tag-*` tag on a QA contact per inbox run. The stale-form posts were answered by the harness and never reached the server.

## Entry 6: Content Studio calendar and post page redesign (your request), 2026-10-26

Tested code: HEAD `915ce04` + working tree, `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `6120a24d8964bd91…`.

**Calendar** (`calendar.html`, new `_cal_entry.html`, `_cal_plan.html`, `_cal_agenda.html`, `_platform_icons.html`)
- **Toolbar:** previous / Today / next and the week's dates, with a summary line ("4 posts · 4 planned · 1 failed"). Week/List is a segmented control (new shared `.segmented`), and the filters apply as they change.
- **Wide screens (xl):** a seven-column week in one card. Today's date is filled with the accent colour, and past days sit on a muted ground. Each post shows its time, channel icons, two lines of caption, and its status as a coloured edge **plus a written label**. Plan items are dashed and labelled "Planned", with the theme's colour dot. A day's + appears on hover or focus (always on touch). Empty future days offer "+ Plan an idea".
- **Narrower:** an agenda with one row per day, a date block, thumbnails, and "Create a post / Plan an idea" on empty days. The list view is the same agenda, showing only days with something in them, with an empty state when the week is empty or filtered out.
- **"Not scheduled yet":** shown as cards with an image, caption, status and channels.
- **View change:** a `week_summary` dict and a `same_month` flag added to the context. Display only.

**Post page** (`post_detail.html`, `variant_result_card.html`, `_results_card.html`, `_metric_card.html`)
- **Title:** the caption's first line (new `post_headline` filter) instead of "Post", with breadcrumbs. A meta line gives the status, channels, type, and "Published / Goes out / Set for / Not scheduled". "Set for" is used for a draft with a time, which won't go out on its own. The actions are ordered with one primary.
- **Layout:** wide (lg) puts the post on the left (media, caption, per-channel captions, call to action, results, performance, options) and review + channels + details on the right. On a phone, review and channels come first.
- **Channel rows:** each has its status, own time, view link, reschedule, and its failure as "Why it failed" with the plain reason, plus "Retry this channel".
- **Other changes:** a post-level error is now a banner at the top, not the bottom. Results and performance numbers are stat tiles. A carousel is a swipeable strip with counters.
- **Fixed on the way:** `.copy-btn` is styled for dark code blocks (gray-400, white on hover), which nearly disappears on a card. A `.copy-btn--light` variant uses the accent colour.
- **Unchanged:** every action, form, condition and message, including the review flow, truthful channel states and the result notes.

**Also:** a Facebook icon and a `channel_tag` Facebook entry.

**Results**
- Full suite: 4,641 passed, 0 failures. ruff and mypy are clean.
- `publishing_qa.mjs`: **19/19**. The 5 new checks: week grid at 1280, agenda at 390, list with no overflow, channels before the post on a phone, channels beside the post at 1280.
- `area_scan`: calendar (week, list, past week) and three posts (published on two channels, failed, carousel) at 360/1280 × light/dark, **0 issues**. The first scan caught the invisible-until-hover "+ Plan an idea"; it is now always readable.
- Two tests asserted old wording. "Started from an AI suggestion" is restored, and my own "Per-channel status" assertion now checks the Channels heading.

**QA data (local):** in the demo workspace's current week, 4 draft/failed posts (24–27) and 2 "QA …" plan items. Drafts and failed posts are never picked up by the scheduler.

**Not verified:** real devices and screen readers.

## Entry 7: third code-review round (10 findings) and a long-standing polling bug, 2026-10-26

Tested code: HEAD `915ce04` + working tree, diff SHA-256 `38c26b8883308c87…`.

**Found while verifying (the most serious item): conversations stopped updating.**
- **What users saw:** in the two-pane inbox, after one conversation was closed or replaced, every conversation opened afterwards never polled. New messages and delivery ticks didn't appear until a full reload. With boosted navigation the same applied on phones, for the second thread opened.
- **Cause:** Alpine stores a field a component sets without declaring it on the outermost `x-data`. In base.html that is `<body x-data>`, one object shared by every page and kept across boosted navigation. The closed conversation's `this._destroyed = true` landed there, and every later conversation read it.
- **How long:** since 2026-09-27, when `chat.js` gained `_destroyed`.
- **Fix:** every private field is declared on its component. That covers chat (9 fields), `cvMenu`, `voiceNote` (whose per-note listeners were also being shared), and the composer (9 fields, including the crop queue and save timers).
- **Guards:** the new `apps/core/tests/test_alpine_fields.py` fails if any app script sets an undeclared `this._…`. Harness check `pane.next-conversation-polls` covers it in the browser.

**Review findings, all fixed:**
- **Held files and voice notes:** they are now a list, each with its own notice by the reply box. A text reply can't hide them, and a second one doesn't replace the first.
- **Refused uploads:** these (wrong type, too large, window closed) are no longer held for a retry that would fail again. The reason is shown instead.
- **Matching files to the thread:** files and voice notes are never auto-matched to a media message in the thread, pending or unknown. A teammate's photo can't be told apart (gap G2), and wrongly matching would hide a send that may not have gone. This reverses the round-1/round-2 pairing; the trade-off is a "Not confirmed" note the person dismisses. Its copy now says to check the thread above, then dismiss.
- **Calendar summary:** it counts posts, not channel entries, and "failed" is per post.
- **Region errors:** a mutation whose answer re-rendered a region now supersedes older reads still in flight, so a late read failure can't wipe the fresh content. While a conversation is loading in the pane, no form answer is let into it, so if that load fails its error still shows (this fixes an interaction between the two rules, found by the harness).
- **Signed out:** shown only when the feed was redirected to the sign-in page itself (`loginUrl` added to the chat config). Any other non-feed page counts as a failed poll.
- **Stale page clock:** a page time more than 5 minutes off the browser's clock is treated as an old page and ignored, and the first poll runs at once.
- **Whitespace-only errors:** a provider error of only spaces or newlines shows nothing, not a lone ".".
- **Cropper Esc:** it still works when the shared dialog stack is absent.

**Harness fixes:** all three harnesses now turn off the browser cache. A cached `composer.js` had made a fixed check look broken. The Back-to-list check waits for the list instead of a fixed sleep.

**Results**
- Full suite: 4,644 passed, 0 failures. One run under heavy load had a single failure that passed when rerun and on a clean rerun.
- ruff and mypy are clean.
- Browser checks: inbox **61/61**, publishing **20/20**, accounts **12/12**.
- New inbox checks: unconfirmed file not matched to another file, upload not paired with another file, held items survive a text send and a second file, refused upload not held, other page not "signed out", write beats a late read failure, next conversation polls.
- New publishing check: Esc without the dialog stack.

## Entry 8: closing the Phase 0–3 gaps (your request), 2026-10-26

Tested code: HEAD `66c0541` + working tree, diff SHA-256 `8c8e288a14185ff3…`. You authorized backend gaps G1–G4 ("close the gaps from phase 0 – phase 3").

**G1, duplicate sends (closed).** Every inbox send carries a random key from the browser. The server builds `inbox:<conversation>:<user>:<key>` (`apps/conversations/inbox_keys.py`): the person comes from the request, never the browser, and the conversation is part of the key.
- **WhatsApp:** dedupes on it per account.
- **Instagram:** a repeat never attempts a second send. It reports the first attempt instead: sent, failed (with the reason), unconfirmed (the inbox keeps saying "Not confirmed"), or still on its way. Before this, a retry while the first was still sending would have sent it again.
- **In the browser:** an unconfirmed reply (or file, while it's still in memory) now offers **"Send again"**, which reuses the key. The copy says so: "Sending it again is safe: if it already went, the customer won't get it twice."

**G2, who sent a reply (closed).** Both channels' projections record `sent_by_user` and `client_key` in the message metadata.
- **Kept apart from `sent_by` on purpose:** reports, follow-ups and AI context treat "no `sent_by`" as a person's reply, and still do.
- **Matching:** the feed exposes `clientKey`. The inbox matches its "Sending…" or "Not confirmed" bubble to exactly its own message, for text and files alike. A teammate's identical words or photo, or an AI reply, can't stand in for it. This replaces both the old text-and-time guess and Entry 7's "never match files".

**G3, welcome tour (closed).** `Membership.welcome_tour_seen_at` (migration `accounts/0024`), a POST endpoint `/onboarding/tour-seen/` (staff "view as" never records), and a flag in the context processor.
- A dismissal an older version kept in the browser is carried over the first time that browser loads a page. Verified: the QA user's was.
- It's per workspace, because the tour's slides follow that workspace's channels.

**G4, product name (closed).** The `SiteSettings.app_name` default and every Python fallback say "Akilent".
- **Data migration (`core/0019`):** renames a stored name only when it is exactly the old default "Automator". A name someone set deliberately is left alone. Your local database now shows "Akilent".
- **Left on purpose:** the SES "Source" message tag stays "Automator". It's a machine tag AWS-side rules may filter on, and no one sees it.
- **Guard:** a test fails if "Automator" comes back into Python code.

**Phase 0 leftovers.** Shared components and root templates had 32 drift findings; now 0, and the baseline went from 815 to 783.
- **The onboarding badge:** it was `bg-ink text-white`, unreadable in dark mode (white on a light badge). It now uses `text-surface`.
- **The "View as" banner:** it used an undefined `--color-danger` with a hex fallback. It now uses the danger button's colours.

**Phase 1, live updates (now tested, and a production bug found).**
- **The bug:** `base.html` declared `hx-ext="preload sse"`. htmx splits on commas and drops spaces, so with `REALTIME_SSE_ENABLED` on, the page asked for one extension called "preloadsse". Live updates never connected, **and hover preloading silently stopped too**.
- **The fix:** `hx-ext="preload, sse"`. A test renders the page with the flag on and off and checks every name is a vendored extension.
- **Verified on the real stack:** uvicorn ASGI with the flag on, Redis, a browser. New `tools/qa/sse_qa.mjs`: **3/3**.
  - A message event refreshes the open thread at once.
  - Another conversation's event is ignored.
  - A conversation event refreshes the list without a toast.
- **Server side checked separately:** the stream opens, Redis delivers, and an idle 8-second wait doesn't time out.
- **Local note:** two dev servers on one SQLite file lock it ("database is locked" in the browser-session middleware). Run one at a time locally; production is Postgres.

**Phase 2 leftovers.**
- `planning.html`'s `min-w-[40px]` is now `w-10`.
- **The media loading grid is not applicable:** Content Studio has no media grid that loads after the page (library and post media render with it), and composer uploads already show "Uploading" with a progress bar.

**Reported by you while this was running.** Hovering a sidebar link on a page with unsaved work opened "Leave without saving?". The hover preload is a boosted GET, and the unsaved-work guard treated it as navigation. Preloads (`HX-Preloaded: true`) now pass. A click still asks. New check `composer.hover-preload-no-prompt`: the preload went out, no prompt, still on the page.

**Results**
- Full suite: **4,660 passed, 0 failures**. ruff, format and mypy are clean.
- Browser checks: inbox **65/65**, publishing **21/21**, accounts **12/12**, live updates **3/3**.
- `area_scan`: dashboard, inbox, onboarding, team settings, library at 360/1280 × light/dark: 0 issues.
- New tests: WhatsApp dedupe, keys scoped to the person and conversation, projection metadata, feed `clientKey`, Instagram repeat cases (sending, unconfirmed, failed), welcome-tour memory, product-name guard, extension names.

**What only you can close:** real devices and screen readers (Phases 0–3), the Meta sandbox and a real failed publish (Phases 1–2), and your acceptance of each phase.

## Entry 9: inbox reload, chat height, page titles, review round 6, 2026-10-26

Tested code: HEAD `66c0541` + working tree, diff SHA-256 `0f37a3137125a51f…`.

**Reported by you: reloading an open conversation lost the inbox.** A full page load of `/inbox/<id>/` (a reload, a link, a new tab) rendered the standalone conversation page. It now renders the inbox with that conversation already in the pane: `.has-conversation`, `data-open`, the row marked, and history set as a click would set it.
- On a phone the conversation shows alone, with Back to the list, as before.
- Its forms post into the pane.
- `conversation_detail.html` was no longer used and is removed.

**Reported by you: the chat's height was unbalanced.**
- **Desktop:** the conversation sized itself from the viewport and its own offset, ending 32px short of the list column. The split's 32rem minimum also overrode the fitted height whenever a suggestion card sat above, so the page scrolled. Now the column, pane, `.cv-wrap` and `.cv` pass the height down as flex columns, the list's card fills its column, and the minimum is 22rem. At 1280×860 both cards end at the same line (828) with no page scroll.
- **Phone:** the inbox title and suggestion card stayed above an open conversation, squeezing it into the bottom half (460px). They're now hidden below lg while a conversation is open (`.inbox-chrome:has(~ .has-conversation)`), so the conversation gets 732px.

**Found while fixing that: every app page showed the site's generic title.** `seo/meta.html` wrote a `<title>` ahead of the page's own in `base.html`, and a browser shows the first. `seo_meta` now takes `title=False` for layouts with their own title block (`base.html`, `public/_base.html`). The landing page keeps its SEO title. A test checks for exactly one title.

**Review round 6 (10 findings).**
1. **Instagram's echo recorded first left the bubble stuck:**
   - **Server:** `record_sent_message` now puts our key and sender on the echoed message and drops "instagram_app".
   - **Browser:** a message with no key still clears a text bubble by its words (a teammate's inbox reply always has its own key).
   - Fixed.
2. **The old browser-wide tour flag marked every later workspace as seen:** it is carried over once and then removed, and no longer written. Fixed.
3. **"Send again" recorded the AI suggestion in the box as used:** the resend now drops `ai_proposal` and leaves the AI state alone. Fixed.
4. **A repeated Instagram file send skipped a caption the first attempt never sent:** the caption now goes, unless the attachment is unconfirmed. Fixed.
5. **A repeated file send stored a new, unused copy:** the view checks for an existing send first and prepares nothing. Fixed.
6. **Kept files were never freed:** they're released on dismiss and put-back. Fixed.
7. **"Send again" could remove the note and then not send:** every precondition is checked first. Fixed.
8. **A WhatsApp repeat answered "ok" whatever happened:** repeats are now checked at the top of both actions, for both channels. Failed or cancelled is refused with the reason; unconfirmed is reported as unconfirmed. Fixed.
9. **An extra query on every page:** the membership is memoised with the account (`current_membership`). Fixed.
10. **The tour opening during "View as":** **not valid.** `base.html` doesn't render the tour when `viewing_as` is set. The membership helper also returns none while viewing as.

**Results**
- Full suite: **4,667 passed, 0 failures**. ruff, format and mypy are clean.
- Browser checks: inbox **73/73**, publishing **21/21**, accounts **12/12**.
- New inbox checks:
  - reload keeps the inbox and pane (1280)
  - reload, then another conversation, then Back
  - phone shows the conversation alone, and Back goes to the list
  - columns balanced, no page scroll
  - "Send again" not counted as AI use
  - a keyless message still clears its bubble
  - dismissing frees the file
- New server tests: WhatsApp repeats by status, no file prepared on a repeat, echo-first merge, reload renders the inbox with the pane, one title per page.

## Entry 10: Phase 4 (WhatsApp and Instagram), 2026-10-26

You said "continue to next phase", which lifts the hold on Phase 4. Phases 5–9 stay on hold until you say otherwise.

### Tested code

| | |
|---|---|
| HEAD | `fc2a649`. You staged part of this work during the phase; nothing is committed. |
| Tracked changes | 30 files, +470 −419. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `927b90f02ede4ab3…` |
| Untracked (SHA-256 prefix) | `apps/whatsapp/tests/test_wa_ui_quality.py` `f844291d…` · `apps/instagram/tests/test_ig_ui_quality.py` `40279b12…` · `apps/core/tests/test_templates_compile.py` `512d805c…` · `tools/qa/channels_qa.mjs` `9c6bb5fb…` |

### Defects found and fixed

| # | Defect | Fix |
|---|---|---|
| 1 | **A working Instagram connection was badged "Sent".** The accounts page reused the message-status component. | The badge shows the connection's own state: Connected, Connection expired, or Setup not finished (with what to do). |
| 2 | **Campaign recipients were all "Sent" once queued.** The table read the recipient row's own status. `queued` only means "handed to the send queue", so the label covered messages WhatsApp later failed, never confirmed, or held at the plan limit. The "Failed" filter also missed messages that failed after queueing. | Rows and filters use `recipients_with_outcome`, the outcome the CSV export and stat cards already used. Each row shows one of: Read / Delivered / Sent (only once WhatsApp says so), Failed, Not confirmed ("may not have been sent; won't be sent again automatically"), Waiting (plan limit, with the real hold time from `WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS`), Queued, or Not sent. |
| 3 | **The number details grid never lined up.** Tailwind v4 compiles `grid-cols-[7rem,1fr]` to `grid-template-columns:7rem,1fr`, which is invalid and ignored. | Now `7rem_1fr`. A new drift rule, `grid-comma`, catches a comma outside parentheses in any arbitrary grid value (fixtures included). None remain anywhere. |
| 4 | **Every flash message on the Instagram pages showed twice**, inline and as the shell's toast. | The inline copies are removed. |
| 5 | **No unsaved-work guard** on the template builder, the campaign composer or the comment-rule form. | All three are guarded. The composer takes its "before" state at the person's first key or pointer press. At load, `x-model` is still filling fields in, later than any timer can safely wait, and the page moves focus in by itself. |
| 6 | **The shared `dirtyGuard` treated a form marked `hx-boost="false"` as boosted**, because it looked for any boosted ancestor. A real submit would have asked "Leave without saving?". | The nearest `hx-boost` decides. |
| 7 | **The template builder's active step and category labels failed contrast:** Meta blue on its own tint measured 4.41:1 (light) and 3.77:1 (dark). | The labels use the app's accent. The tint and marks keep the WhatsApp accent. |
| 8 | **The rule form's textareas carried both `.input` and `.textarea`.** | The form uses `components/form.html` (labels, required marks, linked errors). The view only adds `w-full`. |

### Other changes

- **Template statuses say what they mean** (Templates → WhatsApp). Each badge has one line under it:
  - Approved: "Ready to send."
  - Pending: "Meta is reviewing it."
  - Rejected: "It can't be sent; create a new version."
  - Paused: "It can't be sent while paused."
  - Draft: "Not submitted to Meta."

  A note says statuses come from Meta, that they update when Meta tells us and every 30 minutes (checked: `sync-whatsapp-templates` runs every 1800s), and that "Sync from WhatsApp" checks now.
- **The WhatsApp preview is one component.** The builder's page-level `<style>` moved to `assets/app.css`, unlayered as before. The campaign composer's preview now uses the same `.wa-preview` classes, so it also gets WhatsApp's real dark colours. Raw colours remain only for WhatsApp's own screen.
- **Tokens.** WhatsApp and Instagram drift went from **171 to 0**, and the whole site from 783 to 611. Grey text became `text-ink-subtle`. Low-contrast greens, ambers and reds moved to steps that pass, or to `tone-*-fg`.
- **Smaller fixes:**
  - the manual number form's labels are tied to their fields
  - the webhook rows are a real `<dl>`
  - the health ticks are named Done / Needs attention / Not yet
  - the campaign breadcrumb has `aria-current`
  - Disconnect and Delete use `btn-danger-ghost`
  - the Instagram setup error and empty state use the shared alert and empty-state components
- **A new test compiles every template** (`test_templates_compile.py`). A broken block in a page that no test renders would otherwise reach production as a 500. This phase produced one, and an Instagram test caught it.

### Automated results

| Command | Result |
|---|---|
| `poetry run pytest` (full suite) | **5,010 passed, 25 skipped, 0 failures** |
| `ruff check` / `ruff format` / `mypy` (changed files) | Pass / pass (one file reformatted) / no issues |
| New tests | `test_wa_ui_quality.py` (4), `test_ig_ui_quality.py` (3), `grid-comma` detector fixtures (3), template compile (one per template) |

### Browser verification (automated, headless Chrome; every POST intercepted, other hosts blocked)

**`tools/qa/channels_qa.mjs`: 17 of 17 pass.**
- The number details grid lines up, and the number shows its health.
- Instagram badges are truthful.
- All four template statuses carry their meaning and the freshness note.
- Recipient outcomes are real. The Failed filter shows only failures, and Sent stays empty until WhatsApp confirms.
- Campaign page at 375: no sideways scroll, rows stack, the long name wraps, and 0 contrast failures in light and dark.
- Template builder: leaves freely when clean, asks once something is typed, keeps the text on "Stay", and its own submit doesn't count as leaving.
- The composer and the rule form ask only after a change.
- The preview uses WhatsApp's colours in both themes.

**Area scan (`area_scan.mjs`), 12 screens at 360 and 1280, light and dark:** 0 contrast failures, 0 overflow, 0 clipped controls, 0 unlabelled fields, 0 phone fields under 16px. The scan now disables the browser cache: its first run measured a stale `app.css`.

Screenshots are in `docs/design/qa/phase4/` (local only).

### Backend gaps (need separate authorization)

- **G5, rejection reason:** Meta's template webhook carries a `reason`, but `_process_template_status_change` doesn't store it. A rejected template therefore can't say why. The list doesn't guess.
- **G6, "Retry N failed" vs the Failed count:** retry counts only recipients marked failed. The Failed stat and filter also include cancelled messages and ones Meta reported failed after sending. So the button can say "Retry 10 failed" next to a Failed count of 12.
- **G7, the template sync doesn't stop campaigns:** a pause or rejection that arrives by webhook stops running campaigns, but the same change arriving through the 30-minute sync doesn't. The copy doesn't claim campaigns are stopped.

### Not verified yet

- Real Meta states (Meta sandbox, yours):
  - a real number's health
  - a real template moving through review
  - a real campaign's delivered and read receipts
- Real devices and a screen reader (yours).

### QA data added (local database only)

All of it is named "QA P4" or `qa_p4_`:
- the WhatsApp number `QA-P4-000` (no token)
- four templates
- the list "QA P4 list", with five contacts
- campaign 2, with one recipient per outcome
- two Instagram accounts, `QA-P4-IG-1` and `QA-P4-IG-2` (fake token; one expired)

Delete them in the Django admin, or restore `../automator-db-backups/`. The dev server ran with `WHATSAPP_ENABLED=True` set inline; `.env` is unchanged.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done.** G5–G7 are held as backend gaps. |
| Verification | **Done**: full suite, 17/17 browser tasks, and the 48-load scan. **Open:** Meta sandbox, real devices, screen reader. |
| Acceptance | **Pending your sign-off.** |

## Entry 11: Phase 5 (billing), 2026-10-28

Phase 4 was committed by you (`4e9468d`). You said "continue", which I took as the go-ahead for Phase 5. Phases 6–9 stay on hold until you say otherwise.

### Tested code

| | |
|---|---|
| HEAD | `35db79d` |
| Tracked changes | 13 files, +304 −130. This includes `docs/ops/instagram-app-review.md`, which was already modified before this phase; it isn't mine. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `d334974cda07e571…` |
| Untracked (SHA-256 prefix) | `apps/billing/tests/test_billing_ui_quality.py` `8c988254…` · `tools/qa/billing_qa.mjs` `bdd984de…` |

### Defects found and fixed

| # | Defect | Fix |
|---|---|---|
| 1 | **The plan cards didn't show what checkout charges.** The browser multiplied the monthly price and rounded it to whole units (`toFixed(0)`); checkout charges `pricing.price_for` to the cent. | Each card carries its exact price for every term, worked out on the server with `price_for` and the money formatter. Checked live: $19.00 / $54.15 / $193.80 / $342.00, as checkout would charge. |
| 2 | **After a bank transfer was sent in, the page still said "Payment required" and offered "Complete payment"** and every plan's checkout. Paying again online would have paid twice. | While a transfer is in review, the page says when it was sent and its reference, that the plan switches on once it's confirmed, and "You don't need to pay again". The checkout buttons give way to "Bank transfer in review". An Incomplete subscription's badge reads "Payment in review". |
| 3 | **A double click could start two checkouts or send a transfer twice.** The checkout links and pay links are plain links. The bank-transfer form is `hx-boost="false"`, so the shell's double-submit protection doesn't cover it, and the server keeps every submission (two review requests, two admin emails). | New shared `data-busy-link` / `data-busy-submit` (`app.js`): the first click marks the control busy (spinner, `aria-busy`), and later clicks or submits are ignored. `pageshow` resets them, so Back from the provider leaves the page usable. Checkout links are `hx-boost="false"`, since they leave for the provider. |
| 4 | **The pending-payment page wasn't truthful at its edges:** | Each state is said as the record says it, while polling:<br>• **Pending:** "Confirming your payment".<br>• **Check failing:** "We can't check right now. We'll keep trying."<br>• **Failed or abandoned:** "didn't go through, you haven't been charged".<br>• **Requires action:** "needs another step".<br>• **After 2 minutes:** "Not confirmed yet… your plan switches on by itself once it does. Don't pay again".<br>• **Succeeded:** the page reloads, and the return view, which doesn't verify again for a succeeded payment, confirms and moves on.<br>• **Unknown reference:** "We couldn't find this payment", with nothing polled. |
| | • it said "Confirming your payment" for a payment it couldn't find, and polled nothing | |
| | • it ignored failed status checks | |
| | • it stopped checking after 2 minutes without saying so | |
| | • it sent any final state to the plans page without saying what happened | |
| 5 | **R1, the plans page was 440px wide on a 360 phone.** The compare table widened the phone's layout even inside its scroll box (removing the table brings it back to 360). | Below md the compare table stacks: one card per feature, listing each plan (`.table--cards`, with each cell labelled by its plan). |
| 6 | **The usage warning promised held messages are "not lost".** Campaign messages held by the plan limit are dropped after `WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS` (Phase 4). | For WhatsApp message limits it now says "Campaign messages that wait more than N hours aren't sent", using the real setting (new `campaign_hold_hours` tag). Email and other held limits keep their wording. |

### Other changes

- The current-plan status is a badge (Active, Trialing, Incomplete, Past due, Expired, Cancelled), not coloured text that measured 3.3:1.
- The term selector is the shared segmented control, with `aria-pressed`.
- "Most popular" uses `.badge` instead of `bg-blue-600` and 10px text.
- The card buttons are the standard `.btn` classes, not `bg-gray-900`/`bg-blue-600`.
- The compare table says "Not included" to screen readers.
- Billing drift went from **37 to 0** (whole site 611 → 574).

### Automated results

| Command | Result |
|---|---|
| `poetry run pytest` (full suite) | **5,054 passed, 25 skipped, 0 failures** |
| `ruff check` / `ruff format` / `mypy` (changed files) | Pass / pass (one file reformatted) / no issues |
| New: `test_billing_ui_quality.py` | 4 tests: card price equals checkout's for every term; a transfer in review isn't asked for again; an unknown payment isn't "confirming"; held campaign messages aren't promised forever |
| Note | `test_phase4_load` (contact import, 5,000 rows, 60s budget) failed once on its own at 140s and passed in the full run. It's timing-dependent and doesn't touch billing. |

### Browser verification (`tools/qa/billing_qa.mjs`, headless Chrome)

No payment was made:
- checkout was answered by the harness and never reached the server or a provider
- every POST was intercepted, and other hosts were blocked
- the only real write was a local manual checkout (a pending manual payment row)

**Part A, 17 of 17 pass:**
- the card price matches checkout for all four terms, and the selector's pressed state is correct
- the plans page scans clean at 1280 in light and dark
- the plans page at 360 has no sideways scroll and scans clean in light and dark
- one click starts one checkout (two back-to-back clicks: busy after the first, one request)
- busy is cleared on return
- the pending page: confirming, check failure, failed, needs another step, stopped checking, not found (0 polls), and a clean scan at 360
- the bank-transfer form sends once (two clicks plus a scripted `requestSubmit`: one POST)

**Part B, 1 of 1 passes:** a transfer in review shows the note and reference, with no checkout links.

Screenshots are in `docs/design/qa/phase5/` (local only).

**Notes from running it:**
- The QA Redis container had stopped with Docker Desktop. Logins then fail with a 500, which looks like a harness login failure. I started Docker Desktop and the container again.
- A CDP mouse click only returns once the navigation it starts has finished, so the double-click checks run inside the page.

### Backend gaps (need separate authorization)

- **G8, bank-transfer submissions aren't deduplicated:** `manual_submit` creates a new `ManualPaymentRequest` (and admin email) on every POST for the same pending payment. The page now blocks a double click, but the server would still accept a repeat, for example a resubmit after Back.
- **G9, a transfer with no reference goes back through checkout:** a submit without a reference (only possible without the browser's `required` check) redirects to checkout. Checkout abandons the open payment and creates a new one, and the uploaded proof is lost.

### Not verified yet (yours)

- A real provider payment through Flutterwave or the PayPal sandbox: success, decline, and a webhook arriving after the return.
- A real bank-transfer review in the console.
- Real devices and a screen reader.

### QA data

Created and removed in this phase:
- the bank-transfer method was switched on, then off again
- 9 local manual payments were created and deleted
- one review request ("QA P5 TX-0001") was created and deleted

The QA workspace's billing is as it was: on Trial, with no payments.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done.** G8 and G9 are held as backend gaps. |
| Verification | **Done**: full suite, 18/18 browser tasks. **Open:** a real provider payment, real devices, screen reader. |
| Acceptance | **Pending your sign-off.** |

## Entry 12: Landing page redesign, 2026-10-29

You asked for the landing page to be redone: premium, responsive and elegant. You chose:
- **Direction:** refined dark navy.
- **Copy:** the same registered claims, restructured.
- **Pictures:** illustrated product mock-ups, not screenshots.
- **Sections:** keep the developer section, drop the "Coming soon" strip, and add no new sections.

### Tested code

| | |
|---|---|
| HEAD | `fcccef6` (Phase 5 committed by you) |
| Tracked changes | 5 files, +76 −951. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `78b6a73163ca8e85…` |
| Untracked | `static/css/landing.css`, `templates/accounts/landing/` (12 sections and 8 illustrations), `apps/accounts/tests/test_landing_redesign.py`, `tools/qa/landing_qa.mjs`. Combined SHA-256 `1f8f2367d7a0288a…` |

### What changed

- **Structure:**
  - `landing.html` is now a short shell (header, includes, footer, about 2KB of script).
  - Each section is a partial in `templates/accounts/landing/`.
  - The 360-line inline `<style>` became `static/css/landing.css`. It uses `app.css` tokens only (no raw colour values), with `lp-` classes so nothing collides with the app's components.
- **Look:**
  - Display type: h1 is 40px on phones and 72px on desktop.
  - Soft aurora glow and grid behind the hero.
  - Pill navigation that turns solid on scroll.
  - Teal identity accents, and amber kept for the one primary action.
  - Sections fade and rise in once; none of this happens under reduced motion, and everything is visible without JavaScript.
- **Hero:**
  - Copy on the left and an illustrated inbox on the right: a customer's question, the assigned teammate's reply, an "Order · Payment link sent" card and a "Follow up tomorrow" reminder.
  - Stacked on phones.
  - With WhatsApp off it shows a customer record with an email and an order instead, and never mentions WhatsApp.
- **The journey:** each step now has its own illustration.
  - **Attract:** the post with its chat link.
  - **Engage:** the closed-hours auto-reply, then the teammate's reply.
  - **Convert:** the interested customer, the order and the payment link.
  - **Measure:** post results as bars with no figures, plus the lower-bound note.
- **Content Studio:** an illustrated composer showing the version per channel, an AI-drafted caption, the time per channel, the week calendar and owner approval.
- **Features:** a bento grid with a small illustration in each tile: a keyword auto-reply, a workflow (new lead → wait 1 day → follow-up, all real step types), and Business Health bars. Two balanced rows, with no half-empty tile.
- **Unchanged in substance:**
  - trust (now glass cards)
  - pricing: same plan data and wording, with a gradient border on the featured plan
  - FAQ: two columns, `+`/`−` toggles, plain `<details>`
  - developers, the final CTA (now a rounded navy panel) and the footer
- **Truthfulness:**
  - Every illustration is a `<figure>` with `role="img"`, a full description and a visible "Illustration of…" caption.
  - Sample text inside is `aria-hidden`, and channel badges follow the channels that are on.
  - Results show no numbers, because a sample figure would read as a result we can't prove (messaging.md §6).
  - Copy uses the existing claim register only.
  - **Fixed along the way:** the Instagram card said "DMs arrive in the same inbox as WhatsApp" even when WhatsApp was off.
- **Removed:**
  - the "Coming soon" strip (still shown inside plan cards)
  - the screenshots from the landing page (`img/marketing/` stays for the use-case pages)
  - the now-unused `coming_soon` query in `landing()`

### Automated results

| Command | Result |
|---|---|
| Landing tests: `test_landing`, `test_landing_claims`, `test_landing_redesign` (new), `test_landing_journey`, `test_accounts_ui_quality` | All pass. The claim register passes with WhatsApp on and off, and Meta open and closed. |
| `poetry run pytest` (full suite) | **5,078 passed, 25 skipped.** 4 failed: 3 landing-journey tests that expected the old screenshot and the dropped strip (updated, now passing), and the known timing-dependent `test_phase4_load`. |
| `ruff` / drift | Clean. The drift total is unchanged at 574, and `templates/accounts` stays at 0. |

### Browser verification (`tools/qa/landing_qa.mjs`, headless Chrome, logged out, other hosts blocked)

**26 of 26 pass with WhatsApp on, and 26 of 26 with WhatsApp off.**
- **Widths:** 360×740, 390×844, 844×390 (phone landscape), 768×1024, 1024×768, 1280×800 and 1440×900. At each width:
  - no sideways scroll
  - 0 contrast failures
  - no targets under 24px, apart from links inside sentences (WCAG 2.5.8 inline exception)
- **Keyboard:** the skip link comes first and shows; the menu opens with Enter, Esc closes it and focus returns; FAQ opens with Enter, with a visible focus ring.
- **Reduced motion:** nothing hidden, nothing animating.
- **No JavaScript:** every section visible.

**Found by looking at the screenshots and fixed:**
- the hero cards covered the WhatsApp chip and the caption
- a half-empty Talk tile in the bento
- double spacing between the developer section and the final CTA
- phone cards drifting off-screen, because the desktop offsets outranked the phone reset

Screenshots, top of page and full length at every width, are in `docs/design/qa/landing/` (local only).

**Environment note:** Docker Desktop's engine was unresponsive (errors since yesterday's start), so the QA Redis couldn't run. I didn't force-restart Docker, because that would stop your other containers. The landing checks ran against the dev server with a scratch settings file (in-memory cache, database sessions) that lives outside the repo.

### Not verified yet (yours)

- A real phone: iOS Safari and Android Chrome, including the safe-area header.
- A screen reader pass over the illustrations' descriptions.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** |
| Verification | **Done**: tests and 26/26 browser checks, in both channel set-ups. **Open:** real phone, screen reader. |
| Acceptance | **Pending your review of the page** (`/` on the dev server, or the screenshots). |

### Review fixes (same day)

A code review raised nine findings. All nine were confirmed and fixed:

| # | Finding | Fix |
|---|---|---|
| 1 | The hero illustration without WhatsApp always showed an email, even with email off | `art/_customer.html` shows the last message on a channel that's on (email, else Instagram DM, else Facebook Page message, else none); the description follows |
| 2 | The hero badge "For businesses that sell through chats" carried no claim and showed for email-only | Shown only when a chat channel is on, tagged `data-claim="lost-enquiries"`. The lead under the h1 now also has Instagram/Facebook-only and no-channel versions, so it never names email when email is off |
| 3 | Two channels on gave a three-column grid with an empty column | Columns follow the card count (`:has()`): one card centred, two in pairs, three or four on one row at 1100px |
| 4 | The mobile menu reused the app's `.nav-link` (dashboard hover and focus styles leaked in) | `.lp-mlink`; the page-wide `.lp` focus ring applies |
| 5 | Raw `rgb()` colours in `landing.css` and a hard-coded `theme-color` | 34 values are now `color-mix()` of app tokens. The palette test bans `rgb()`/`hsl()` and pins `theme-color` to `--color-gray-950` |
| 6 | The coming-soon test no longer checked the plan cards | It attaches an active entry to a plan and asserts it appears in `#pricing` |
| 7 | Help had gone from the desktop header | Back in the primary nav. Links no longer wrap; the full nav starts at 1080px (it wrapped at 960 with six links) |
| 8 | Sections hidden under `html.js` could stay invisible if the script failed, or in print | The `js` class is set by the script, at the same moment the reveal observer starts, and first. Print shows everything. The hero art's entrance no longer depends on the class |
| 9 | Pricing reveal delays past `d3` did nothing | Capped at `d3` |

**Results:**
- Landing tests: 58 passed (new: hero channel matrix ×4, badge claim, classes and motion).
- `ruff`: clean. Drift: unchanged at 574.
- Browser: 26/26 with WhatsApp on, and 26/26 with WhatsApp and email off. The header was also checked by eye at 960, 1024 and 1080.

**Not changed (pre-existing, flagged):** the developer section's email API list and code sample show even when email is off. The old page did the same.

### Hero refinement and exact prices (same day)

You said the hero felt off. Six causes, all fixed in `landing.css` (hero block), `_hero.html` unchanged in copy:
- **Alignment:** the window is now level with the copy (the figure's top margin balances the caption).
- **Columns:** `minmax(0,1fr) minmax(0,1.1fr)`, with a wider gap.
- **Headline:** "in your inbox." is its own line, so "in" is never stranded.
- **Floating cards:** they overlap only empty window edges (order top-right, reminder bottom-left over a spacer) and never cover content.
- **Depth:** a soft token backlight and a gradient ring around the window.
- **Phones:** left-aligned copy and full-width stacked buttons.
- **Inbox list:** now follows the window's width (container query), so the two-pane inbox no longer squeezes at 1024px.

**Prices:** the landing cards rounded prices (`floatformat:0`: $11.90 showed as $12). They now use billing's `money` filter, the same as the Plans page. New test: `test_plan_prices_are_exact_not_rounded`.

**Results:**
- Landing and billing tests pass.
- Browser: 26/26 with WhatsApp on, and 26/26 with it off.
- Hero checked by eye at 390, 1024, 1280 and 1440.

### Second review (same day): 10 findings

**Fixed:**
- **Price test:** pins `BILLING_CURRENCY=USD` and reuses the `plans` fixture. `Decimal` is imported at the top. New test: prices follow the billing currency (ZMW shows `K49.00`, never `$`).
- **Trust grid:** the 2-column layout (an edit made outside this session) kept. An odd last card spans the row, so 3 or 5 cards never leave one stranded. The duplicate 1024px rule is removed.
- **Headline gradient:** `width: fit-content`, so the gradient spans the words (centred on tablet).
- **Reminder card:** no wrapping; the empty strip is 3.25rem against a reach of about 2.3rem.
- **Inbox list:** the container threshold is now 35rem, so stacked 560–639px phones show the thread alone. Hiding the list on small laptops is deliberate.
- **Order card:** overhang reduced to 1rem, inside the narrowest desktop gutter (2rem).
- **Bank-transfer review email:** the fallback templates (`.html`/`.txt`) use `money`.

**Kept, with reasons:**
- **The landing page follows `BILLING_CURRENCY`:** plans are priced in it (`apps/billing/money.py`), so the old literal `$` was the bug. Whole numbers in a zero-decimal currency are correct.
- **`manage/plan_costs.html` and `manage/business.html`:** price sits beside Akilent's own costs and margin, which are worked out in dollars. Switching only the price's currency would make the margin row mix currencies.

**Open (yours):** the bank-transfer email's stored `SystemEmailTemplate` row (seeded by `apps/email/migrations/0015`) still says `${{ plan.price_monthly }}`. It's used ahead of the fixed files. Edit it in Manage, or authorize a data migration.

**Results:**
- Landing, billing, email and core tests pass. `ruff`: clean. Drift: 574.
- Browser: 26/26 (WhatsApp off).

### "How it works" redesign (same day)

**Before:**
- **WhatsApp off:** three small email-only glass cards ("Connect your domain"), even with Instagram and Facebook on.
- **WhatsApp on:** four rows that weren't connected to each other, with a WhatsApp badge always in the Engage picture.

**Now:** one illustrated timeline in every channel setup.
- **The rail:** a line runs down the middle on desktop (on the left on phones), and its teal segments fill as each step appears.
- **Step numbers:** discs on the rail, numbered 1…n by a CSS counter.
- **Feature tags** under each step.
- **Pictures:** each stands on a stage (glow, dot grid, hairline frame) with the hero's gradient ring.
- **Step map:** the heading links to each step.

**Steps follow the channels:**
- **Attract:** Content Studio, with a WhatsApp chat link, or comment rules without WhatsApp.
- **Engage:** names only the chat channels that are on; the badge shows the first one.
- **Reach:** when only email is on (reuses the customer-record picture).
- **Convert.**
- **Measure:** post results, post performance (`post-metrics`) or Business Health.

The faint outline numerals from the plan were dropped: they collided with the step labels, and the disc on the rail already carries the number.

**Results:**
- New test `test_the_journey_follows_the_channels` covers five setups: steps in order, no channel that's off named, illustrations labelled, step map matches.
- Landing tests pass. `ruff`: clean. Drift: 574.
- Browser: 26/26 with WhatsApp on (a 2px overflow at 360 was found and fixed: the step column now shrinks) and 26/26 with it off.
- Checked by eye at 1440 in both setups, and at 390.

### Content Studio refinement (same day)

**Before:**
- a flat white composer on a white band
- a four-item tick list
- a "measurable results" headline with nothing in the section to back it when WhatsApp is off
- a picture that showed one time for every channel

**Now:**
- **Cards:** four feature cards in a 2×2 grid, plus a full-width tinted "measure" card. It shows post results through the WhatsApp chat link, or each post's reach and engagement without WhatsApp (`post-metrics`).
- **Composer:** stands on a light stage (the shared `.lp-stage-panel`).
- **Composer details:**
  - a "2 versions" counter
  - a real post thumbnail
  - a time row for each live channel (whose dots double as the calendar legend)
  - an "Approved by Ruth" card on the title-bar corner

**Channel fixes found by the new test:**
- The first card named both Instagram and the Facebook Page even with only one on. With one channel on, it now says "Publish to Instagram" / "Publish to your Page" (`instagram-publishing` / `facebook-publishing`).
- "One post, every channel" was renamed to "One post, a version for each", because "every channel" is a banned phrase (`test_inbox_is_not_claimed_for_channels_that_do_not_arrive_there`).

**Results:**
- New test `test_content_studio_backs_its_headline_and_follows_the_channels` covers four setups.
- Landing tests pass. `ruff`: clean. Drift: 574.
- Browser: 26/26 with WhatsApp on and 26/26 with it off. A 2px overflow at 360 was found and fixed: the studio column now shrinks.
- Checked by eye at 1440 and 390.

---

## Entry 13: Phase 6, automations and forms (2026-10-29)

You said to continue with the remaining phases. This is Phase 6. The exit-gate tasks:
- edit a workflow
- pause and resume
- leave with unsaved edits (and be warned)
- a failed save keeps the edits

### Tested code

| | |
|---|---|
| HEAD | `6992545` |
| Tracked changes | 19 files, +272 −156. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `bd4af18add166a8e…` |
| Untracked | `apps/automation/tests/test_automation_ui_quality.py`, `tools/qa/automation_qa.mjs`. Combined SHA-256 `f61592e8732174be…` |

### Defects found and fixed

| # | Sev | Defect | Fix |
|---|---|---|---|
| D1 | High | **Flash messages were lost after many saves app-wide.** A boosted form posts and redirects, and htmx fetches the next page. If that page ships its own scripts, the shell throws the render away and loads the page for real. The render had already used up the message, so "QA P6 Tag helper is on." and similar confirmations never showed. | `ShellMiddleware._reload` (`apps/core/htmx.py`) marks the discarded page's messages unseen, so Django keeps them for the real load. An end-to-end test fails without the fix and passes with it. |
| D2 | High | **The workflow editor, form builder and post composer ran `init()` twice.** `x-init="init()"` came on top of Alpine's own call, so every edit started two saves. Each boosted visit also left window listeners behind. The composer's stale `online` listener could autosave a draft its user had chosen to discard. | `x-init` removed. `destroy()` removes the listeners. The editors' own `beforeunload` handlers are gone, because the shared `akDirty` guard covers leaving. A test guards against `x-init="init()"` anywhere. |
| D3 | High | **Publish / Turn on published the server's draft while an edit was still waiting for autosave**, and the browser asked "Leave site?". | Publish saves pending edits first and stops with the reason if that save fails. |
| D4 | Med | **Editor save:** every failure that threw (including an HTML 500) read "You're offline. Your changes will be saved when you reconnect", and nothing saved on reconnect. An edit made during a save was marked saved. A save in flight didn't count as unsaved work. | Failures are classified with `akFailure` and keep the edits ("Click Save to try again"). Saves retry when the connection comes back. A revision counter keeps later edits dirty. Status reads "Saving… / Saved / Unsaved changes". |
| D5 | Med | **Form builder:** only guarded closing the tab (in-app navigation dropped unsaved questions). Two saves could overlap, so an older version could land last. Any failure read "Check your connection", with no retry. | Uses the shared guard. One save at a time. Classified failures with a Try again button and a retry on reconnect. |
| D6 | Med | **Statuses:** the stats page called a paused automation "Draft". The list didn't show edits waiting on a live automation. Drafts had a warning badge and no next step. | Each `Workflow.Status` has its own label. "Unpublished changes" with "Review changes". "Draft · off" with "Finish and turn on". Form status shows as a badge. |
| D7 | Low | **Filtered empty lists said nothing existed** ("Nobody here yet" on the Failed tab). | "No customers match" / "No answers match", with a way back to all. |
| D8 | Low | **Dark-mode contrast:** the step number used `bg-white` (3.3:1), and the WhatsApp preview button used fixed brand-600 (2.05:1). | Surface and accent tokens. |
| D9 | Low | **Targets under 24px:** breadcrumbs (app-wide), "More options" / "Remove", the "+" step button, checkbox labels. Plus a 58px sideways scroll at 390 from the step toolbar. | Breadcrumb links get a 24px minimum in `app.css`. `min-h-6` on the rest. The toolbar wraps. |

**Drift:** `templates/automation` 102 → 0 (`text-gray-400/500` → `text-ink-subtle`, `text-[11px]` → `text-xs`). Total 574 → 472, and the baseline was lowered.

**Scanner change (`tools/qa/ui_scan.js`):** two exemptions, both following WCAG 2.5.8:
- a link or button rendered inline inside a sentence (the inline exception)
- a checkbox inside its label, which is measured by the label

### Automated results

| Command | Result |
|---|---|
| `poetry run pytest` (full suite) | **5,109 passed, 25 skipped, 0 failed.** |
| `test_automation_ui_quality.py` (new) | 10 pass: status labels, the stats page when paused, filtered empty state, Publish saves first, single `init()` everywhere, composer listeners released, the message surviving the full load. |
| `ruff check`, `ruff format`, drift | Clean. Drift 472. |

### Browser verification

**`tools/qa/automation_qa.mjs`: 25 of 25.** Headless Chrome, signed in as the QA user (dark theme). Save, publish and form-save posts are answered by the harness. Pause and resume ran for real on the seeded `qa-p6` workflow.
- **Scans:** list, editor, runs, filtered runs, stats, forms list and form builder at 1280 and 390. No sideways scroll, no contrast failures, no targets under 24px.
- **Editor:**
  - one edit makes one save
  - an in-app navigation with unsaved edits asks first, and "Stay" keeps them
  - an HTML 500 keeps the edits and says so (not "offline")
  - offline says so, then saves by itself when the connection returns
  - an edit made during a save stays unsaved
  - Publish with a pending edit saves first, then publishes, with no browser prompt
- **List:** Pause then Turn on, with each badge matching the backend and "…is on." shown.
- **Form builder:** one save per edit; a failed save shows "Couldn't save. Your changes are still here." with Try again; leaving asks first.

**Regression:** `tools/qa/publishing_qa.mjs` 21/21 after the composer change.

Screenshots are in `docs/design/qa/phase6/` (local only).

### Backend gaps

None new. The save endpoints store the whole draft, so a retry is safe and no deduplication is needed. G5–G9 still need your authorization.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** |
| Verification | **Done**: tests and 25/25 in the browser. **Open:** a screen-reader pass on the save status, and a real phone. |
| Acceptance | **Pending your review** |

### Review fixes (same day): 8 findings

A code review raised eight findings. All eight were confirmed and fixed.

**Fixed:**
1. **Code tab (High):** Save read the code back in, the watcher counted that as an edit, and "unsaved" never cleared. Autosave then repeated every 2.5s, and Publish did nothing. Now:
   - Dirtiness is based on content. An edit counts only if the draft differs from what was last saved or is in flight.
   - Publish lets its own resubmit through. On the Code tab it used to loop.
2. **Form builder, overlapping saves (High):** the one-save guard read `saveState`, which an edit during a save resets to "dirty". It now has its own in-flight flag.
3. **Form builder, question keys (High):** keys from the server were copied on by position. The server drops blank questions, so a blank question, or one added during a save, could take another question's key. Keys now map to the questions sent that the server keeps, and only onto ones still on screen without a key.
4. **Publish with broken code:** Publish stopped silently. It now says "Couldn't save: the code has a mistake…".
5. **Publish waiting:** Publish polled `busy` every 100ms. It now awaits the save in flight; a second click while waiting does nothing, and the button shows busy.
6. **Duplicated wording:** two hand-written failure-wording tables and two copies of the CSRF regex. Both now use `akFailure.draftSaveMessage` and `window.akCsrf` in `app.js`.
7. **Unknown answer filter:** `?status=bogus` claimed "No answers match". The view now treats an unknown filter as All.
8. **Composer timers:** a composer you'd left kept its caption poll and undo timer running. `destroy()` now clears them.

**Results:**
- Tests: automation, conversations, core and publishing pass, including three new unit tests.
- Browser: `automation_qa.mjs` **30/30**, with five new checks:
  - Code-tab save settles with one save
  - Code-tab publish saves then publishes
  - broken code stops Publish and says why
  - one form save in flight
  - keys land on the right question
- `publishing_qa.mjs` 21/21.

A harness flaw was also found and fixed: evaluating `save()` waited for its promise, so the timing check started too late.

## Entry 14: Phase 7, Insights and analytics (2026-10-30)

You said to continue. This is Phase 7. The exit-gate task: each dashboard loads, fails gracefully, and shows empty and sparse data honestly, telling "zero" apart from "unknown".

Scope: Business Health (`/insights/`, all partials, including the Email section), the automation policy pages, the shared line chart, and the dashboard's lazily loaded panels.

### Tested code

| | |
|---|---|
| HEAD | `60ef3a2` |
| Tracked changes | 26 files, +275 −195. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `5076ffd61c24f2ec…` |
| Untracked | `apps/insights/tests/test_insights_ui_quality.py`, `tools/qa/insights_qa.mjs`. Combined SHA-256 `0961f15c524acc6e…` |

### Defects found and fixed

| # | Sev | Defect | Fix |
|---|---|---|---|
| D1 | High | **A week with nothing to measure was drawn as a perfect week.** The chart turned every missing reading into 0, so a week with no reply to time showed a 0-minute median reply. A week with no snapshot was skipped, so the line ran straight across it. | The shared chart (`apps/core/charts.py`, `{% line_chart %}`) treats `None` as "no reading": the line breaks, the table shows "—", hover says "No data", and no marker is drawn. A reading cut off by gaps on both sides is drawn as a dot. Momentum passes `None` for those weeks and adds an empty column for a missing week. A clock change isn't taken for a missing week. |
| D2 | Med | **One week was drawn as a "trend".** | Charts start from two counted weeks. With one, the table shows and a sentence says when the charts will start. |
| D3 | Med | **A chart with nothing recorded rendered nothing**, leaving an empty card. | It now says "nothing recorded for this period yet". |
| D4 | Med | **Email engagement:** days with no opens were left out, so the line joined distant days. The chart is all-time, while the page's period selector suggested otherwise. | Every day from the first event to the last is a column (a day without opens is a real 0). The card says it covers everything since tracking began. |
| D5 | Med | **A failed deliverability score made the card silently disappear.** | It shows "Couldn't work out the deliverability score for this domain right now". (`error` was already rendered but never set.) |
| D6 | Med | **No freshness.** Nothing said when the figures were from. | "Figures as of 30 Oct, 06:11. Reload for the latest." Each insight says when it was spotted. Momentum says which week it counts up to. |
| D7 | Med | **The empty proof bar assumed WhatsApp** ("Once WhatsApp messages start coming in"). | "Once customers message you on a connected channel". |
| D8 | Med | **Removing a goal happened in one click.** Deleting a policy used its own inline "Sure?" pattern. | Both use the shared confirm dialog with danger styling. The goal and policy are named, and the copy says the policy can't be brought back (the view hard-deletes it). |
| D9 | Med | **Policy editor:** hand-styled fields with a blue focus ring and `dark:` overrides. The number fields had no accessible name. The back link was an unnamed icon. Each flash message showed twice (inline and as a toast). No leave guard. | Shared `.input`/`.select` fields. Each "No reply for [n] hours" row is a `<label>`. "Back to policies" is named. The inline message list is removed. `dirtyGuard` plus a busy submit. |
| D10 | Med | **Dark-mode contrast:** severity icons in stock red/amber/emerald/blue, `text-brand-800` advice text, and red/amber run states on the policies page. | Tone tokens (`tone-coral-fg`, `tone-amber-fg`, `tone-teal-fg`, and a new `tone-info-fg` utility). |
| D11 | Med | **The dashboard's panels stayed a skeleton forever when their load failed** (`aria-busy="true"` stayed set). | The region opts into the shared error state: "Couldn't load these panels", with Try again. `app.js` clears `aria-busy` on any region that shows a failure. |
| D12 | Low | **Targets under 24px** on the dashboard panels: "View all", "View activity log" and setup-step links. Also the weekly-report checkbox label. | `min-h-6`. |

**Drift:** `templates/insights` 51 → 0. Total 472 → 421, and the baseline was lowered.

### Automated results

| Command | Result |
|---|---|
| `poetry run pytest apps/insights apps/conversations apps/email apps/core apps/accounts` | Everything else passed, with 2 failures. **Mine:** `test_an_empty_series_renders_nothing_rather_than_empty_axes` failed because D3 changes that behaviour deliberately; it is rewritten to check there are still no empty axes, plus the sentence. **Known:** `test_import_5000_contacts` (72.9s against a 60s budget) also fails on a clean checkout and has nothing to do with this phase. |
| `test_insights_ui_quality.py` (new) | **24 pass:** chart gaps (line break, no fill across gaps, scale, all-gaps means no data, zero is data, isolated dots), table dash and tooltip, the empty chart, momentum gaps, missing weeks, the clock change, one week vs two, email day filling, the failed score, freshness, channel-neutral copy, both confirms, the policy editor, tokens only, the dashboard error region. |
| Chart tests (`test_charts.py`, `test_chart_rendering.py`) | Pass after the rewrite above. |
| `ruff check`, `ruff format`, drift | Clean. Drift 421. |

The full suite was not re-run for this phase.

### Browser verification

**`tools/qa/insights_qa.mjs`: 23 of 23.** Headless Chrome, signed in as the QA user. Data from scratchpad `p7_seed.py`: four weekly snapshots with one missing week and one week with no reply to time, an open insight, a goal and a policy. Every POST is answered by the harness, so nothing was written.
- **Scans:** Insights, policies, the policy editor and the dashboard, at 1280 and 390, in **both themes**. No sideways scroll, no contrast failures, no targets under 24px.
- **Freshness:** the page, insight and momentum timestamps are shown.
- **Reply chart:** three separate readings drawn as dots, "—" in the table for the empty week and the missing week, no "0". Hovering the empty week says "No data" with no marker.
- **Goal removal and policy delete:** both ask first, and Cancel sends nothing.
- **Policy editor:** the number field is named "Customer inactive for days". Leaving with a change asks "Leave without saving?", and Stay keeps you on the page.
- **Dashboard panels:** a 500 shows "Couldn't load these panels" with Try again, no skeleton, and `aria-busy` cleared. Try again then loads the panels.

**Found by the browser, not the tests:**
- the dashboard's three small links
- readings cut off by gaps drew as 2px specks, which is why the dot was added

**Harness flaw fixed:** the app's own usage beacon (`/internal/ux-event/`) was counted as a sent change.

Screenshots are in `docs/design/qa/phase7/` (local only).

### Backend gaps

None new. All the changes are presentation; no backend states or rules changed. G5–G9 still need your authorization.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** |
| Verification | **Done**: tests and 23/23 in the browser. **Open:** a screen-reader pass on the charts (table view and hover), and a real phone. |
| Acceptance | **Pending your review** |

## Entry 15: Phase 8, contacts, interested customers, orders, starter packs, chatbots and support (2026-10-30)

You said to continue. This is Phase 8. The exit-gate task in each area: list, filter, detail, edit, and confirm before deleting.

**Scope:** contacts (the reference area), CRM ("Interested customers"), commerce (orders), verticals (starter packs), chatbot, support (agent inbox and customer portal) and scheduler.
- **seo:** only a meta-tag partial; no UI.
- **Scheduler:** `templates/scheduler/index.html` is dead code. Its view now redirects to Campaigns → Scheduled. It was swept for tokens but not reworked; you may want to delete it.

### Tested code

| | |
|---|---|
| HEAD | `12b628c` |
| Tracked changes | 42 files, +347 −299. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `139ce74c58e9e298…` |
| Untracked | `apps/core/tests/test_small_areas_ui_quality.py`, `templates/chatbot/_status.html`, `templates/commerce/_order_status.html`, `templates/crm/_lead_status.html`, `tools/qa/small_areas_qa.mjs`. Combined SHA-256 `f118bba12320e5a2…` |

### Defects found and fixed

| # | Sev | Defect | Fix |
|---|---|---|---|
| D1 | High | **The order payment card said untrue things.** A failed payment still read "Payment link created — share it with the customer". Cancelled and refunded orders offered "Request payment". | The card follows the records. A pending link is "ready to share". A failed attempt says so and offers "Request payment again". Cancelled and refunded orders say there's nothing to collect. Request payment has a busy state. |
| D2 | High | **A customer's "Orders: 3" link showed other people's orders.** It went to `/orders/?q=<phone>`, which the orders page ignores, so it opened everyone's orders needing attention. | The orders list takes `?contact=<public_id>` (this business's customers only; any other id is ignored). It shows "Showing orders for … · Show everyone's", and the tabs keep the filter. The profile links to it. |
| D3 | Med | **"Needs attention" with nothing waiting looked like a brand-new account** ("Turn conversations into orders… Create your first order"). | "Nothing needs your attention. Every order is paid or closed." with a link to All. |
| D4 | Med | **Orders and leads were unpaginated**, so every row rendered at once. | 50 per page with the shared pagination. Newest first. |
| D5 | Med | **The order's "Customer journey" claimed steps that aren't recorded** (Opportunity, a channel "DM", Lead). It showed "Attributed revenue" for unpaid orders. | Only the recorded links: Insight → Conversation → Order, marked "not paid yet" until paid. An unpaid order shows "Order value, not paid yet". |
| D6 | Med | **"Everyone who showed interest" listed only open leads.** Bought and lost leads were left out. | Renamed "Still deciding" and says who's included. Status badges have tones: bought (success), lost (danger), qualified (info). |
| D7 | Med | **Starter packs changed live automations in one click.** Activate publishes them straight away. Re-sync puts back the starter versions, which replaces your edits and turns paused automations back on. | Both ask first and say exactly that. Re-sync uses danger styling. |
| D8 | Med | **Chatbot and support forms were hand-styled at 14px**, so iOS zooms the page on every field, with their own focus ring. The ticket status, priority, assignee and escalate fields had no labels. The chatbot's Position choice reset after a validation error. | Shared `.input`/`.select`/`.textarea` (16px on phones). Labels added. The position choice is kept. |
| D9 | Med | **A chatbot with no allowed sites said "Active"**, though its widget is refused everywhere. | A shared badge reads "Active · no sites allowed" (warning) in that case. |
| D10 | Med | **CRM pipeline on a phone:** an inline `repeat(N, 1fr)` squeezed every stage into a narrow column. | `.pipeline-grid`: one column on a phone, two on a tablet, one per stage from `lg`. |
| D11 | Med | **Status pickers used the amber primary button for the current state** (lead status, deal stage). The lead page's fields had no linked labels. | The current status is pressed (`aria-pressed`, secondary plus ring). The page's real action stays amber. Labels use `for`/`id`. |
| D12 | Low | **Each chatbot save message showed twice** (inline and as a toast). Icon-only back arrows had no name (chatbot ×5, support portal ×2). | The inline lists are removed. Back arrows are named "Back". |
| D13 | Low | **Targets under 24px:** links in table cells (names, order numbers, sort headers) app-wide, "← Customers"-style back links, the help tip "?", and small links on the contact and chatbot pages. | A shared rule: `.table :is(td, th) a:not(.btn)` gets a 24px minimum. The help tip button gets a 24px target with no layout shift. `min-h-6`/`min-w-6` on the rest. |
| D14 | Low | **The list readiness legend put `<a>` directly inside `<dl>`** (invalid markup). The order and lead pages had no breadcrumbs. | A labelled group of links with `aria-current` on the active filter. Breadcrumbs added. |

**Drift:** contacts, crm, commerce, scheduler and verticals all reach 0. Total 421 → 298, and the baseline was lowered.

### Automated results

| Command | Result |
|---|---|
| `pytest apps/contacts apps/crm apps/commerce apps/chatbot apps/support apps/verticals apps/scheduler apps/core` | All passed except the known `test_import_5000_contacts` timing failure, which also fails on a clean checkout. |
| `test_small_areas_ui_quality.py` (new) | **19 tests, 20 cases, all pass:** filter-aware empty state, pagination and tabs, closed orders, failed vs pending payment, awaiting-payment tone, the customer filter (and refusing another business's customer), the "Still deciding" heading, the pipeline on a phone, lead labels and the pressed status, starter-pack confirms, the chatbot badge (both cases), messages shown once, shared fields, named back links, labelled support fields, valid legend markup. |
| Re-run after the last fixes | Same packages plus drift and template-compile tests: exit 0. |
| `ruff check`, `ruff format`, drift | Clean. Drift 298. |

The full suite was not re-run for this phase.

### Browser verification

**`tools/qa/small_areas_qa.mjs`: 91 of 91.** Headless Chrome, signed in as the QA user. Data from scratchpad `p8_seed.py`: a customer on a list, a paid order, an order with a failed payment, a cancelled order, an open lead, and a chatbot with no sites. Every POST is answered by the harness, so nothing was written.
- **Scans:** 20 pages at 1280 and 390, in both themes:
  - contacts: list, profile, lists, list, segments, fields
  - orders: both tabs and three orders
  - CRM: interested customers and a lead
  - starter packs
  - chatbot: list, page and new
  - support: inbox, portal and new ticket

  No sideways scroll, no contrast failures, no targets under 24px.
- **Phone fields:** every text field on the chatbot, ticket, lead and support pages is at least 16px.
- **CRM:** the pipeline is one column on a phone.
- **Contacts:** search, then profile, then delete, which stays disabled until "delete" is typed. The profile's order count opens exactly that customer's three orders.
- **Orders:** the current tab is marked. The failed payment says so and offers a new link (not "Payment link created"), with an "Awaiting payment" badge. The cancelled order offers nothing to collect.
- **Lead:** the current status is pressed and not amber; the fields are named.
- **Starter pack:** Activate asks first, and Cancel sends nothing.
- **Chatbot:** the bot with no sites shows "no sites allowed".

**Found by the browser, not the tests:** the table-cell link target sizes (app-wide), the help tip, the small links, and the orders filter bug (D2).

**Harness fixes:**
- the lead check matched the header's theme toggle; it is now scoped to the status form
- the theme switch now waits for the colour transitions before scanning (two false light-theme contrast readings)
- one run hit a page-load timeout that didn't recur, and left a headless Chrome holding the port, which was stopped

Screenshots are in `docs/design/qa/phase8/` (local only).

### Backend gaps

None new. The orders customer filter and pagination are read-only list options. No states or rules changed. G5–G9 still need your authorization.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** |
| Verification | **Done**: tests and 91/91 in the browser. **Open:** a screen-reader pass and a real phone (the iOS zoom fix especially). |
| Acceptance | **Pending your review** |

## Entry 16: Phase 9, public pages, email, logs and the Operator Console, plus the full regression pass (2026-10-30)

You said to continue. This is Phase 9, the last area phase. The exit-gate task is a full regression checklist across all areas, both themes, and several widths.

### Tested code

| | |
|---|---|
| HEAD | `ec74057` |
| Tracked changes | 70 files, +480 −473. `git diff HEAD --binary -- . ':!docs/design/ui-quality-log.md'` SHA-256 `e776b74837161f11…` |
| Untracked | `apps/core/tests/test_public_ui_quality.py`, `tools/qa/phase9_qa.mjs`. Combined SHA-256 `6302ddfb64c54cd5…` |

### Defects found and fixed: Phase 9 areas

| # | Sev | Defect | Fix |
|---|---|---|---|
| D1 | Med | **Raw hex colours in the public shells** (`public/_base.html`, the "for" pages, help styles, docs layout, legal print style). | All colours now come from the palette. Code blocks are dark in both themes, so they get fixed tokens: `--code-bg`, `--code-fg` and `--code-line`. |
| D2 | Med | **The email unsubscribe page** (seen by the business's customers) was hard-coded light, with its own greys and reds. | It links `app.css` and uses tokens, so it follows the reader's dark mode. The ✓ is hidden from screen readers. It has a 16px side margin on phones. |
| D3 | Med | **The help search box took focus on load.** Keyboard and screen-reader users started mid-page, past the skip link and the header. | `autofocus` removed. The first Tab now reaches "Skip to content". |
| D4 | Low | **Grey text left in email (54 places), logs (45) and the Operator Console (128).** `text-[11px]` in email (3). | `ink-subtle` / `ink-muted` / `ink`, and `text-xs`. **Drift is 0 across the whole product.** The baseline was 1,252 at the start of the programme. |
| D5 | Low | **Docs:** the "On this page" heading was 3.02:1. Links in table cells were 17px tall. The public "Sign in" link was 21px tall. | `ink-subtle`, and 24px minimums. |
| D6 | Med | **Operator Console:** the settings page scrolled sideways at 390 (the logo file input wouldn't shrink in an implicit grid column). The checkbox and radio labels and several links were under 24px. "Deactivate" was 2.52:1 (`text-amber-600`). | `grid-cols-1`, `min-w-0`, `min-h-6` and `tone-amber-fg`. |
| D7 | Low | **Shared:** "Learn more" help links and links in any `.table-wrap` table were under 24px. | `components/help_link.html` has a 24px minimum. The table-link rule covers `:is(.table, .table-wrap table)`. |

### Defects found by the full regression pass

The per-phase checks ran at 390. The full crawl (`tools/qa/area_scan.mjs`) runs at 360, and new seed data showed states the earlier checks hadn't seen.

| # | Sev | Defect | Fix |
|---|---|---|---|
| R1 | **High** | **The inbox page scrolled as a whole on a 1280×860 screen** once the list was longer than its column. A row's `.sr-only` label is `position: absolute`. The scrolling list column wasn't positioned, so those labels escaped its scroll box and stretched the page (203px here). | `position: relative` on the list column. A test guards it. |
| R2 | Med | **Fields under 16px on phones in 13 templates and one script.** A `text-sm`/`text-xs` on a shared `.input`/`.textarea`/`.select` overrode its `text-base` phone size, so iOS zooms. This affected the email template editor, workflow editor, campaign compose and console forms, among others. | Made desktop-only (`sm:text-sm` / `sm:text-xs`) everywhere, including `static/js/sample-variables-editor.js`. Two tests guard templates and script-built fields. |
| R3 | Med | **Sideways scroll at 360:** the insight action row (Dismiss clipped), the Instagram accounts row (Disconnect clipped), and the help page's two "need a hand" cards. | The action rows wrap (`min-w-0 max-w-full` instead of `shrink-0`). The help row is `minmax(0,1fr)` and the cards wrap. |
| R4 | Low | **Unlabelled fields:** "Other ways customers pay" on Build, and the email assets search. | Labels added (the search is now `type="search"`). |
| R5 | Low | **The dashboard's chatbot panel** (shown once a bot exists) had a 20px bot name and a 16px "Analytics" link. | 24px minimums, and an accessible name for "Analytics". |
| R6 | Med | **Type check:** the Phase 7 chart change typed series as `list[float \| None]`, which mypy rejects for the sparkline's `list[float]`. You found this with `mypy .`. | `Sequence[float \| None]` (covariant). `mypy .` is clean. |

### Automated results

| Command | Result |
|---|---|
| `poetry run pytest -n 4` (full suite, final) | **5,170 passed, 25 skipped, 0 failed.** The first attempt at the default 12 workers crashed in collection with WinError 1450 (insufficient system resources, about 500 MB free). This was an environment problem, not a test failure; 4 workers ran cleanly. |
| `test_public_ui_quality.py` (new) | **11 pass:** drift is 0, code tokens, no help autofocus, the unsubscribe page uses tokens, small targets, console settings at phone width, no field under 16px (templates and scripts), action rows wrap, labels, the inbox list column contains its absolute children. |
| `ruff check .`, `ruff format --check .`, `mypy .` | Clean. 652 source files. |
| Drift | **0 total.** The baseline records zero everywhere. |

### Browser verification: full regression

Each harness ran against the dev server with fresh seed data. Every POST is answered by the harness, except Phase 6's pause and resume, which ran for real on QA data.

| Harness | Result |
|---|---|
| `landing_qa` (WhatsApp on) | 26/26 |
| `landing_qa` (WhatsApp off) | 26/26 |
| `inbox_qa` | **73/73** after R1 and two harness fixes |
| `publishing_qa` | 21/21 |
| `accounts_qa` | 12/12 |
| `channels_qa` | 17/17 |
| `billing_qa` | A 17/17, B 1/1 |
| `automation_qa` | 30/30 |
| `insights_qa` | 23/23 (the dashboard failed until R5) |
| `small_areas_qa` | 91/91 |
| `phase9_qa` public / user / operator | 23/23, 31/31, 47/47 |
| `area_scan` (crawl from the sidebar, 360 and 1280, both themes) | **119 pages, 0 with issues.** It was 9 before R2–R4. |

**Not re-run:** `sse_qa.mjs`, which needs the uvicorn server on :8002 (Entry 8).

**Phase 9 scope notes:**
- Public pages are scanned in light only. Their shell is always light (`data-theme="light"`), so a forced dark scan tests a state visitors never see.
- The Operator Console was checked with a separate local test operator, `ui-qa-operator@akilent-qa.invalid`. Its credentials are `QA_UI_OP_*` in the gitignored `.env`. It exists in the dev database only.

**Harness fixes:**
- `inbox_qa` "restored in time order" required position 0. The seeded thread has aged and now holds an older message, so the check now asserts time order and presence.
- The inbox "Not connected" check failed once on timing (one failed poll in 9s) and passed on the re-run.
- Headless Chrome left on a port by a crashed run is now stopped by its profile directory before each harness.

**Diagnosis note (R1):** I first thought a late-rendering suggestion card above the split was the cause and added a resize observer. Measuring showed the split fitted exactly and the overflow came from the escaped `.sr-only` labels, so I reverted that change. Only the `position: relative` fix remains.

Screenshots are in `docs/design/qa/phase9/` (local only).

### Backend gaps

None new. G5–G9 still need your authorization.

### Sign-offs

| Gate | Status |
|---|---|
| Implementation | **Done** |
| Verification | **Done**: full suite, every area harness and the 119-page crawl are green. **Open (yours):** real iPhone and Android checks (the iOS zoom fix especially), and a screen-reader pass. |
| Acceptance | **Pending your review** |

### Programme status

All ten phases (0–9) are implemented and verified. Under the release rule, three items are still open:
- your acceptance review of Phases 7–9 (Phase 8 committed as `ec74057`)
- the real-device and screen-reader passes listed in each entry
- the pilot-user validation of the flagged flows (inbox send and recover, scheduling a post, billing checkout, editing a workflow)

## Entry 17: Content Studio app feel, Phases 0 and 1 (2026-11-03)

Plan: "Content Studio: make it feel like an app" (2026-11-03). Approving it covered Phases 0 and 1 only. Working tree on `d96bf3f`, uncommitted.

**Phase 0: bugs from the production walkthrough**
- **Publish now on the calendar:** posts sent with Publish now (no schedule) now appear on the calendar, placed at the time they went out.
- **Today:** the month view now tints today's cell and fills its date, like the week view.
- **The plan form:**
  - It returns to the page it was opened from (`?next=`, same-site only).
  - "Plan an idea" from a calendar day now fills in the date.
  - The Delete button no longer submits the save form: the forms were nested, and are now separate.
- **The picture editor's crop area** has a fixed height, so it's no longer 100px tall.

**Phase 1: navigation**
- **No more full reloads for Media and the composer:**
  - `cropper.min.js`, `composer.js` and `media_library.js` load with the shell (`base.html`).
  - The composer's crop dialog wires itself through `akilent.onPage`, with cleanup.
  - `post_new` lost its `@full_page_load`.
- **Instant tabs:** `data-instant-tabs` (`static/js/app.js`).
  - The tab takes the active look when its request goes out.
  - It reverts on an error, abort, timeout, Back, or a newer navigation.
  - No preload on the tabs: a click during a hover preload was dropped by htmx.
- **Skeletons:** `templates/components/page_skeletons.html`. A slow page shows the tab's skeleton under the tabs after 150ms, and the old content comes back if the navigation fails.
- **Steady header:** the header above the tabs has one minimum height, so the tab row doesn't jump.
- **Submit-button guard:**
  - Moved from `<body hx-disabled-elt>` into `app.js`, for forms only.
  - It used to log a "returned no matches" console error on every boosted link click.
- **Overview query fix:** Overview no longer runs a query per post (`requires_approval`, `can_review_any`).
- **Analytics:** GA4 enhanced measurement counts history changes, and the session tracker records server-side per request, so in-place navigation is still counted. Overlays (Phase 2) will need an explicit page view.

**Verification**
- **Tests:** `test_studio_review_fixes.py` covers:
  - the calendar entries;
  - `next` handling, the date prefill and the unnested delete form;
  - boosted Media, editor, composer and calendar navigation with no `HX-Refresh`;
  - the shell scripts;
  - query counts per tab that don't grow with the number of posts.
- **Full suite:** 5,720 passed, 25 skipped. Ruff, format, mypy and `makemigrations --check` are clean.
- **New harness:** `tools/qa/studio_feel_qa.mjs`, 11/11 on the dev server (SQLite, dark theme):
  - the tab marks active 2–4ms after the click (`performance.mark`);
  - Media, Overview and the composer swap in place;
  - the crop dialog works after a swap;
  - skeletons appear only on loads over 150ms;
  - the tab reverts on a 500;
  - today is highlighted;
  - there are no console errors.
- **Existing harness:** `tools/qa/publishing_qa.mjs` still passes, including the unsaved-work guard and the crop dialog.
- **Server time:** every Content Studio tab renders in under 100ms locally with 40 posts.
  - The roughly 600ms time-to-first-byte reported on production wasn't reproduced locally, so it needs measuring on the server (network, the database, and the shell's per-request billing and entitlement queries, four of each).

**Open**
- Phases 2–5 each need their own go-ahead.
- A timing check on akilent.com after deploy.

## Entry 18: Content Studio app feel, Phase 2: drawers and modals (2026-11-04)

**What changed**
- **One overlay host** (`templates/components/overlay_host.html`, styles in `static/css/overlay.css`, `static/js/app.js` "Overlay"):
  - a right-hand drawer or a centred modal over the current page, chosen by the response's `data-overlay`;
  - it sits outside `<main hx-history-elt>`, and `htmx:beforeHistorySave` closes it, so a history snapshot never holds an open overlay.
- **History:**
  - opening pushes the link's URL; Back, Esc, the backdrop, Close and a finished save all close it, popping that entry;
  - a capture-phase `popstate` listener keeps htmx from re-fetching the page underneath;
  - a navigation started inside the overlay first puts the page's URL back with `replaceState`.
- **Server:** `overlay_or_page`, `overlay_done`, `partial_response` and `is_overlay` in `apps/core/htmx.py`:
  - a partial sends `Vary: HX-Request, HX-Target` and `Cache-Control: no-store`;
  - flash messages become an `X-Toast`;
  - `ShellMiddleware` turns an overlay request answered by a view that can't show itself there into a real page load, with the message kept for that load.
- **Forms inside the overlay** post back into it (`htmx:beforeProcessNode`):
  - a 400/422 re-renders in place;
  - a save answers `204` plus `HX-Trigger: overlay-close`;
  - any successful post refreshes the page underneath with one request. The composer opts out (`data-overlay-refresh="off"`).
- **J/K and the arrows in the drawer's title row** step through the `[data-row]` rows shown on screen, in screen order, with replaceState (no new history entries).
- **Analytics:**
  - GA4 counts the history change itself;
  - an `akilent:pageview` event fires for each overlay, so the harness can check it.
- **Converted:**
  - **Drawers:**
    - post (from Library, Overview, the calendar, Sales and Channels);
    - plan item edit;
    - posting slots;
    - caption templates and hashtags (from Media and from the composer's caption box).
  - **Modals:**
    - Add idea / Plan an idea (with the date filled in);
    - pillars;
    - themes;
    - CSV import (check and import in one modal);
    - the media editor (xl);
    - the "How posts are credited" help on Sales.
  - Every URL is still the full page.

**Verification**
- **Full suite:** 5,789 tests, 0 failures, 25 skipped. Ruff, format, mypy and `makemigrations --check` are clean.
- **Tests:** `apps/publishing/tests/test_studio_overlay.py`, 32 tests, covering:
  - fragment and headers;
  - the full page at the same URL;
  - a drawer action that redirects back re-renders with a toast;
  - Delete lands on a real page load with its message;
  - saving 204 / refusing 422;
  - each side page in its shape;
  - the openers on each list.
- **New harness:** `tools/qa/studio_overlay_qa.mjs` on the dev server:
  - the drawer opens in place;
  - focus is inside and the page is inert;
  - the pageview is recorded;
  - J steps without new history;
  - Back closes it with no re-fetch and focus on the row;
  - Esc closes it;
  - the URL is the full page;
  - the drawer is full width at 390px;
  - Add idea opens with the day and focus on Topic;
  - a refused save stays with its reason;
  - a save closes it and the calendar shows the idea without a reload;
  - delete in the drawer;
  - the side pages open and close;
  - the composer keeps typed text under its drawer;
  - no console errors.

**Open**
- The help modal on Sales can't be exercised locally: the article belongs to the WhatsApp channel, which is off on the dev server.
- Phases 3–5 each need their own go-ahead.

### Entry 18a: fixes from the review of Phases 0–2 (2026-11-04)
- **The composer was unstyled when reached in place.**
  - `{% block extra_head %}` is now rendered in a shell swap too. It travels into `<main>` with the page, so a page's `<style>`/stylesheet arrives with it and leaves with it. This applies to the composer, the media editor and the email block editor.
  - `<main>`'s classes (`block main_class`) come with the swap (`<template data-main-class>`) and with Back (read from the restored page).
  - Harness: `composer.styled-after-swap` checks the sticky action bar and preview after a boosted visit, and `main.classes-restored-after-composer`.
- **The tab row jumped (136/146/158px).**
  - Every Content Studio header is `.cs-head`, 2.75rem high from sm up (`static/css/studio.css`), with no subtitle.
  - The Needs-check explanation moved under the tabs.
  - The inline `<style>` the `:has(+ nav)` rule matched is gone.
  - Harness: `tabs.same-height-on-every-tab` (148px on all 7 tabs).
- **The media editor showed a broken image without CORS.** It now shows the picture without `crossorigin` as a preview only: a fresh `<img>` (the same element kept its failed load), the message says so, and Save is disabled. It opens almost full screen (`size="full"`).
  - Harness: `editor.no-cors-shows-preview`, against an https test host answered with no CORS headers.
  - **Still needed on AWS:** S3 CORS for `https://akilent.com` (GET/HEAD), plus a CloudFront policy that forwards `Origin`.
- **Unhandled `AbortError: Transition was skipped`:** `ready` and `updateCallbackDone` are caught.
- **Plan form errors** show under the field (`aria-invalid`, `aria-describedby`), keep what was typed, and answer 422 on the page as well as in the modal. There's no toast any more (it covered Close). In the modal the buttons are pinned to its bottom edge (`.ak-overlay-actions`).
- **Smaller fixes:**
  - Overview's action button on a post opens the drawer.
  - Today has `aria-current="date"` (week, month, list).
  - Full pages also `Vary: HX-Target`.
- **Kept as they were, with the reasons from Phase 1:**
  - no `preload` on the tabs (a click during a hover preload was dropped);
  - no `cs-tab` view-transition underline (the tab is already active before the transition, so there is nothing to slide).
- **Verification:**
  - full suite 5,801 tests, 0 failures, 25 skipped; ruff, format and mypy clean;
  - `studio_feel_qa.mjs` 14/14 (cache disabled);
  - `studio_overlay_qa.mjs` 30/30.

### Entry 18b: second review of Phases 0–2 (2026-11-05)
- **Media editor:**
  - all its controls sit in one fieldset that stays disabled until Cropper runs, and for good in preview-only mode;
  - the "preview only" message sits under the picture;
  - overlays and dialogs move focus without scrolling (`focusInitial(root, preventScroll)`), so the editor opens at its top.
  - Harness: `editor.preview-only-controls-disabled` (11 of 11 controls disabled), `editor.opens-at-top`, `side.media-editor-controls-ready-and-at-top`.
- **Plan file:** the two dropped Phase 1 items (tab `preload`, the `cs-tab` underline) are marked dropped, with their reasons.
- **Ideas without channels** become a post on the connected channels (else every one switched on). They used to fail with "At least one platform must be selected"; found in a local end-to-end run.
- **Speed, measured locally on the QA workspace** (SQLite):

  | Page | Time | Queries |
  |---|---|---|
  | Composer | 74ms | 37 |
  | Channels | 54ms | 36 |
  | AI | 112ms | 65 |
  | Library | 81ms | 33 |
  | Overview | 136ms | 45 |
  | Calendar | 299ms | 36 |
  | Sales | 122ms | 68 |

  - No page queries per row, and no page calls Meta while rendering.
  - The shell repeats its entitlement reads 4–5 times per request (`billing.api._state` is uncached): subscription ×5, membership/plan features/overrides/module subscriptions ×4, site settings ×3, about 17 queries on every page.
  - **Proposed** (not built): a per-request memo, cleared on any save of those models.
- **Verification:**
  - full suite 5,802 tests, 0 failures, 25 skipped;
  - `studio_overlay_qa` 33/33, `studio_feel_qa` 14/14, `publishing_qa` 21/21.

### Entry 18c: third review of Phases 0–2 (2026-11-05)
- **Existing pictures still opened as preview only.** CloudFront was already fixed (SimpleCORS adds the header on cache hits too, checked). The stale copy was the browser's: the grid's no-CORS load is cached for a year without the header, and Chrome handed it to the editor's CORS load. The editor now asks for `?cors=1`, a separate entry in the browser's cache that CloudFront ignores (its cache policy drops query strings). A URL that already has a query is left alone.
- **Focus fell to `<body>` and the editor opened scrolled.**
  - The overlay focused its content before Alpine had set it up, so focus landed on "Square 1:1" just before Alpine disabled it. Focus now moves a frame later, then the scroll is reset.
  - If a focused control in the overlay is ever disabled, focus moves to Close.
  - Harness: `editor.opens-at-top` (max scrollTop 0 over 2.5s with a slow, failing picture) and `editor.focus-stays-in-editor` (ends on Close).
- **The follow-ups badge was fetched twice per navigation.** The nav is re-rendered out of band on each in-place navigation, and both new badges (desktop and mobile) fetched on `load`. Each badge now has an id and `hx-preserve`. Harness: `nav.badge-not-refetched-per-page` (0 fetches over a 7-tab loop).
- **The unexplained full reload.** The only app-side causes are the "never strand a navigation" fallbacks (send error or timeout on a boosted GET). They now record why in `sessionStorage["akilent:hard-nav"]` and in the console. Harness: `tabs.loop-never-reloads` over the 7-tab loop with the cache disabled.
- **Verification:**
  - full suite 5,804 tests, 0 failures, 25 skipped;
  - `studio_feel_qa` 16/16, `studio_overlay_qa` 34/34, `publishing_qa` 21/21.

### Entry 18d: fourth review of Phases 0–2 (2026-11-05)
- **`ReferenceError: previewOnly is not defined`, and the editor opening scrolled 310px, on akilent.com.** Neither happens with the current code (closing by Esc, Close and Back logs nothing; the editor opens at scrollTop 0). Production was running old JavaScript: static files kept fixed names (`static/js/app.js`) under `max-age=31536000`, so browsers and CloudFront kept last deploy's `media_library.js` (no `previewOnly`) and `app.js` (focus without `preventScroll`) against the new templates. `StaticToS3Storage` is now `S3ManifestStaticStorage` (content-hashed names, not strict), so every deploy's files are new URLs. A trial collectstatic hashed all 209 files in 9s. Tests: `apps/core/tests/test_static_hashing.py`.
- **The editor in a short window.** In the modal from `lg` up, the controls sit beside the picture and the stage takes the modal's height (`calc(94dvh - 9.5rem)`). Below `lg` the stage is `min(55dvh, 40rem)`. At 1280×631 the whole picture and the Save button are on screen (body content 531px in 520px, was 836px). Cropper's wheel zoom is off, so the wheel scrolls the modal. Harness: `side.media-editor-fits-short-window`.
- **The chosen shape looks chosen**: accent text, a tinted background and a 1px accent ring (`static/css/studio.css`). Harness: `side.media-editor-shape-shown-selected`.
- **Preload and the tab underline** are still deliberately dropped (Entry 18c, the plan file). **Speed** is unchanged; the billing per-request memo awaits a decision.
- **Verification:** full suite, 0 failures; ruff clean; `studio_overlay_qa` 36/36, `studio_feel_qa` 16/16.

### Entry 19: Phases 4 and 5, with the Media redesign (2026-11-05)
- **Media is a gallery.**
  - The policy box is gone. A slim summary ("8 files · 100.2 KB") sits under the tabs, with an ⓘ popover holding the removal rules, and Upload is the primary button in the header (Templates & hashtags as an icon button).
  - One sticky, blurred filter row (static on phones): a pill search over names and tags, segmented All · Images · Videos · Unused · Archived (`?kind=` and `?archived=1` still work), a tag select, sort (Newest · Most used · Name · Size) and Grid / List. It swaps `#media-results` in place and keeps the URL in step; focus stays in the search box.
  - Cards are the pictures: a 4:5 frame, the picture contained in it over a blurred copy of itself, so a tall screenshot shows whole. On hover or focus (always on touch): a select box, a type badge (PNG, Video 0:42, Reel cover, Edited), and Edit, Use in a new post and ⋯ (Rename and tags, Download, Remove with the outcome spelled out). Under it, a clean name ("WhatsApp image · 10 Sep") and a usage chip ("2 posts", or "Unused" in teal).
  - Grouped by date with sticky labels when sorted by newest. Copies are flagged ("Looks like 01_home.png") by content hash, or by the same size and name less " (1)".
  - Clicking a picture opens its details as a drawer: a large preview (focused, so ← → and J/K step through the grid), facts, name and tags, "Used in" post thumbnails that open the post in the same drawer (Back returns), files made from it, and Edit · Use in new post · Download · Remove. Remove is disabled with the reason when a draft or scheduled post needs the file.
  - Selection: tick (Shift for a range), then a bar up from the bottom: N selected · Add a tag · Tag · Remove (or Put back) · ✕. Esc clears it. `media_library.bulk` applies the one removal rule per file and the message says how each ended.
  - Upload from the button or by dropping files anywhere ("Drop to upload"); each file is a card with a progress ring and a local preview until the grid refreshes in place (no page reload).
  - "Use in post" opens the composer with the file (`?media=<id>`, this business's live files only).
- **Phase 4 controls** (`static/css/controls.css`): a segmented control for the composer's six formats (three to a row below `sm`), Media's show and view, the calendar's Week/Month/List, and the Sales and Analytics periods. Switches (`role="switch"`) for Require review and the weekly email (they save when flipped; Save stays for no-JS), Bio's own colours, and Repeat on/off. The Library's two date fields are one popover with presets (7, 30, 90 days, this month, any time); a bottom sheet on phones.
- **Phase 5**:
  - The month calendar: a "+" on each day from today (on hover or focus, always on touch) opens Add idea for that day; planned ideas drag to another day through `plan_move` (not to a past day, not once published), behind `cs_scheduling`.
  - Shortcuts: N (new post, Content Studio), / (the page's search), J/K (rows; Enter opens), ? (lists them in the command palette). Never while typing or with a dialog open.
  - Visual finish: Content Studio's 36 underlined links use one quiet link style (`.cs-link`), and the Library table's rows a light divider and soft hover (`.cs-row`).
  - Drawers: a link inside an open drawer loads in its place as a history entry; Close goes back past all of them.
- **Not done in this pass:** Phase 3 (undo toasts, live filters on Library/Calendar/Sales) still needs its go-ahead, so archiving has no Undo toast yet (Put back is in Archived). Empty states and radii were swept on Media only.
- **Verification:** full suite 0 failures (25 skipped); ruff clean; `studio_gallery_qa` 16/16 (new), `studio_overlay_qa` 36/36, `studio_feel_qa` 16/16, `publishing_qa` 21/21; no sideways scroll at 360px on Media, the composer, Calendar, Sales and Analytics.

### Entry 20: Phase 3, live filters, edits in place, Undo (2026-11-05)
- **Live filters** (`static/js/app.js`, `form[data-live-filter="#results"]`): search applies 300ms after typing stops, everything else on change. Only the results region is replaced, so focus stays in the search box. The URL follows (replaceState) with empty fields left out, and a virtual pageview is sent. The hidden submit and the chips' links keep it working without JavaScript. Used on Library, Media, Calendar and Sales.
- **Chips** (`publishing/_filter_chips.html`, `apps/publishing/live_filters.py`): each filter in use is a removable chip linking to the page without it ("Clear all" when there are several); with JavaScript the chip clears its field and applies in place.
- **Library**: one **Show** control replaces Status and "Where it stands": the worklist buckets, then the statuses no bucket covers (published, cancelled). The Filter button is gone. `?bucket=` (every `bucket_url()` key, tested) and `?status=` links still work. Select all is an Alpine handler, so it survives the swap.
- **Undo is the server's**:
  - `hx_toast(…, undo=path)` adds a third part to `X-Toast`, and `accounts.utils.flash(…, undo=path)` puts `undo:/path` in a flash message's tags. Toasts show **Undo** for 30 seconds, which POSTs once to that path, then refreshes the page in place (`akilent.refreshPage`).
  - `apps.core.htmx.action_done` finishes a POST: 204 with the toast from the background, else a flash and redirect. `overlay_done` takes `undo` too.
  - Fixed on the way: a toast's POST action sent an empty CSRF token, because the cookie is HttpOnly. `CSRF()` now falls back to `<body hx-headers>`.
- **Media cards remove in place**: the card's Remove posts with htmx. Archived: the card goes, with Undo (`media-restore`). Deleted: the card goes, with no Undo (the confirm said so). Blocked: the card stays and the toast says why. Put back in Archived works the same way. The details drawer's Remove also offers Undo when it archives.
- **Undoing a cancel** (`services.undo_cancel`, `posts/undo-cancel/?ids=`): if every cancelled channel's time is still more than 2 minutes ahead, the post goes back to scheduled at those times, through the readiness check and approval (it goes to review if it needs an approval it no longer has, or stays a draft with the reason if a check stops it). Otherwise it comes back as a draft with no schedule, and the toast says so. Offered on a single cancel and on the Library's bulk cancel.
- **Confirms spell out the outcome**: Delete drafts (deleted for good; other ticked posts untouched), Cancel scheduled (unpublished channels won't go out; Undo follows), Delete post, and Channels → Disconnect (posting and new Messenger messages and comments stop, conversations kept, scheduled Facebook posts wait for a reconnect).
- **Verification:** full suite 5,831 passed, 0 failures, 25 skipped; ruff, `ruff format --check` and mypy clean. `studio_gallery_qa` 20/20 (new: Library live search keeps focus, a chip clears its filter, Show applies on change, archive in place, then Undo brings the card back); `studio_overlay_qa` 36/36, `studio_feel_qa` 16/16, `publishing_qa` 21/21.

### Entry 21: the remaining plan items, and the fifth review (2026-11-05)
- **Remaining plan items:**
  - Media's whole-library sort and Unused filter join the query-count test (`test_tab_queries_dont_grow_with_the_number_of_posts`, which already covered seven tabs).
  - Nine section empty states use `components/empty_state.html`, which gains `action_overlay` and `compact`: Overview (twice), Planning, Reconciliation, Recurring, Themes, Pillars, Slots and Bio.
  - Radii in Content Studio's inline styles and `studio.css` use the tokens (the in-between values snapped by 2–4px).
  - Before each upload the page asks the server (`media-similar`: the same SHA-256, hashed in the browser up to 25 MB, or the same size and name less " (1)") and the card asks "Keep both / Skip".
  - Area scan of Content Studio: 12 pages at 360 and 1280px in light and dark, 0 issues.
- **Review fixes:**
  1. **The date popover closes:** after a preset (`$root`, not the button's `$el`), on Esc (focus back on the trigger) and on an outside click, for every `details.pop`. Closed panels get `display: none` whatever else styles them. It opens leftward from the right of the bar, with no sideways scroll. The trigger and chip both read "Last 7 days" or "This month" (`live_filters.date_range_label`).
  2. **CloudFront:** not fixable in the app. A random query string is a cache hit, and responses now carry S3's own CORS headers, not the `*` the SimpleCORS response policy added before. A copy cached from a request without `Origin` has no CORS header for anyone. Fix in AWS (see the report).
  3. **The Media toolbar** sticks under the 4rem top bar (`top: calc(4rem + var(--safe-top))`).
  4. **Date labels** are their own non-sticky row and no longer cover the first card.
  5. **No focus ring** on the heading focused after a page change (`[tabindex="-1"]:focus`).
  - **Should fix:**
    - each card says what Remove will do before it's pressed: Delete, Archive (with how many published posts use the file), or "can't be removed yet" (`media_library.needed_ids`, one query a page);
    - cancelling a confirm returns focus to the button or its menu's trigger;
    - the selection bar has Archive, with Undo (`media-restore-many`);
    - the file drawer always shows dimensions (read from the picture if the server doesn't know them) and file size;
    - the Library's filters use Media's toolbar;
    - Week view drags posts and ideas through the same Move checks.
  - **Unchanged:** preload and the tab underline stay dropped (reasons in Entry 18c). Speed is the billing memo, which awaits a decision.
- **Verification:** full suite 5,841 passed, 0 failures, 25 skipped; ruff, format and mypy clean. `studio_gallery_qa` 26/26, `studio_overlay_qa` 36/36, `studio_feel_qa` 16/16, `publishing_qa` 21/21; `area_scan /publishing` 0 issues.

### Entry 22: sixth review, server speed, Sales, and the studio's look (2026-11-05)
- **Sixth review fixes:**
  - The Library's date trigger resets when its chip is removed: the results region carries the dates it was filtered by (`#library-dates`), and the popover syncs from it after every swap.
  - A file a draft uses keeps **Remove** in its ⋯ menu, disabled, with the reason under it.
- **Server speed** (production measured 430–1,030 ms to first byte; every query is a round trip):
  - `apps.core.request_context.remembered`: small repeated reads happen once per page. `RequestMemoMiddleware` opens the memo for GET and HEAD only; POSTs, Celery and shells read fresh. Never the shared cache.
  - Used for entitlement (`billing.api._state`, read 4–10 times per page), `SiteSettings.load` (2–3) and opening hours (2–4). Saves of the models they come from call `forget` (signals, plus the billing writes that skip signals). Onboarding's "awaiting first payment" reads the same billing state.
  - The suspended-member check asks the usual case first (one query instead of two). Two "next action" suggestions drop an `exists()` their link query already answers.
  - **Sales had an N+1:** `bio.credit_basis` re-read the bio page for every credited chat when a workspace has none (`page=None` meant "not given"). Now a sentinel; 75 → 34 queries on the local demo.
  - Queries per tab (tests, 12 posts): Overview 38 → 25, Library 27 → 15, Calendar 29 → 17, Channels 30 → 17, Sales 39 → 21, AI 40 → 22, Analytics 53 → 20.
  - `Server-Timing: db;dur=…;desc="N q", app;dur=…` on every response to a signed-in person (`ServerTimingMiddleware`, outermost).
  - `CONN_HEALTH_CHECKS = True`. `CONN_MAX_AGE` was already 60 with sync gunicorn workers, so connections are reused. Not checked from here: whether the database is in the app servers' region and zone (the host is a deploy secret). The new header will show it.
  - Locked in by `test_studio_speed.py`: a query budget per tab, entitlement and settings read once per page, a change during a page seen at once, the header's format. Plus a Sales test that its queries don't grow with chats.
- **Sales:**
  - Four tiles: Chats, Leads, Orders, and Paid revenue per currency. Each has a trend line and the change against the period before. Confirmed credits only, the same rule that ranks posts.
  - The report reads both periods in the same queries (a date range twice as long).
  - Each post row has a funnel (chats → leads → orders → paid, each a share of the widest step), a larger picture and its revenue on the right.
  - Full width: the posts on the left, the revenue breakdown on the right. The rules sit behind an ⓘ, and empty states have an icon and a next step.
  - Marks use one hue (the accent; its lighter step for tracks). Up and down colours only repeat the arrow and the words.
- **Look and feel:**
  - The title is the tab's name, under a "Content Studio" crumb (`_cs_title.html`, `studio_tab` tag); Overview keeps "Content Studio".
  - A four-size type scale with tighter headings.
  - One card treatment: hairline, `--radius-surface`, no shadow; only clickable cards lift.
  - Setup progress is "Setup N/M" with a ring in the top bar. It opens the checklist under the bar (Esc or a click outside closes it) and goes once setup is done. The corner bubble is gone.
  - Yellow stays for the page's one main action. In-card saves use `.btn-tonal` (navy), and so do empty-state actions in the studio.
  - Things swapped in place (chips, cards, rows, tiles) fade and grow in over 150 ms. A media card being removed fades out first (`swap:150ms`). Page changes are left to their own transition. Toasts scale in. Reduced motion turns all of it off.
- **Not built:** tab preload and the sliding underline (Entry 18c), and the rest of the studio redesign proposal (Overview data, Planning board, Bio preview, Channel health, copy trimming), which wasn't part of this go-ahead.
- **Verification:** full suite 0 failures; ruff, format, mypy and `makemigrations --check` clean. `studio_gallery_qa` 29/29 (new: date chip resets the trigger, blocked Remove says why, setup opens under the bar), `studio_overlay_qa` 36/36, `studio_feel_qa` 16/16, `publishing_qa` 21/21; `area_scan /publishing` 14 pages, 0 issues.

### Entry 23: seventh review (2026-11-05)
- **Overview title:** "Overview" under the "Content Studio" crumb, like the other tabs (`studio_tab`).
- **Setup stuck at 4/5:** a paused channel's step stays on the checklist, marked "Not counted", but is left out of the count. Setup is still not complete while a channel is paused (unchanged rule). When only the paused step is left, the top bar shows "Setup paused" with a pause icon instead of a ring short of full, and the checklist says everything you can do today is done.
- **Narrow pages:** Channels, AI and Bio page use the full width with `.cs-cols` (two columns from lg, one below).
  - Channels: the accounts side by side, then review and recent failures.
  - AI: brand voice beside post ideas.
  - Bio page: the link and posts on the left, the settings on the right (sticky). The "Set up your bio page" card keeps a readable width.
- **Sales:** a post with no confirmed credit at all moves out of "Too early to judge" into a folded "Only unconfirmed credits (N)" group. It's kept, because such a post can still show real activity (e.g. 6 bio-link chats and a paid order), but it isn't read as a sample.
- **Bio picture field** (found by the scan once a local bio page existed): labelled, 16px on phones, and fits its column at 360px.
- **Analytics tab:** behind the `cs_analytics` release gate. Production's `RELEASED_FEATURES` doesn't include it, so it's hidden there by design; local runs had it on.
- **Signed out mid-tour:** sessions are in the database (`SESSION_ENGINE` default) and the deploy doesn't clear them, so a redeploy doesn't sign anyone out. The limit is `SESSION_COOKIE_AGE` = 8 hours from sign-in, not from the last request (`SESSION_SAVE_EVERY_REQUEST` is off), plus `SESSION_EXPIRE_AT_BROWSER_CLOSE`. A long review session hits the 8 hours. Left as is: a policy decision.
- **Verification:** ruff, format and mypy clean. `studio_gallery_qa` 29/29, `studio_overlay_qa` 36/36, `studio_feel_qa` 16/16, `publishing_qa` 21/21; `area_scan /publishing` 14 pages, 0 issues. Full suite: one failure, `test_the_next_app_code_works_after_an_old_sign_in` (time-based authenticator codes), which passes on its own (4/4 in its file).

### Entry 24: sessions read from Redis, kept in PostgreSQL, extended while active (2026-11-05)
- **Store:** `SESSION_ENGINE = "apps.core.sessions"`, Django's `cached_db` with every cache call guarded. Stock `cached_db` still raises with Redis down on the write after a database fallback, on `exists` (new keys at sign-in) and on `delete` (sign-out). PostgreSQL stays the source of truth; existing database sessions carry over (same table).
- **Cache alias `sessions`:** the same Redis instance and database as the cache today, with 0.5 s connect/read timeouts so a hung Redis can't hang sign-in. `SESSION_REDIS_URL` moves sessions to a dedicated Redis later with no code change (Stage 2 of the user's session plan).
- **Extended while active:** `SessionRefreshMiddleware` extends a signed-in session at most every 5 minutes, so the 8 hours count from the last activity. The effective limit is 7 h 55 m – 8 h after the last activity. `SESSION_SAVE_EVERY_REQUEST` stays off. Activity is a page, a page change, an overlay opening or any POST; htmx polling and the live-updates stream don't count, so an idle open tab still times out.
- **Found and fixed:** `reset_user_mfa` ended sessions by deleting database rows, which would have left cached copies signed in. It now deletes through the session store (database and Redis).
- **Known edge (as stock Django):** a sign-out while Redis is unreachable can't delete the cached copy. If Redis returns with that data, the old key works until the copy expires (≤ 8 h); the browser's cookie is already gone. **Closed in Entry 25.**
- **Tests** (`apps/core/tests/test_sessions.py`): no writes within 5 minutes; one write and a longer expiry after 5; polling and the live-updates stream don't extend; active people stay signed in for 10 hours; 8 h 01 m idle signs out; a cleared cache keeps people signed in; with the cache failing on every call people stay signed in, a new sign-in works and sign-out works (stock `cached_db` fails this test); sign-out removes the session from both stores; database sessions from before the switch carry over; the settings hold.
- **Against real Redis** (local Docker, `redis:7.4-alpine`): restart, `FLUSHDB`, stopped (sign-in and sign-out still work), hung (`docker pause`, answered in 1.1 s), and container recreation with an empty volume. Signed in throughout; sign-out removes the session from both stores.
- **Verification:** full suite 0 failures; ruff, format, mypy and `makemigrations --check` clean.
- **Before production:** run the same checks on staging (the scratch script's steps: restart, flush, stop, pause, recreate).

### Entry 25: durable session revocation and an absolute lifetime (2026-11-05)
- **The edge from Entry 24:** a cookie copied before a sign-out that Redis didn't see must stay dead when Redis comes back with its copy.
- **Session generation** (`accounts.SessionSecurity`, migration `0026`): one number per person in PostgreSQL. Each session records the generation it signed in under (`user_logged_in`, read fresh from the database at sign-in). `SessionRefreshMiddleware` signs out a session whose number doesn't match.
  - No extra query per request: `EmailBackend.get_user` joins the row into the user query Django already makes. `test_checking_the_generation_adds_no_query`, and the per-tab query budgets, guard it.
  - Sessions signed in through `ModelBackend` (username sign-in, e.g. the admin) read it with one more query.
- **It goes up** when a sign-out (`flush`) or key rotation (`cycle_key`) can't confirm the Redis delete, and on every MFA reset. Password changes were already covered: Django checks each session against the password hash.
- **Trade-off:** a sign-out without Redis ends the person's other sessions too. That's the safe direction, and only then; an ordinary sign-out ends one session (tested).
- **Existing sessions:** they have no recorded generation and no row, both 0, so nobody is signed out by the deploy.
- **Absolute lifetime:** `SESSION_ABSOLUTE_AGE` (env, default 7 days) from sign-in, however active. Sessions from before the deploy start counting at their first request after it. The 8-hour inactivity limit is unchanged.
- **Tests:**
  - a copied cookie after a sign-out during an outage, with Redis back holding the old copy, is refused;
  - an ordinary sign-out leaves other devices signed in;
  - MFA reset and password change during an outage end old sessions;
  - a new sign-in after the outage works;
  - 7 days, active every 6 hours, then signed out;
  - a pre-deploy session stays signed in and gets its week from then;
  - no extra query.
  - The copied-cookie and MFA tests fail with the generation check turned off.
- **Real Redis, separate processes** (`tools/qa/session_redis_check.py`, for staging too):
  - sign-out in one worker while Redis is hung (it keeps its copy), then the copied cookie in another once Redis is back: refused, with Redis still holding the old copy;
  - plus restart, flush, hung (1.1 s), stopped and recreated. All pass.
- **Remaining limits:**
  - **Generation granularity:** the generation is per person, so revoking one session durably when Redis is down means revoking them all.
  - **Absolute lifetime:** checked from a value inside the session, so it relies on the session data not being altered. It's signed, and only the server writes it.
  - **Monitoring isn't built.** The log lines to alert on:
    - "Session cache unavailable on …" (fallbacks);
    - Django's "Error saving to cache";
    - "Sign-out without Redis: ended every session" (forced revocations).
- **Verification:** full suite 0 failures; ruff, format, mypy and `makemigrations --check` clean; `tools/qa/session_redis_check.py` all steps pass against local Docker Redis.

### Entry 26: Inbox Phases 0 and 1: speed, then each conversation's state (2026-11-06)
Plan: the Inbox plan of 2026-11-06 (the Content Studio plan now lives in `docs/plans/content-studio-app-feel.md`).

- **Phase 0, speed.** Query counts with 30 conversations and a 60-message thread (`apps/conversations/tests/test_inbox_speed.py`, which also checks they don't grow with twice the conversations and five times the thread):

  | Request | Before | After | Plan target |
  |---|---|---|---|
  | list `/inbox/` | 33 | 17 | ≤ 25 |
  | pane | 38 | 18 | ≤ 20 |
  | panel (new) | — | 21 | ≤ 20 |
  | full page `/inbox/conv_…/` | 62 | 28 | ≤ 40 |
  | feed | 29 | 13 | ≤ 15 |
  | badge | 4 | 3 | ≤ 5 |

  - **The panel is one over:** its last query is the recommended action; gating it would hide recommendations that exist without the Insights feature.
  - **List:** the assignee came with a query per row (now joined); four tab counts are one query, and the two waiting counts one more (`state.waiting_counts`); the pager no longer runs the row previews for every conversation; All, Assigned to me and Closed page by the stored `last_message_at` (falling back to `created_at`) and work out message facts for the page's rows only.
  - **Pane:** opens on the latest 50 messages, or from the first unread one, up to 200 (`_recent_thread`); older ones through `messages_feed?before=` ("Show earlier messages", keeping the reader's place). The details panel moved to `_conversation_panel.html`, loaded by `conversations:panel` once it shows (at once as a column, when opened as a drawer); its actions answer with the panel, the composer's Next steps out of band, and `inbox-refresh`. Saved replies load when their menu opens. The conversation state is one query (`state.state_of`) instead of three.
  - **Site-wide:** `is_suspended_member` no longer queries when a workspace resolves; `workspace_context` is lazy (nothing read it); `components/icon.html` is a dictionary lookup (`apps/core/icons.py`) instead of a 67-branch chain (template work on the list −20%).
  - **Badges:** poll every 300 s instead of 120 s, only while on screen in a visible tab (the nav is on the page twice).
  - **Server time** (local SQLite, 200 conversations, a 500-message thread): list ~130–160 ms, pane ~65–105 ms, panel ~55–90 ms, full page ~190 ms. At 2,000 conversations and 200,000 messages the unfiltered list went from 3.0 s to 0.76 s.
  - **Load, two workers:** a simulation (threads with a two-request limit, serve times stretched ×6 to match prod) puts the workers 86–91 % busy with three open Inbox tabs and one person opening conversations, so requests queue: prod's 1.1 s badge is waiting for a worker, not slow code. More workers (or threads) is the fix and an infrastructure step; the simulation is for comparison, not absolute numbers.
- **Phase 1, state.**
  - A row whose latest message is ours and didn't go says "Not sent:" with the reason on hover; the poll's hash covers it, so a send that fails later reaches the list.
  - Failure text is worked out from the stored error code when shown (`views._failure`), so old messages lose "(error 131042)"; account-level codes (131042, 131037, 190) carry a fix link (`friendly_errors.account_level_fix`). Codes stored as "#131042" or "131042/…" read the same.
  - Closed is the stored status everywhere (row, header); an open conversation gone quiet for a day with us speaking last is "Quiet". The ⋯ menu offers Reopen on a closed conversation.
  - Open views show open conversations only (All, Assigned to me now too); every count shows, 0 included; the channel filter lists channels with conversations (plus one in force).
  - Search above the list: name, phone, email or message text, within the view and channel, as you type; the poll sends it too; kept in the URL on the list's own address; `/` focuses it.
- **Not done, reported:** search at 200,000 messages takes 0.8–1.2 s on local SQLite (over the 300 ms target); the fix would be a `pg_trgm` index, a migration, not added without approval.
- **Verification:** full suite 0 failures; ruff, format, mypy and `makemigrations --check` clean; `tools/qa/inbox_qa.mjs` 79/79 (now loading the lazy panel, plus Show earlier and live search); `area_scan /inbox` 26 pages, 0 issues.

### Entry 27: Inbox Phase 2: room for messages, one window-closed state, the details beside the chat (2026-11-06)
Plan: the Inbox plan of 2026-11-06, Phase 2.

- **Room for messages.** Measured by `tools/qa/inbox_qa.mjs` (`QA_LAYOUT=1`, which now passes or fails each size):

  | At | Thread before | Thread after |
  |---|---|---|
  | 1280×631 | 267 px | 427 px (target ≥ 420) |
  | 1024×631 | 223 px | 421 px |
  | 1280×800 | 436 px | 596 px |

  - One 44 px bar heads the list: "Inbox", the view menu, the channel filter, Follow-ups, Set-up (`inbox.html`, `_inbox_views.html`). It replaces the title row and the toolbar. At lg it sits over the list column only, so the conversation starts at the top. The feed sends `_inbox_views.html` out of band inside its digest, so counts stay live; the poll skips while focus is in the bar.
  - The six view tabs are gone: one menu at every width (it already replaced them beside an open conversation).
  - The suggested next step moved into the empty pane. Below lg the list has the screen, so it's on the dashboard there.
  - The inbox's main area: less padding at lg (`lg:py-2.5`) and up to 110rem wide.
  - The conversation header is one row: the name with its status beside it. The status gives way first, and its badges drop out whole (wrapped onto a hidden line), never cut mid-word. On a phone-narrow pane the name sits over the status inside the same row. Badges are short ("Missed", "Waiting · 2h"), with the sentence in their title.
- **Layout fixes.** No sideways scroll under the list (was 20 px; the old toolbar's negative margin). Nothing moves when a conversation opens: the bar is the same either way (the list used to move 140 px as the suggestion card hid). Both checked at five sizes.
- **One window-closed state** (`_conversation_pane.html`, `_template_form.html`):
  - a one-line note with a "Why?" tip (`help_tip.html` takes `tip_label`), in place of the `<details>` notice and the "Send a template · Required" line;
  - WhatsApp: the template picker in the reply box's place; the dialog (window open) uses the same form;
  - the last send failed for an account reason (`failureFix`): that problem and its fix link first, with "Try a template anyway" (chat.js `accountProblem`);
  - no approved templates: says a template must be approved by Meta, with a link to create one;
  - Instagram and Messenger: the reply box switched off ("Reply once they write again"); attach, mic, saved replies and "Edit & resend" hidden until the window opens; Messenger after a private comment reply keeps its own explanation.
- **The stray blue goes.** The chat's accent is the theme's; Send and the mic are navy (`--color-brand-600`), recording is the theme's coral.
- **The details beside the chat.** A docked column where the pane keeps the chat ≥ 420 px beside a column ≥ 280 px (`@container cv (min-width: 44.75rem)`). That is a 1440 px window with the sidebar open, or 1280 with it collapsed. The header's ⓘ hides and shows it, and its edge drags (or arrow keys) between 280 and 440 px; both are remembered. CSS `min()` keeps the chat ≥ 420 px whatever width is chosen. Narrower, it's a drawer that slides in, under akDialog (focus in and back, Esc, inert page), with a `ResizeObserver` in place of the window resize listener.
  - **Differs from the plan:** the plan said docked at ≥ 1280 px. At 1280 with the sidebar open, the pane is 580 px, and docking would leave the chat at 268 px.
  - **Differs from the plan:** the plan said to use the shared `{% drawer %}`. It teleports its content to `<body>`, which can't also be a column, so the panel keeps one element with the same dialog contract.
- **Setup over the composer:** the checklist opens from the header and ends above the reply box at 1280 and 390 (`QA_OB=1`, composer covered at 0 of 3 points, closed or open). The tour is a one-time dialog.
- **Tailwind:** the committed CSS was built with 4.3.3; building with 4.3.3 reproduces it exactly, so there is no drift.
- **Speed** (local data, queries / median server time): list 22 / 59 ms, feed 13 / 41 ms, pane 17 / 42 ms, panel 21 / 47 ms, full page 32 / 108 ms; `test_inbox_speed.py` budgets unchanged and green.
- **Verification:**
  - full suite 5,940 passed, 0 failed; ruff, format, mypy and `makemigrations --check` clean;
  - `test_inbox_layout.py` (new, 10 tests); seven layout tests updated to the bar and the one-row header;
  - `inbox_qa.mjs` 79/79, `QA_LAYOUT` 5/5 sizes, `QA_COMPOSER` with WhatsApp on (a second local server, every POST intercepted);
  - the contrast scan in light and dark: 0 failures; `area_scan /inbox` 26 pages, 0 issues.
- **Open:** S4 ("a stale form answer never replaces the next conversation") failed in 2 of 4 full runs. The tag form posted as a full page, then passed in the 2 later runs, and the cause wasn't found. The harness's "no element" failure now prints the recent requests.

### Entry 28: Inbox Phase 3: Content Studio's look, and controls that act at once (2026-11-06)
Plan: the Inbox plan of 2026-11-06, Phase 3.

- **Title:** the bar's title reads as Content Studio's does. A small "Inbox" crumb sits over the view's name in large type, and the name is the view menu, with its count and the missed count beside it (`_inbox_views.html`). The crumb is plain text: as a link it only led back to the same page, and at 16 px tall it was a poor tap target.
- **Buttons:** yellow is only for Send (and, once the window has closed, the inline "Send template" that stands in for it). The dialog's Send template, AI "Use reply", "Review template" and "Save answer", "Create lead", and the follow-up Save are tonal. "New messages" is a solid navy pill. The mic stays navy.
- **Channel filter:** a `cvMenu` on the same GET form. Its items are submit buttons, the button shows the channel in force, and there's a `<noscript>` select.
- **Template picker:** a list to read (name, kind, language, the first two lines of text), built from radios, so the keyboard and a page without scripts both work. It replaces the select in both the composer and the dialog (`_template_form.html`).
- **Details menus that post:**
  - Pause here (1 hour, 4 hours, a day, until turned back on), Run one, Choose a form;
  - Later: its items post, and "Pick a date" opens a small time form in place;
  - each keeps its plain form in `<noscript>` for `panel_page.html`.
- **Disclosures and links:** "What automations did" is a styled disclosure with a count (still `<details>`, so it works without scripts). "Why didn't it reply?" is an arrow link. The plan's switch item didn't apply: the inbox has no on/off checkboxes.
- **Surfaces and type:** the list, conversation, details and skeleton use the surface radius with no shadow. The details' section headings use Content Studio's card-heading size.
- **Motion (150 ms):**
  - the details and a newly opened conversation fade in;
  - rows fade up when a search replaces the list (`.is-fresh`), never on the 8-second refresh;
  - menus already fade;
  - rows, menu items and saved replies share one hover;
  - none of it runs under reduced motion.
- **Speed** (queries / median server time, local data): list 22 / 57 ms, feed 13 / 36 ms, pane 17 / 40 ms, panel 21 / 36 ms, full page 32 / 102 ms. Unchanged from Phase 2.
- **Verification:**
  - `test_inbox_look.py` (new, 8 tests);
  - full suite 5,946 passed. 2 failed in a run slowed to 2 h 46 min by the browser checks running alongside (the 5,000-contact import load test, and a time-based automation test); both pass on their own, and neither touches the inbox;
  - ruff, format, mypy and `makemigrations --check` clean;
  - `inbox_qa.mjs` 79/79; `QA_LAYOUT` 5/5; `QA_LOOK` (new) with WhatsApp on, in dark and light: menus open, a template can be picked;
  - the contrast scan in both themes: 0 failures; `area_scan /inbox` 26 pages, 0 issues.

### Entry 29: Inbox Phase 4: the keyboard (2026-11-06)
Plan: the Inbox plan of 2026-11-06, Phase 4.

- **J/K** open the next or previous conversation in the pane, as mail clients do (`static/js/inbox_pane.js`, capture phase, so app.js's page shortcuts step aside).
  - Focus stays on the row, so J/K carry on from there.
  - The first J adds a history entry, and further steps replace it, so Back returns to the list rather than stepping back through every peek.
  - Below 1024 px, where there's no pane, they move between rows as on other pages (rows carry `data-row`), and Enter opens one.
- **Read state:**
  - a J/K load asks for `?peek=1`, which doesn't mark the conversation read (`views.conversation_detail`);
  - it's marked read (`action=mark_read`) once it has stayed open 1.5 s;
  - moving on, or closing the pane, cancels that, so five quick J presses mark only the last one;
  - a click still marks read at once.
- **R** focuses the reply box (when a reply can go).
- **E** closes the conversation. Every close answered in the pane or panel (the ⋯ menu's too) now carries an Undo:
  - the state before the close (status, assignee, close reason, close time) is signed into the Undo's address, so the server keeps nothing and nothing can be forged (`views.undo_close`, POST only, 2 minutes);
  - Undo puts all of it back, an earlier close reason included;
  - Undo refuses if anything changed since (the customer wrote, someone reopened or closed it again), if the link is someone else's, or if it has expired, and says so;
  - the pane's toasts now keep their Undo (`_messages_to_toast`).
- **A** assigns the conversation to you. **Esc** goes back to the list, with focus on that conversation's row.
- **Where these keys stay inactive:** while typing, with a dialog or menu open, and below 1024 px for R, E, A and Esc. All are listed under "?".
- **N** is now per page: `data-shortcut-new` on Content Studio's tabs (`publishing/_subnav.html`). Before, it fired on any `/publishing/` address, edit forms included. The inbox has none.
- **Rows opt out of Content Studio's `[data-row]` arrival fade,** which would otherwise run on every 8-second refresh; they still fade on a search.
- **Verification:**
  - `test_inbox_keys.py` (new, 10 tests): peek, mark read, click; close then Undo restoring status, assignee, reason and close time; an earlier close reason; no change after the customer wrote; forged and someone else's links; POST only; the hooks on the page;
  - `QA_KEYS` (new) at 1280: the first J peeks with focus on the row; five quick J presses marked read 0 times in the first second, then once, for the conversation stopped on; K, R, A ("You"), E (closed, toast with Undo, Undo reopens it), Esc (list, focus on the row); a J typed in the search box does nothing;
  - ruff, format, mypy and `makemigrations --check` clean.
  - with Phase 5A–5C in place, the full regression: `pytest -n 4` (full suite) passing, ruff, format, mypy (695 files) and `makemigrations --check` clean; `inbox_qa.mjs` 79/79; `QA_LAYOUT` 5/5; `QA_KEYS`, `QA_COMPOSER` and `QA_LOOK` as above; the contrast scan in both themes 0 failures; `area_scan /inbox` 26 pages, 0 issues.
- **Fixed on the way:** `test_inbox_look`'s reduced-motion check read only the file's last such block, which is now this phase's; it reads every block.


### Entry 30: Inbox Phase 5A–5C: instant open, one look for bubbles, the template picker (2026-11-06)
Plan: the Inbox plan of 2026-11-06, Phase 5 as revised after the prod review of the same day (items 1–4).

- **5A, opening feels instant** (`static/js/inbox_pane.js`):
  - **Named skeleton:** the loading pane shows the row's name and avatar at once (`fillSkeleton`).
  - **Fetched ahead:**
    - a row hovered for 150 ms, and the next one after J, are fetched ahead, one at a time;
    - each is a peek with `X-Inbox-Prefetch: 1`, so it is never marked read and never uses up a queued toast (`views.conversation_detail`).
  - **Kept panes:**
    - the last 5 panes are kept for 60 s, each only while its row in the list reads the same (status, assignee, last message, "Not sent"), so a stale pane is never shown;
    - anything posted from the pane drops its copy;
    - new messages after it come from the thread's own poll;
    - a click on a kept peek still marks it read at once.
  - **Headers:** the pane response now has `Vary: HX-Request, HX-Target` and `no-store` (it didn't).
  - **Background requests:** they already pause while the tab is hidden (list, both badges, thread), so nothing to change there.
  - **The ~900 ms gap on prod:** not reproducible here. The sign-in page alone showed `cfOrigin=579` ms against `cfEdge=4` ms, so the time is between Cloudflare and the app, which matches Phase 0's worker-queue finding (Entry 26). Workers are the user's decision.
- **5B, bubbles and errors:**
  - **One palette on every channel:** incoming on the surface colour, outgoing a navy tint (`--color-brand-500` at 16 %, 34 % in dark), the page ground behind. Dark mode follows the tokens; the WhatsApp greens and the Instagram purple are gone.
  - **Grouping:** messages from the same side and sender within 5 minutes form a group. Only the last shows the tail, time and ticks; the others show their time beside the bubble on hover (`.cv-hover-time`, out of the flow).
  - **AI chip:** "Sent by AI" is a chip.
  - **Account-level failures** (billing, display name, connection) are said once, in a banner above the composer with the fix, whether the window is open or closed. Each message they stopped says "Not sent ⓘ", with the reason on tap. Message-level failures keep their full text.
- **5C, the template picker** (`_template_form.html`, `tplPicker` in `chat.js`):
  - **Inline (window closed):** one "Send a template" button opens the picker over the thread; on a phone it's a sheet from the bottom.
  - **Picker contents:**
    - readable names (`MessageTemplate.label`);
    - search past 3 templates;
    - each value a labelled field (`MessageTemplate.fields`: "Value 1" for `{{1}}`, Meta's example as the hint);
    - a live preview filled the way the send fills it (position i fills `{{i}}` or `{{name}}`, as `campaign_ui.render_body`);
    - Cancel.
  - **Keyboard:** Esc or a click outside closes it, with focus back on its button. The inbox's Esc no longer closes the conversation while a non-modal dialog is open.
  - **Send:** disabled, and not yellow, until the template and every value are in.
- **Verification:**
  - **Unit tests:**
    - `test_inbox_fast.py` (3): fetch ahead keeps the toast and the unread, pane headers, skeleton slots;
    - `test_inbox_bubbles.py` (7): labels, fields, the picker, search, one banner, "Not sent", the AI chip;
    - updated: the look test (two yellows, never at once), the layout test (the banner), and the confidence test (no per-channel palette).
  - **`QA_FAST` (new), at 1280:**

    | Open | Name shows | Whole pane | Requests |
    |---|---|---|---|
    | Cold click | 10 ms | 401 ms | 1 |
    | After a hover | 17 ms | 17 ms | 0 |
    | Reopened | 31 ms | 31 ms | 0 |
    | J (next fetched ahead) | 22 ms | 22 ms | — |

    Mark-read as planned.
  - **`QA_PICKER` (new), with WhatsApp on:**
    - the thread stays 406 px at 1280×631 with the picker open, which sits over the thread;
    - the preview fills in as you type;
    - Send is grey, then yellow;
    - Esc closes it with focus back on its button;
    - one banner, one reason, one Fix link, two "Not sent";
    - grouping and the AI chip;
    - a 390 px sheet;
    - light and dark.
  - the regression in Entry 29 ran with this phase in place. `QA_LOOK` opens the picker before choosing a template.
- **Not done here (5D–5F need a go-ahead):** two-line rows, the empty pane, wording, live feel, and the responsive harness.

### Entry 31: Inbox Phase 5D–5F, the blink, two prod bugs, nginx compression (2026-11-06)
Plan: the Inbox plan of 2026-11-06, revised Phase 5, items 5–8 and the responsive spec. Also: the user's signed-in prod measurement, the list's 8-second blink, and two bugs from the prod review.

- **The ~900 ms gap: nginx sent HTML uncompressed.**
  - **The measurement (the user's, signed in):** the list spent ~600 ms more than the follow-up badge between the origin and Cloudflare, growing with page size, not with load.
  - **The cause:** `nginx/conf.d/default.conf` had no `gzip`, and the `nginx:1.27-alpine` image ships it off. Cloudflare pulled the 123 KB list uncompressed over the long link.
  - **The fix:** gzip for HTML, JSON, JS, CSS and SVG, with `gzip_proxied any`. `text/event-stream` is left out, so `/events/` isn't held back. Checked with `nginx -t` on the production image (throwaway certificate).
  - **Also checked:** `ServerTimingMiddleware` is outermost and `render()` runs inside it, so `app` includes rendering.
  - Rolling out the config, and the server-side `curl` check, are the user's steps.
- **The blink:**
  - **Cause:** a site-wide `.htmx-request { opacity: .55; pointer-events: none }` dimmed the list for every 8-second check's whole round trip, and blocked clicks on it.
  - **Background refreshes no longer look busy:** app.js marks background refreshes `.ak-auto-request` (the requests it already classed as `_akAuto`: a timer or a pushed event on a `data-auto-refresh` element), and those never dim or block. A click on the same element (Next page) still shows busy.
  - **Search** keeps its box usable and shows a thin line under it.
  - **AI ideas poll:** `publishing/_ai_ideas.html`'s 2-second poll gained the marker.
  - **Header redraw:** `chat.js applyStatus` updates the header badges' hover text in place when only that changed (a "(5 minutes)" in a title), instead of re-rendering them.
- **Prod bugs:**
  - **Words run together in AI text:** the AI's narrow no-break spaces (U+202F) rendered as almost nothing. `proposals.plain_spaces` turns odd spaces into plain ones and drops zero-width ones, keeping emoji joiners, where a proposal is checked (reply text and template values). AI messages sent before this show with plain spaces (`_message_json`).
  - **"You're welcome…" twice:**
    - **The race:** a direct send's outbox row stayed QUEUED while it checked the window and eligibility (a Meta call). The outbox drain could claim it in that gap, and both sent it.
    - **The fix:** `send_outbound` now claims the row (QUEUED to SENDING, conditional update) before sending, and the drain passes `claimed=True`. Whoever claims first sends; the other stops, and the inbox shows the row as on its way, not failed. Both Instagram and Facebook.
    - Not confirmed against prod data; the race is real either way.
- **5D:**
  - **Rows** are two lines (65 px, from about 96), with the channel on the avatar's corner and the badges dropping out whole.
  - **Quick actions** on hover: Close (with Undo), Assign to me, Follow up tomorrow. They post the pane's actions with `X-Inbox-Row`, answered 204 with the toast and an `inbox-refresh` (`views._row_action`).
  - **Wording:**
    - "Quiet" is now "Waiting on them", with a help tip in the header and hover text on the row;
    - Messenger says "24-hour window closed";
    - "Why?" reads as a tip (a ? icon, no underline);
    - the bar's icons have tooltips;
    - follow-ups use a calendar icon, not the bell (bar, rows, sidebar).
  - **The empty pane** says what needs you (waiting, missed, unassigned, follow-ups due), each a link to its view, plus the keys. It refreshes with the list.
- **5E:**
  - **"New messages"** where the customer's unread run starts (`_first_unread`, the same run `_recent_thread` keeps), and the thread opens on it if more is unread than fits.
  - **Arrivals:** messages that arrive while it's open slide in.
  - **Changed rows:** a row whose content changed on a refresh is highlighted for a moment; times don't count as a change.
  - **Screen readers:** the thread is already `role="log"` with `aria-live`, so no second announcement.
  - **Not built:** the customer typing indicator; Meta sends none.
- **5F:**
  - **Breakpoint:** two panes from 768 px (it was 1024): a 320 px list (380 from 1024), with details as a drawer and docked from about 1440.
  - **Under 768:** one pane at a time. The conversation slides in with Back, the reply box clears the safe area, and the details open full screen.
  - **Touch targets:** "Why?", the reply box and the setup button are 44 px on touch.
- **Verification:**
  - **Unit tests:**
    - `test_inbox_quick.py` (8): row actions, avatar channel, tooltips, the empty pane, New messages;
    - `test_plain_spaces.py` (3);
    - outbox claim tests for Instagram (2) and Facebook (1);
    - updated: the copy rules (no "Remind", no "Follow-ups due"), the Waiting on them tests, and the view-link count.
  - **Harness:**

    | Check | Result |
    |---|---|
    | `QA_BLINK` | list opacity 1 and clickable through 800 ms checks; search shows a line |
    | `QA_ROWS` | rows 65 px, Close and Undo for real, tooltips, the empty pane |
    | `QA_LIVE` | divider, only the changed row highlighted, slide-in |
    | `QA_RESP` at 360, 390, 768, 1024, 1280 and 1440, light and dark | no sideways scroll, panes 1/1/2/2/2/2, thread 458–734 px, 0 touch targets under 44 px, 0 contrast failures |
  - **The regression found two faults in the phone slide, both fixed:**
    - it made the page scroll sideways while it moved (the split now clips horizontal overflow under 768);
    - its `both` fill kept the transform in effect, which made the column the containing block for fixed elements, so the full-screen details sheet came out the column's width. It now fills `backwards`.
  - **Phone drawer check:** the main harness no longer taps a backdrop on a phone, since the details fill the screen. It checks the sheet is full width and that its Close button closes it.
  - **Full regression:**
    - `pytest -n 4`: 5,982 passed, 0 failed. One earlier run reported 37 failures but had collected about 800 fewer tests; two runs after it were clean.
    - ruff, format, mypy (695 files) and `makemigrations --check` clean.
    - `inbox_qa.mjs` 80/80; `QA_LAYOUT` 5/5; `QA_KEYS`, `QA_COMPOSER`, `QA_LOOK`, `QA_PICKER`, `QA_FAST`, `QA_BLINK`, `QA_ROWS`, `QA_LIVE` and `QA_RESP` as above.
    - The contrast scan in both themes: 0 failures, and the inbox's phone screens 0 targets under 44 px. `area_scan /inbox`: 26 pages, 0 issues.


### Entry 32: Inbox Phase 6, the details panel answers who, where, what next (2026-11-06)
Plan: the Inbox plan of 2026-11-06, Phase 6 (from the user's panel proposal). Docking stays at about 1440, the Phase 2 decision.

- **The order, on every chat:** who, Status, Next step, Customer value, Activity (folded), and Delete, set apart. An empty section shrinks to one line with its action ("No follow-up · Set one", "No notes · Add note", "No tags").
- **Who:** avatar with the channel mark, name once, phone with copy, email only when there is one, and "Customer since Nov 2026 · 2 conversations · View profile". The Name, Phone, Email, Customer since and Conversations rows are gone.
- **Status:** label over value, action on the right (the docked column is about 290 px, too narrow for a label column).
  - **Status:** Open (· waiting for you / · missed), Waiting on them, or Closed, with Close (keeps its Undo) or Reopen.
  - **Assigned:** You / a name / Unassigned, with Assign to me or Unassign.
  - **Reply window:** "Open · closes in 6h" or "Closed 4 days ago" from the customer's latest message; Messenger counts its 7 days. In the pane, open or closed is the composer's own `windowOpen`, so the two can't disagree (a seeded Instagram chat showed they can). On WhatsApp, "Send a template" opens the composer's picker.
  - **Automations:** On (Pause for… menu), "Paused until 14:00" or "Paused" (Resume). Run one stays a menu. The old "no end" check (year ≥ 2100) never matched: "until I turn it back on" pauses 10 years. Now: more than a year away.
  - **Account problem:** the composer banner's own check (`accountProblem`), with Fix it.
  - A panel action's answer now triggers `cv-changed` as well, and the pane polls at once, so the header follows a Close, Reopen or Assign made in the panel.
- **Next step:** "Only your team sees these" said once. The follow-up keeps its Done, Later and Not needed (the follow-up copy rules); none set folds the form behind "Set one". Notes show the latest, "N more", and Add note.
- **Customer value:** where they stand (or "Not tracked yet · Track as interested"), orders (the open one and what they've paid; a count instead of a total across currencies), tags as chips with Add a tag folded.
- **Activity:** what automations did, forms, earlier conversations (loaded when it opens, `?part=history`), and "Why didn't it reply?" only when the customer's last message was left for the team.
- **Folded forms** are `<details class="pop">`: they work without scripts and the site's Esc handler folds them (the conversation stays open).
- **Speed:** the panel stays at its 21-query budget. Status comes from `with_activity` and the paid total from subqueries on the conversation's own query; earlier conversations cost nothing until Activity opens.
- **Also fixed:** "paused … for 1 hours" now says "1 hour".
- **Verification:**
  - `test_inbox_details.py` (20): order on a full and an empty chat, name and team line once, email, each Status state and action, the window open/closed/Messenger/none, automations three ways, account problem, follow-up folded and set, notes, orders and paid, tags, history lazy and on the no-JS page, every action a form there.
  - Updated: the panel answer's `HX-Trigger`, "Pause for…", the follow-up form's promise said once, "Why didn't it reply?" only when left for the team.
  - Harness `QA_PANEL`: at 1440×900 docked, the follow-up line ends at 625 px (Status and Next step above the fold); a folded form opens, Esc folds it with focus back on its summary and the conversation still open; Pause for 1 hour then Resume, for real; Send a template opens the picker; 390 full-screen sheet and 1440 docked in both themes: no sideways scroll, 0 contrast failures, 0 touch targets under 44 px at 390.
  - `inbox_qa.mjs` 80/80; `QA_RESP` unchanged from Entry 31.

### Entry 33: four small things from the prod review of 5D (2026-11-06)
- **"Waiting for a reply" vs "Waiting on them":** the empty pane's item is now "Needs your reply" (the customer is waiting on you); rows keep "Waiting on them" (you're waiting on them).
- **Names cut off:** in the list, the time column on line 1 was as wide as the widest badge on line 2, so a "Follow-up due" or "Waiting on them" chip took width from the name. The row grid is now three columns (`"name name when" / "preview meta meta"`): the name only gives way to the time. In the conversation header, the name now wins and the status chips take what's left, dropping out whole (a leading empty item lets even the first one wrap away).
- **Hover actions over the preview:** on hover or focus the time and badges step aside, the preview takes the whole line, and only its end fades out before the actions.
- **Follow-ups count on the calendar:** it sits just off the icon's corner (no horizontal overlap), with its ring.
- **Verification:** `QA_ROWS` now also measures the names cut (0), badges cut (0), the time hidden and the preview fading on hover, the count's overlap with the icon (0 px across), and the header's name (not cut, both chips shown). `QA_RESP`: no sideways scroll at any width, 0 touch targets under 44 px on touch sizes, 0 contrast failures. Conversations tests pass.

### Entry 34: Campaigns Phase A, results that tell the truth (2026-11-06)
Plan: the Campaigns plan of 2026-11-06 (from the user's review of live Campaigns), Phase A. The Inbox plan moved to `docs/plans/inbox-app-feel.md`.

- **The outcome, from delivery:** `COMPLETED` only means every message was queued. `campaign_ui.lifecycle_state(campaign, stats)` now gives a finished campaign Failed (nothing accepted), Partly sent (some failed or unconfirmed), Not sent (nobody could be sent it), Sending (still queued or held for plan room) or Sent. The list and the campaign page pass `delivery_stats_for`'s numbers; the public API still passes none, so its vocabulary is unchanged. `components/status.html` knows "Partly sent" (amber) and "Not sent".
- **History:** "completed" says what happened ("Finished: 0 of 2 sent, 2 failed", `outcome_label`); the "sent" event, written when recipients are chosen, reads "Messages prepared for 2 customers". "Every message handed to WhatsApp" and "Sent to 2 customers" are gone.
- **Plain reasons:** `friendly_errors.failure(code, stored)` is the one place for this, shared by Inbox (`conversations/views._failure` now calls it) and Campaigns. A stored sentence that starts with Meta's code ("131042: Business eligibility…") reads plainly too. Each failed recipient shows the reason and "Fix it"; above the table, `campaigns.failure_groups` lists "2 × billing problem · Fix the payment method in Meta", most first (one query).
- **Retry:** while `health.account_problem` stands (the latest settled send failed with 131042, 131037 or 190, and nothing has gone through since) and the failures are account-level, the fix comes first and Retry becomes a quieter "Retry anyway" whose confirm says why it would fail. Message-level failures keep "Retry N failed".
- **Before sending (warn and confirm, the user's choice):** the composer shows the problem with its fix in step 1 and on Review. Send asks "Send anyway?" with the reason, then posts `confirm_account_problem=1`; without it the server refuses with the reason (400) and nothing is created. Without scripts, a "Send anyway" box on Review does the same.
- **Budgets (`test_campaign_speed.py`):** list 15, campaign page 19, composer 19 queries, the same with 6× the campaigns and recipients.
- **Verification:** `test_campaign_results.py` (14): outcome three ways, badges and history on both pages, grouped plain reasons with the fix, a stored sentence, Retry swapped and kept, `account_problem` cases, the warning in steps 1 and 4, Send refused then sent with the confirm, and Send asking first. Full suite: one scheduler test (`test_drain_fires_due_message`, untouched) failed in the parallel run and passes alone. ruff, format, mypy, migrations clean. Not yet checked in a browser: the Campaigns harness comes in Phase G.

### Entry 35: Campaigns Phase B, the composer's bugs (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase B.

- **Wrong start time:** `delivery.forecast` found 08:00 in the workspace's zone correctly, but `_campaign_estimate.html` printed it with `|date`, which turns an aware time back into the server's zone (UTC): 08:00 in Lusaka read "Sat 06:00 (Africa/Lusaka)". Every forecast time is now shown with `|timezone:<zone>` first. A test with the workspace in Africa/Lusaka at 22:00 on a Friday asserts "Sat 08:00 (Africa/Lusaka)"; with the old filter it fails.
- **"Send now" outside sending hours:**
  - step 3's line under Send now comes from the forecast for now (`pace_now`): "Your sending hours are closed now, so it starts Sat 08:00 (Africa/Lusaka)";
  - Review's When reads "Starts Sat 08:00 (…) · Outside your sending hours now", and a scheduled time outside the hours adds when it really starts;
  - after sending, the message says "Your campaign is ready. Your sending hours are closed, so it starts Sat 7 Nov 2026, 08:00 (Africa/Lusaka)." instead of "on its way".
- **Authentication templates:** `campaign_ui.template_options` leaves them out, and `campaigns._check_sendable` refuses one posted by hand ("Meta doesn't allow those in campaigns").
- **Fill in the blanks:**
  - a customer detail shows only its fallback, filled in from the start ("there" for a name, `variables.default_fallback`) and required;
  - fixed text shows only its box and the warning, and is required;
  - the field not chosen is disabled, so it isn't sent;
  - without scripts both show and the server reads the one matching the choice (as before);
  - the choice reads "Fixed text, the same for everyone".
- **Harness:** new `tools/qa/campaigns_qa.mjs` (the Inbox harness's plumbing, every POST intercepted). `QA_BLANKS`: a name shows only its fallback ("there", required), fixed text only its box and warning, and back again; the preview fills in after a change.
- **Verification:** `test_campaign_composer.py` (7): the start time, Send now in step 3 and Review (closed and open), the message after sending, authentication templates neither offered nor sendable, the fallback filled in, and only the chosen kind of answer counting. Full suite with no failures; ruff, format, mypy and migrations clean.

### Entry 36: Campaigns Phase C, one address and honest wording (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase C. Phases A and B were committed by the user (70b7dac, 5058110).

- **One address, `/campaigns/` (`apps/core/campaign_urls.py`):** the list, `new/`, `estimate/`, `test/`, `settings/`, `lists/`, `<pk>/`, `<pk>/export.csv` and `<pk>/<action>/`. URL names are unchanged, so every `{% url %}` and `reverse()` lands there; 17 hard-coded links in templates and views now point there too.
  - **Old addresses** (`/email/campaigns/`, `/whatsapp/campaigns/…`) redirect with the query string kept: 301 for a GET, 308 for anything else, so a form posted to an old address still posts. The email composer and email campaign pages keep their `/email/campaigns/…` addresses, and the public API keeps `/api/v1/whatsapp/campaigns/`.
  - **Switched off:** the WhatsApp pages 404 while WhatsApp is off (they were hidden by ChannelGateMiddleware under `/whatsapp/`), and the list 404s with neither email nor WhatsApp on, as `/email/campaigns/` did.
  - **Sidebar:** Campaigns matches `campaigns`.
- **"by email or WhatsApp":** the list's line names only the channels that are on ("…on WhatsApp" while email is off).
- **Lists in Campaigns' context:** the same page at `/campaigns/lists/` (from the list and the composer) keeps Campaigns in the sidebar and goes "← Campaigns"; at `/contacts/lists/` it stays under Customers.
- **Tab counts:** one aggregate query for all of them (it was one per tab plus one), and Finished now shows its count. The list's budget drops from 15 to 12 queries.
- **Verification:** `test_campaign_paths.py` (12): every page under `/campaigns/`, six old addresses redirecting, a POST to an old address (308), the WhatsApp pages off with WhatsApp, the wording with email off and on, Finished's count, and Lists from Campaigns and from Customers. Existing tests moved to the new addresses (the API and nav-resolution tests keep theirs). `campaigns_qa.mjs QA_PATHS`: old addresses land on `/campaigns/`, and the sidebar stays on Campaigns through Lists (boosted) and back. Full suite: only `test_import_5000_contacts` failed, the load test known to fail on a clean checkout; ruff, format, mypy and migrations clean.

### Entry 37: Campaigns Phase D, a list that shows what campaigns did (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase D. Phase C was committed by the user (1cbe840, 6d6fcdb).

- **Result columns:** Sent ("2 of 3": accepted by WhatsApp, of the people on the list), then Delivered, Read, Failed and Replied, each as a share of the messages the campaign prepared, so a row reads across (`campaign_ui.rates`, `percent`). A few never round to "0%" ("<1%"), nearly all never to "100%" ("99%"), and nothing to share shows "—". The exact counts are on hover. The Not sent column went; its count is in Sent's hover.
- **Replied** (the user's decision: replies now, sales after a tracked code): the recipient wrote back within 3 days of their campaign message reaching WhatsApp, as an inbound `MessageLog` from the same WhatsApp contact (`campaigns._replied`, an `EXISTS` on the contact/timestamp/direction index; `reply_counts_for`, one grouped query per page). The column's help tip says it counts replies, not why they wrote. A reply within the window of two campaigns counts for both.
- **Tiles, once 3 campaigns have gone out:** Sent this month, Delivery rate and Replies, each with the Sales sparkline over the last 8 weeks, plus the last campaign's name, status, outcome and fix (`campaign_ui.summary`: one query over 8 weeks of messages, grouped by the workspace's own days). The sparkline geometry moved to `apps/core/charts.kpi_spark`, shared with Sales: a week with nothing sent is a gap, not a 0, and one reading draws no line. No Orders tile until Phase H.
- **Before that (`whatsapp/_campaign_start.html`):** the last campaign's outcome with its fix ("Finished: 0 of 2 sent, 2 failed · Meta blocked … · Fix the payment method in Meta"), and up to three approved marketing or utility templates to start from, each opening the composer with it chosen (`?template_id=`). With none approved, it says how to get one. With no campaigns at all, the first-run checklist stays as it was.
- **Speed:** the list's budget goes from 12 to 16 queries (15 with the start card). The 4 new queries are replies, the last campaign's failure reasons, and for the tiles the timezone and the 8-week query. None of them grow with campaigns or recipients (tested both ways). The latest campaign is taken from the loaded rows when it's there.
- **Verification:** `test_campaign_list.py` (13): shares and rounding; rates from delivery; replies within 3 days only, once per person, none for a failed message or a skipped recipient; the columns and help tip; tiles with 3 campaigns (values, trend hover, the last campaign with its fix); this month in the workspace's own days (00:30 on 1 November in Lusaka); a week with nothing sent as a gap; the start card with its fix and starting points (authentication left out); a starting point opening the composer with it chosen; no approved message; drafts only. `campaigns_qa.mjs QA_LIST`: six widths in both themes, start card and tiles. No sideways scroll, no contrast failures, no overflow. Found and fixed: the last campaign's name link was 24 px tall on touch (now 44), and "0 of 0" and "Not sent" wrapped at 1280. The help tip's 24 px button is the shared component. The tiles run used a temporary third campaign in the dev workspace, deleted afterwards.

### Entry 38: Campaigns Phase E, a campaign's page leads with what it came to (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase E.

- **The outcome first** (`campaign_ui.outcome_banner`, in `_campaign_live.html`). Once a campaign has gone out, a banner says what it came to:
  - all failed: "2 of 2 failed", the biggest reason in plain words, and the account fix;
  - partly sent: "Sent to 1 of 2, 1 failed" and the reason;
  - all sent: "Sent to 3, 67% delivered";
  - nobody reachable: "Nobody on the list could be sent it".

  The fix shows only while `health.account_problem` holds, as Retry does since Phase A: once a message has gone through since, there's nothing to fix. There's no banner while it is still sending, where the progress line stays.
- **The funnel** (`campaign_ui.funnel`) replaces the five equal tiles: On the list → Sent → Delivered → Read → Replied. It uses the Sales funnel's bars, each a share of the widest step, with a `.sf--5` variant that becomes two columns on phones. A line under it says what each step means. Replied is counted as on the list (within 3 days, `reply_counts_for`).
- **The message** (`campaign_ui.message_with_sources`): the composer's WhatsApp-style preview with each blank showing where its value comes from (`{Customer's first name, or "there"}`), the fixed text itself, or `[name]` when unmapped. Shown for drafts too.
- **Recipients:**
  - the tabs show their counts (Everyone, Sent, Failed, Not sent);
  - each failure reason has "Show these N" (or "Show who"), filtering the table through `campaigns.failed_for_reason` by the group's error codes (`key`; "none" for a failure stored without a code);
  - while filtered, a line names the reason, with "Show every failure";
  - a reason that isn't this campaign's is ignored;
  - the filter survives pagination;
  - the 5-second live poll now also updates the tab counts out of band, and stops when the campaign is done, as before. The live partial now sends `Vary: HX-Request, HX-Target` and `no-store`.
- **Skeleton:** `skeleton-campaign` (title, banner, funnel, rows). `app.js` shows a skeleton for any link with `data-skeleton`, not only tabs, filling `#main` after 150 ms and restoring it if the navigation fails. Every link that opens a campaign carries `data-skeleton="campaign"`.
- **Speed:** the detail budget goes from 19 to 20 queries; the one added is who replied, for the funnel.
- **Verification:** `test_campaign_detail.py` (15): the banner for all failed, partly, all sent, after a later send (no fix) and while sending (none); the funnel's steps and shares, including replies and a reply before the message; the message's sources; the tab counts; the per-reason filter (two codes, an unknown reason, a code-less failure); the live poll's out-of-band counts, headers, and stopping; the skeleton link and template. `campaigns_qa.mjs QA_DETAIL`: six widths in both themes with no sideways scroll, no contrast failures, no overflow and no small targets among the new controls; the skeleton shows while the page is held back and is gone once it lands. The dev workspace has no finished campaign, so the banner was checked by the tests only.

### Entry 39: Campaigns Phase F, the composer reads like the rest of the app (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase F.

- **Templates as a list to read:** the composer's message step uses the Inbox picker's look (`.cv-tpl-option`) as a radio list. Each option has the readable name (`MessageTemplate.label`: "Download app", not `download_app`), its kind and language ("Update · EN"), and the start of its text with blanks named. Past three templates there's a search box over name and text, with a "No message matches" line; it's hidden without scripts. Templates campaigns can't send stay listed, dimmed, with the reason. The form carries `campaign-composer`, which gives the borrowed Inbox styles their colours.
- **Fill in the blanks:** each blank is a fieldset with a segmented choice, Customer detail · Fixed text (`src__<key>`), then either the menu of details with its fallback, or the text box with its warning. The server reads the kind chosen (`src__=literal` wins over the menu, so a page without scripts, which sends both, still means what was chosen), and an older form or draft link with `var__=literal` still works. A blank that isn't a detail (a code, a date) starts on Fixed text, and a draft with fixed text reopens on it. Found in the browser and fixed: switching back to Customer detail fired the preview's request before Alpine re-enabled the menu, so the preview showed an empty blank; the preview now waits 50 ms after a change. The blank's question sits inside its box, not on the border.
- **Review:** a Cost line with no price ("5 utility messages, charged by Meta to your WhatsApp account", or marketing): Meta charges the business's own account, at rates Akilent doesn't know. The message is named readably.
- **Test send:** a quiet disclosure with a chevron (the Inbox details' `.cv-disclosure`), not a card.
- **Buttons:** Sending hours' Save is navy (`btn-secondary`); the final Send stays the page's one yellow button.
- **Verification:** `test_campaign_wizard.py` (10): the list's names, kinds and text; search past three only; fixed text winning over the menu; a detail using the menu and its fallback; a non-detail blank starting on Fixed text; a fixed-text draft reopening on it; the cost line for utility and marketing; the readable name on Review; the disclosure; navy Save and yellow Send. `test_campaign_composer.py` was updated for the new control. Harness:
  - `campaigns_qa.mjs QA_BLANKS` now drives the segmented control. Its preview text had been losing every "s" (an unescaped `\s` in a template literal), now fixed.
  - New `QA_WIZARD`: six widths in both themes, with no sideways scroll, no contrast failures and no overflow. The segmented items were 30 px on touch, now 44. Search with no match shows none and says so, and Review shows the cost and opens the test field.
  - The search run used three temporary approved templates in the dev workspace, deleted afterwards.

### Entry 40: Campaigns Phase G, look, speed and small screens (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase G (the last before H).

- **Look:**
  - every Campaigns page (list, a campaign, the composer, Sending hours) has Content Studio's header (`.cs-head`, `campaigns/_title.html`): the page's name in the shared title size under a small "Campaigns" crumb (none on the list itself), the subtitle under it, and through `.cs-head` the shared heading sizes and flat cards below;
  - Sending hours' on/off is the shared `.switch` (`role="switch"`, a 44 px row), with the times in the shared input style.
- **Local times:** the campaign page's "sent …"/"created …" and History, and the list's row times, were printed with `|date` on an aware time, which shows the server's UTC (the Phase B bug again). They now read in the workspace's zone; 22:30 UTC on 6 Nov reads 7 Nov, 00:30 in Lusaka. The campaign page's subtitle names the template readably.
- **Speed: query budgets lowered** (`test_campaign_speed.py`):
  - list 16 → 15: the email plan check only on the Email tab;
  - campaign 20 → 19: one count by status and skip reason replaces two, and Pause and Cancel share one "still waiting" check, with the workspace's timezone added;
  - composer 19 → 17: readiness from the templates already loaded, so an account whose only approved template is a one-time code isn't "ready";
  - Sending hours given a budget of 13.
- **Speed: server time with 50 campaigns × 500 recipients** (25,000 messages, 40 replies per campaign; SQLite on the dev box; a throwaway script, deleted). The machine was noisy for part of the run, so the before/after comparison was taken in a single run.

  | Page | Server time |
  |---|---|
  | list | 245 ms (was 422 ms) |
  | a campaign | 47 ms |
  | a campaign filtered by reason | 37 ms |
  | composer | 36 ms |
  | live part | 41 ms |

  All are under the 300 ms target. What changed on the list:
  - **Per-day function gone:** the tiles grouped 8 weeks of messages by local day with a date function per row (a Python call per message on SQLite). They now count 9 ranges (8 weeks and this month) with plain comparisons worked out in the workspace's zone (`campaigns.activity_in_ranges`).
  - **One reply check, not two:** the list's Replied column reuses the tiles' reply counts for campaigns that started inside their 8 weeks, and counts only older campaigns on their own (`summary` returns `replies` and `since`). In the same run, the tiles query went from 244 to ~165 ms, and the 121 ms reply count disappears for recent campaigns.
  - **Remaining cost:** the reply check (an `EXISTS` per message). Buckets alone take 24 ms.
- **Phones:** checked, not changed. The composer's step bar already reads "Step 2 of 4" below 640 px, its preview already stacks under the step, and the list and recipient tables are cards (`table--cards`).
- **Verification:**
  - **Tests:** `test_campaign_look.py` (5): the header on every page; the switch; local times on the campaign and the list; replies reading the same inside and outside the tiles' weeks; a one-time-code template alone not being ready. Budgets for four pages.
  - **Browser sweep** (`campaigns_qa.mjs`): `QA_SETTINGS` (new: the switch, its 48 px row, the times hiding when off, navy Save), `QA_LIST`, `QA_DETAIL`, `QA_WIZARD`, `QA_BLANKS` and `QA_PATHS`. Six widths in both themes, with no sideways scroll, no contrast failures, no overflow and no small targets among Campaigns' own controls.
  - **`area_scan /campaigns`:** 6 pages at 360 and 1280 in both themes, 0 issues. Under Git Bash it needs `MSYS_NO_PATHCONV=1` and Windows paths; otherwise `/campaigns` becomes a Windows path and nothing is scanned.

### Entry 41: Campaigns Phase H, chats and orders credited to a campaign (2026-11-07)
Plan: the Campaigns plan of 2026-11-06, Phase H (the user's decision: replies now, sales only from a tracked signal).

- **The signal:** WhatsApp's own reply reference, not a code added to messages. When a customer swipes to reply to a campaign's message, or taps one of its buttons, the webhook carries `context.id`, the id of the message answered. Every inbound webhook is already stored (`MessageLog.raw_payload`), and every sent campaign message keeps its WhatsApp id, so no template, button or sending change was needed.
  - `whatsapp.interactive.replied_to` reads the id.
  - `conversations.entry_points.match_campaign_reply`, run with the other inbound hooks and never raising, links the conversation to that campaign once.
  - A message typed on its own is a reply (Phase D's Replied), never a credit, even minutes later.
  - Another workspace's campaign message is never matched.
- **Migration** (`conversations/0029_entry_point_whatsapp_campaign`):
  - `ConversationEntryPoint.post` becomes optional, with a new `whatsapp_campaign` and kind `campaign_reply`;
  - a check that every entry point names a post or a campaign, and one entry per conversation and campaign;
  - `ConversationAttribution.source_whatsapp_campaign`, filled from the first entry point when a lead, deal or order is made;
  - the "a guess credits nothing" constraint now also forbids a guessed conversation naming a campaign.

  Existing rows all have a post, so the check holds on existing data. Applied to the dev SQLite database only.
- **Where it shows:**
  - the list's **Orders** column (count, with the paid count on hover) and an **Orders this month** tile with its 8-week trend (the tiles are now five: three across from 1024 px, five from 1280, the last campaign full-width on phones);
  - an order's or lead's "Where this came from" now says **From a WhatsApp campaign: <name>**, "The customer replied to the campaign's message", linked to it.

  Help tips and the help article (`whatsapp-campaign-lists`, "Replies and orders") say what counts and what doesn't. `docs/marketing/messaging.md` adds the third kind of evidence and a `campaign-orders` claim row (the claims test passes).
- **Content Studio's report keeps its meaning** (`publishing.results.journey`): its chats and "tracking on" read only a post's entry points, and a chat a campaign started still counts among the chats no post is credited with (it used to require no entry point at all).
- **Speed:** the list's budget goes from 15 to 16 queries: orders for the tile, with the column reusing them for campaigns inside the tiles' weeks (as Replied does) and counting only older ones on their own.
- **Verification:** `test_campaign_credit.py` (12):
  - the answered id read from the webhook;
  - a reply to the campaign message linking the chat once;
  - a typed message, a reply to another message, and another workspace's campaign not linking;
  - an entry point with neither a post nor a campaign refused by the database;
  - an order from that chat credited (and counted, with paid);
  - an order from a chat that only wrote not credited;
  - a guessed conversation naming a campaign refused by the database;
  - the list's column and tile;
  - Content Studio's chats and untracked chats;
  - the order page's "From a WhatsApp campaign".

  `campaigns_qa.mjs QA_LIST` with five tiles: six widths in both themes, no problems (a temporary third campaign in the dev workspace, deleted after).

### Entry 42: Inbox pages Stage 1, three trust bugs (2026-11-08)
Plan: the Inbox-area pages plan of 2026-11-08, Stage 1 (approved).

- **1a, forms that couldn't publish:** the Flow JSON upload left out Meta's required `name=flow.json` part, so every publish failed with "(#100) The parameter name is required".
  - One status everywhere (`ConversationForm.display_status`, `components/status_badge.html`): the list, the header and the WhatsApp card no longer say Published, Ready to send and Not published about the same form. Live only when it can be sent: a form with unpublished changes says "Changes not published" (answers are matched against the current questions, so the old Flow isn't sent).
  - Readable errors (`conversations/flow_errors.py`); Meta's raw text kept for operators (`flow_error_detail`, migration `conversations/0030`).
  - A live form republishes as a new Flow and switches only once it's published; a draft is updated in place; a request or validation error never makes a new Flow; the old Flow isn't retired while a template button opens it.
- **1c, duplicate "Wants to buy" comments:** a prod diagnosis (`docs/audits/2026-11-08-duplicate-comments-diagnosis.py`) found four distinct comments from one tester, each from its own webhook. Not an ingestion bug; no clean-up or constraint added.
- **1b, comment rules (freeze exception):** a deliberate exception to the pilot freeze, for trust: old spam ("double your money", crypto, "DM me") was still visible on live posts, and overlapping or over-broad rules mis-tag real customers.
  - `instagram/services/rule_lint.py`: rules are first-match, so it warns when a rule never acts (an earlier rule catches all it would), overlaps another, or uses common or very short keywords (keywords match inside words: "hi" in "this"). Common words are a warning for hide, delete and automatic replies, a softer note for flag and lead rules ("price" is fine there).
  - Merge, with a before → after preview in a modal: the target gains the keywords and channels, the other is paused, never deleted.
  - **Apply to existing comments** (last 30 days, up to 2000): a preview of how many will be hidden or flagged, then a background job (`apply_rules_to_existing`) with a progress card. Moderation only: no email, lead or DM, delete becomes hide, safe to run twice.
  - The activity log in plain words: "Already hidden", "Couldn't hide", "Lead · team emailed", never "None"; "team emailed" only when the email was really sent (`ModerationLog.emailed`, with `backfill`; migration `instagram/0021`).
- **Verification:** `test_flow_status.py`, the publish tests in `test_forms_complete.py` and `test_flows.py` (multipart parts), `test_rule_lint_backfill.py` (23). conversations, instagram, facebook, whatsapp suites green; ruff and mypy clean. Prod live Flow publish on the WABA still to do.

### Entry 43: Inbox pages Stage 2, the sidebar becomes six sections (2026-11-08)
Plan: the Inbox-area pages plan of 2026-11-08, Stage 2 (Phase N of 2026-11-05, as revised).

- **One registry** (`apps/core/sections.py`): Home · Inbox · Customers · Campaigns · Content Studio · Automations, each with its tabs. The sidebar (`components/_nav.html`), the tab bar and Ctrl+K all read it, worked out once per request (`{% nav_sections %}`, no queries). Every page keeps its URL; nothing redirects except the two set-up pages below.
  - Tabs: Home › Overview · Insights; Inbox › Conversations · Comments (Instagram or Facebook on) · Follow-ups; Customers › All customers · Interested · Orders; Campaigns › Campaigns · Message templates; Automations › Automations · Forms · Build with AI · Why didn't it reply?
  - A tab is hidden when it doesn't apply (channel off, optional tool switched off) and locked when the plan lacks it (opens the locked page). A main item with no usable tab is hidden. Exactly one main item is active, in the server render and after Back (each item's `data-nav-exclude` lists other sections' pages under its prefix: `/inbox/forms/` is Automations).
- **The tab bar** (`components/_section_tabs.html`) renders once from `base.html` above any section page with more than one tab: `data-instant-tabs` and skeletons as in Content Studio; at phone width it scrolls sideways with faded edges and the current tab in view; the full-height Inbox gets a compact one. Content Studio keeps its own tabs.
- **Kept for Meta App Review** (`docs/ops/instagram-app-review.md`): Connections stays in the sidebar, Settings › Integrations stays a tab, Content Studio › Channels is unchanged, and "Inbox → Comments" is now literally a tab.
- **Below the six:** Connections, Settings (now with an **Activity log** tab: the old Message history, `logs/messages.html` extends the settings shell), and a **Help** menu (Help & guides, API docs, Contact support when released: `/support/` is businesses writing to Akilent, so it isn't an Inbox tab). Billing moved to the account menu as "Billing & plans".
- **Inbox settings** (`conversations/inbox_settings_base.html`): Saved replies · Teams & routing (`/inbox/settings/teams/`, the teams and the routing rules on one page; `/inbox/teams/` and `/inbox/routing-rules/` go there, and their forms come back there). The inbox bar's Set-up menu and Follow-ups button became one Inbox settings link; Forms left that menu for Automations.
- **Inbox badge** (`/inbox/badge/`): unread conversations for me (assigned to me, to nobody, or to one of my teams with nobody picked) plus follow-ups due for me. Unread is shared by the team like a shared mailbox. One badge per nav, and only the visible nav's copy asks (the existing intersect/visibility rule); it also refreshes on `inbox-refresh`.
- **Ctrl+K** lists every section, tab and account page from the registry, and finds moved pages by their old names ("Message history: now Settings › Activity log").
- **"Navigation got simpler"**: built, then removed the same day at the user's request (not to be shown to users); `Membership.nav_notice_seen_at` added in `accounts/0030` is dropped by `accounts/0031`. Ctrl+K still finds moved pages by their old names.
- Help: Getting started says Home › Insights; other help paths (Inbox → Comments, Settings → Integrations, Settings → AI) still hold.
- **Verification:** `apps/core/tests/test_nav_sections.py` (47):
  - the sections for each gating;
  - every old URL's section and tab;
  - locked tabs and hidden sections;
  - the browser excludes;
  - the sidebar, the account menu and the tab bar;
  - one badge per nav and its counting rules;
  - Inbox settings and its redirects;
  - Activity log in Settings;
  - Ctrl+K and old names;
  - the notice once and never for new members;
  - query budgets: badge ≤ 12, registry 0.

  Updated for the new layout: nav tags, billing lock, follow-ups surfaces, inbox bar, studio badge and Facebook rules-card tests. Tailwind rebuilt (4.3.3).

### Entry 44: Inbox pages Stage 3, shared list components and undo (2026-11-08)
Plan: the Inbox-area pages plan of 2026-11-08, Stage 3 (components 1, 3, 4, 5 and undo). Documented in `docs/design/akilent-ui-spec.md` §19 and on the styleguide.

- **Settings list** (`components/_settings_list.html` + `.slist*` CSS, `{% row_menu %}` → `components/_row_menu.html`): a head with count and an Add button that opens a modal (`?add=1` through `overlay_or_page`; errors come back in the modal as 422), rows with a ⋯ menu, and the shared empty state. Adopted by Saved replies, Teams (Inbox settings › Teams & routing), Forms and WhatsApp Message templates. Add partials are in `templates/conversations/add/`.
- **Message templates:** search, category/status/language filters and a Show samples switch (Meta's `hello_world` and `3p_direct_integration_test_template` are marked Sample and hidden by default, sorted last). The readable name ("Order update") sits over the raw name in `<code>`. That's display only: `display_name` isn't a field, and every send uses `name`. ⋯ Preview / Edit / Duplicate / Delete… replace the bare icons.
- **Message preview** (`components/_message_bubble.html`): the existing `.wa-preview` markup (WhatsApp's own colours kept) with a compact variant. Used for saved replies and automatic-reply rules. `whatsapp-template-preview.js` shares the markup and CSS but wasn't refactored.
- **Status badge** (`components/status_badge.html`, Draft · Pending · Live · Paused · Error + reason): `MessageTemplate.display_status` (`whatsapp/_template_status.html` is now a wrapper), `ConversationForm.display_status`, and `display_status` on `CommentTrigger`/`ModerationRule`.
- **Times** (`{% when value %}`, `apps/core/display.py`): "just now · 5 min ago · 3 hours ago · yesterday · 12 days ago", then "8 Nov, 13:29" (with the year if it isn't this year), counted on the workspace's calendar (`followups.business_zone`). The output is `<time datetime title>` with the exact time on hover. It replaces `timesince` on Comments, Comment rules, Form answers and the conversation panel. Tested at 23:59/00:01, at New Year and with a UTC/workspace mismatch.
- **Undo** (`apps/core/undo.py`, `/undo/`, signed for 90s, a registry of handlers):
  - Follow-up Done → reopen;
  - Pause rule → resume;
  - Hide comment → unhides at Meta, then locally;
  - Delete saved reply → restore.

  Saved replies are soft-deleted (`deleted_at`, `conversations/0031`; the default manager hides them on every read path, and `all_objects` is used only for undo and purge). They're purged after 30 days by a daily Celery beat task. Bulk hide undo comes with bulk select in Stage 4.
- **Verification:** `apps/core/tests/test_shared_components.py`:
  - time boundaries;
  - status mappers;
  - readable names and the raw-name send;
  - the settings-list pages and Add modals;
  - each undo action, including Meta unhide and an expired or forged token;
  - saved-reply read paths and the purge.

  The full suite is green apart from the known `test_import_5000_contacts` failure and a scheduler drainer test that is flaky in the parallel run (it passes alone). The template-list test was updated for escaped apostrophes, and the pending reason is shown again. Tailwind rebuilt (4.3.3).

### Entry 45: Inbox pages Stage 4, plus the 2026-11-08 production walkthrough's quick fixes (2026-11-08)
Plan: the Inbox-area pages plan, Stage 4, and item 1 of the user's production walkthrough (with the small consistency items it named).

- **Comments** (`comments.comment_feed`, `_comment_thread.html`, `_comment_items.html`):
  - one list across Instagram and Facebook, newest first, **Not answered** by default (one post, from its row or Content Studio, defaults to Everything);
  - each comment shows its post (picture, caption, link to the live post) and its replies nested beneath;
  - posts with no comments aren't listed;
  - "Load more" pages on by time (`?before=`, answered with just the rows);
  - hidden and deleted comments fold into "3 hidden · show";
  - ⋯ per comment;
  - bulk select with **Hide** (Undo unhides each at Meta, partial failures reported, `undo_handlers.comment_hide_many`) and **Delete…** ("Permanently delete 7 comments from Instagram and Facebook? This can't be undone.");
  - channel tabs only when both channels are on;
  - queries are constant in the number of comments and replies (tested).
- **Follow-ups:** rows are `[data-row]` (J/K, Enter). Later's presets are worked out on the workspace's clock: "In 1 hour", "This evening, 18:00" (until 17:00), "Tomorrow, 9:00", "Next week, Sun 15 Nov, 9:00" (`followups.later_choices`, `"evening"` choice). The email setting is a switch. Done's Undo is from Stage 3. "Open chat" stays a page (as planned).
- **Why didn't it reply?:** opens as a drawer from a conversation's ⋯ menu (`overlay_or_page`, static). The page's picker is a search (`?pick=1&q=`).
- **No full reloads:**
  - `/automations/` shipped an inline `templatePicker()`, which made `<main>` unboosted, so its tab links to `/build/` and `/email/templates/` fully reloaded too;
  - Message templates (email tab) loaded `template-starter-picker.js` and `code-tabs.js`;
  - all of these now load with the shell (`static/js/page_factories.js`, `akilent.onPage`), and the AI draft inline script is `data-shell-safe`;
  - `apps/core/tests/test_no_full_reload.py` checks every section tab plus the Stage 4 pages.
- **J/K then Enter:** Enter opens the focused row; J/K step from the focused row; the 8-second list poll puts focus back on the same conversation (`inbox_pane.js`).
- **Inbox badge:** one request per poll. A hidden `data-badge-source` in the desktop nav asks; both navs' badges and the inbox's "Follow-ups to do" copy it (`app.js syncBadges`, `data-due`).
- **Stored Flow errors:** errors saved before Stage 1a in Meta's raw wording are translated when shown (`flow_errors.readable`, `ConversationForm.flow_error_text`). Operators keep the raw text under Details.
- **Rule lint:** a lead or email-the-team rule on common words ("Potential lead": service, help, link, contact) is now a banner warning; flag-only rules keep the quieter note.
- **Consistency:**
  - Meta's Approved shows as **Live** ("Approved by Meta. Ready to send.");
  - the Templates page is titled "Message templates", like its tab;
  - Insights wording is fixed: "1 unanswered conversation needs attention", "worth a message from you", and the alert email's verb;
  - each insight shows when its issue started ("Started 3 hours ago", the longest wait) or when it was last checked, not when the insight row was created;
  - "Reload for the latest" became a Refresh link.
- **Alpine "Unexpected identifier 's'":** not reproduced. `apps/conversations/tests/test_alpine_quotes.py` renders the inbox, a conversation, its pane and panel on WhatsApp, Facebook and Instagram, with apostrophes in every name, message, saved reply, template, team and note, and finds no broken expression. It stays as a guard; the failing expression is needed from the console.
- **Verification:**
  - `test_inbox_tabs_stage4.py` (14), `test_no_full_reload.py` (19), `test_alpine_quotes.py` (3) and `test_stored_flow_errors.py` (3);
  - lint, badge and automation-list tests updated;
  - the app suites are green apart from the known `test_import_5000_contacts`;
  - Tailwind rebuilt (4.3.3 standalone binary).

### Entry 46: Premium pass D1–D3, plus the 11-08 follow-ups (2026-11-08)
Plan: the design audit of 2026-11-08 (D1 colour and buttons → D2 graphite dark → D3 type, then D5). The user chose a neutral primary, graphite, and D1 → D2 → D3 before the Dashboard; the public site is unchanged for now.

- **D1, colour and buttons** (`assets/tokens.css`, `assets/app.css`):
  - the primary button is solid neutral, `--color-primary`: #111318 with white text on light, #F4F4F5 with #0B0C10 text on dark;
  - secondary is a quiet ink fill with no border, `--color-secondary-*`; `btn-tonal` folds into it; ghost is text;
  - every button is 36px (32 small, 40 large), with an 8px radius and 150ms transitions; icon buttons match their row;
  - brand amber is now a highlight (`--color-highlight`): the active tab's underline (section tabs, Content Studio, tickets, `.tab-active`) and the setup progress bar;
  - the top bar's Send is a secondary button, so the page's own primary is the one primary;
  - the public pages (`templates/public/_base.html`, `data-surface="public"`) keep amber buttons; the landing page has its own stylesheet and is unchanged.
- **D2, graphite dark:**
  - canvas #0B0C10, surface #111318, wells #16181D, hover #1A1C21, borders #1F2126 / #33353C (about 6% and 12% white);
  - ink #F4F4F5 / #A1A1AA / #8B8B94;
  - this replaces the navy ink scale; `test_contrast` passes in both themes.
- **D3, type:**
  - section headings (`text-lg`) are 16px and titles (`text-3xl`) 28px;
  - `font-bold` renders 600, so the app uses three weights;
  - headings are set tight;
  - new `.metric` / `.metric-label` / `.metric-value`: a 12px uppercase label above a 28px tabular figure, with no box; Content Studio's `.kpi-*` match it;
  - public pages keep the previous sizes.
- **D6 (small):**
  - the Dashboard reads the same setup state as the top bar: with only a paused channel left it says "Setup is done for now", never "almost ready" at 4/4;
  - Meta's sample templates are no longer offered as campaign starting points.
- **11-08 follow-ups:**
  - the Alpine error was the closed-window template picker keying values by the variable's name ("Customer's first name"); fields are now keyed by position (`var__<id>__<n>`), the server also reads the old name-keyed form, and the Automations picker and campaign composer escape the name (`|escapejs`); there's a test with that variable;
  - Comments' Reply is a ghost button with an inline box, and post links are plain text with ↗;
  - "Next week" is the coming Monday at 9:00;
  - the top bar shows the workspace on every page (a `workspace_account` context processor; Comment rules didn't pass `account`);
  - `X-Akilent-Timing` (total, db, queries) goes out next to `Server-Timing`, which Cloudflare adds its own entries to;
  - Automations dropped from 36 to 14 queries (the readiness channel checks run once per page);
  - nginx `keepalive_timeout` 30s → 620s with `keepalive_requests 10000`, so Cloudflare reuses its connections to the origin.
- **Not done:**
  - the Inbox pill overlap: not found from the CSS, needs a screenshot;
  - HubSpot's "(Sample Contact)" rows: HubSpot's own demo data in the connected portal;
  - before/after screenshots of real pages: not taken.
- **Verification:** the full suite is green. Tailwind rebuilt (4.3.3 standalone); landing.css changed only in unused theme variables.

### Entry 47: D5 Dashboard, D4 Settings layout, D6 small fixes (2026-11-08)
Plan: the design audit's D5 (fewer boxes, Dashboard first), D4 (Settings side list and two-column sections, per the user's spec), and the D6 items folded in.

- **D5:**
  - **Dashboard:**
    - the work queue is four figures, not tiles: a label above a 28px number, no box or fill, and a small dot when there's work (amber yours, coral missed, teal leads);
    - the quick-action cards are a row of secondary buttons;
    - the panels are plain sections with 13px headings and hairline lists;
    - empty sections are one muted line with their action (`empty_state.html line=True`);
    - the email stats are figures;
    - the plan's usage bar is amber;
    - only the "Get more out of Akilent" recommendation stays a card.
  - **Shared styles:**
    - `.stat-card` is a figure everywhere it's used (no border, no icon tile);
    - `.panel-primary` has no lifted card.
  - **Content Studio's "Where your posts stand":** figures with labels above; Failed shows a coral figure and dot instead of a red box.
  - **The composer's Send:** the neutral primary, with a grey well when disabled; the mic is secondary.
- **D4, Settings:**
  - a grouped side list (Account: Profile, Security · Workspace: Business, Team, Opening hours, AI, Optional tools, Integrations · Developer: API keys, Activity log) replaces the 10-tab row;
  - at phone width it folds into "Settings · Profile";
  - every page has one width (max 64rem) and no width caps;
  - each settings group is a `.settings-section`: title and description on the left, fields on the right, hairlines between groups, stacking on phones;
  - converted by script: Profile, Business, Security, Opening hours, Optional tools, Team (invite), two-factor, passkeys and email codes; AI's head by hand;
  - the AI tools pages (report, knowledge base, playground) lose their card;
  - `.btn-danger-outline` is ready for a last "can't be undone" section.
- **D6:**
  - checkboxes are drawn: a hairline box, the primary's fill when checked, both themes;
  - the top bar's chip says "Waiting on a channel", matching the Dashboard;
  - the thread's day label is pinned at its top while scrolling, so it's never half cut off;
  - failed sends in a row with the same reason say it once ("5 messages not delivered ·" on the last), with a "Not sent" mark on each;
  - Customers' Filter button is no-JS only, since the filters already apply as you type.
- **Insights:** "1 1 customer is waiting" came from `_at_risk.html` printing each count before a sentence that already carries it. The other wording fixes are deployed, but stored insight titles are only rewritten by the nightly `generate_insights` run.
- **Not done:**
  - Inbox settings, Content Studio AI and Bio page, and Channels in the two-column sections;
  - the sticky "Unsaved changes" bar (pages keep their Save buttons);
  - the Insights redesign (proposed as the next D5 page).
- **Verification:** the full suite is green apart from `test_import_5000_contacts`. Tailwind rebuilt.
