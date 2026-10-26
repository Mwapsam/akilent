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
