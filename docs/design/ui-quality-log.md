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
