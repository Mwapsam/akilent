# Akilent UI Specification

Concrete, buildable specification for the Akilent product redesign. This is the contract
every screen, component, and phase implements against. It supersedes ad-hoc decisions in
individual templates. Companion documents:

- [`docs/brand-colors.md`](../brand-colors.md) — canonical palette (unchanged; this spec
  consumes it).
- [`plan`](../../../.claude/plans/analyse-the-ui-how-effervescent-aurora.md) — phased roadmap.
- [`assets/app.css`](../../assets/app.css) — the single token + component source.

Rebuild after any token/template change:
`tools/tailwindcss.exe -i assets/app.css -o static/css/app.css --minify`

---

## 1. Product model

Akilent is a **communications platform**: businesses send, automate, schedule, and monitor
customer messages across **Email** and **WhatsApp** (SMS is a planned third channel). Every
screen serves one step of a single spine:

```
Create ─▶ Choose channel ─▶ Choose audience ─▶ Compose ─▶ Send / Schedule ─▶ Monitor
```

The five questions the UI must always answer, in order:

| Question | Where it is answered |
|---|---|
| What can I send? | `+ Send` action, Templates, Channels |
| Who am I sending to? | Contacts, AudienceSelector |
| When will it send? | ScheduleSelector, Scheduled page, Dashboard "Upcoming sends" |
| What happened? | Dashboard activity, Insights, Activity/Logs, message lifecycle |
| What should I do next? | Dashboard "Needs attention", onboarding checklist, empty states |

### Product hierarchy

```
Akilent
├── Product
│   ├── Send        Email · WhatsApp · [SMS later]  ──▶ Messages (campaigns, broadcasts)
│   ├── Automate    Workflows / rules               ──▶ automated Messages
│   └── Audience    Contacts, segments
│   ↓
│   Schedule / Send  (message lifecycle)
│   ↓
│   Analytics        Insights
│   ↓
│   Activity         Logs
└── Platform
    └── API          Domains, Mailboxes, keys, webhooks, docs
```

---

## 2. Message lifecycle & status vocabulary

One canonical, cross-channel lifecycle. Applies to campaigns now; later to individual
messages, broadcasts, and automated sends.

```
Draft ─▶ Scheduled ─▶ Sending ─▶ Sent
   │          │           │
   └──────────┴───────────┴────▶ Failed
                          │
                          └────▶ Paused   (automations / throttled sends)
```

| State | Meaning | `.badge` variant | `.status-indicator` dot | Icon (`components/icon.html`) |
|---|---|---|---|---|
| **Draft** | Not yet queued; editable | `badge-neutral` | `gray-400` | `pencil` |
| **Scheduled** | Queued for a future time | `badge-info` | `blue-400` | `clock` |
| **Sending** | Actively dispatching | `badge-warning` | `amber-400` (pulse) | `paper-plane` |
| **Sent** | Fully dispatched | `badge-success` | `teal-500` | `check` |
| **Failed** | Send errored / bounced hard | `badge-danger` | `coral-500` | `alert-triangle` |
| **Paused** | Halted, resumable | `badge-neutral` | `gray-500` | `pause` |

Rules:
- The label text is always the capitalised state name above — never synonyms
  ("queued", "in progress", "complete").
- Delivery outcomes (delivered / opened / clicked / bounced) are **metrics on a Sent
  message**, not lifecycle states. They render as counts/rates, not status badges.
- `.status-indicator` = dot + label, used inline in tables and headers.
- Only **Sending** animates (subtle 1.5s pulse); respects `prefers-reduced-motion`.

---

## 3. Visual signature

Bold and distinctly Akilent — **infrastructure-grade, not a generic marketing dashboard**.
Reference points from `docs/brand-colors.md`: Stripe, Linear, Vercel, Twilio.

| Principle | Implementation |
|---|---|
| Deep navy foundation | Sidebar, marketing hero, consoles, code blocks use `brand-950 #0E1526` / `gray-900`. Dark theme canvas = `brand-950`. |
| One accent, used sparingly | Amber `#FFB020` = one primary CTA per view. Everything else secondary/ghost. Teal = WhatsApp/success only. Coral = errors only. |
| Confident typography | Page titles `text-2xl sm:text-3xl font-semibold tracking-tight text-gray-900`. Section titles `text-lg font-semibold`. Body `text-sm text-gray-700`. One family: **Inter Variable** (self-hosted), `font-feature-settings: "cv11","ss01"`. |
| Clean light surfaces | Canvas `#F3F5F7` → white cards → muted wells `#FAFBFC`. |
| Hairline borders, not heavy outlines | `1px` `--color-border #E8EDF3`; elevation from layered shadow (`--shadow-card`), not thick strokes. |
| Dense but breathable | Card padding `p-5 sm:p-6`; section rhythm `space-y-6`; table rows `py-3`. Generous line-height on prose only. |
| Distinct channel indicators | Email = amber dot + `mail` icon; WhatsApp = teal dot + `message-circle` icon. A `.channel-tag` component renders `<icon> <label>` with the right color. Never rely on color alone — always icon + label. |
| Restrained motion | Transitions `150ms` on color/opacity, `200ms` on layout, single curve `--ease-emphasized`. No entrance animations on page load. No parallax. Everything off under `prefers-reduced-motion`. |
| Excellent empty / loading / error states | Every list and detail screen ships all three (§9). No raw "No results". |

Explicitly banned (from `docs/brand-colors.md`): purple / rainbow / blue-purple SaaS
gradients, neon accents, pure black, Tailwind stock grays, white text on amber or teal-500.

### Type scale (add to `@theme` as needed; names are Tailwind defaults)

| Token | Size / line-height | Use |
|---|---|---|
| `text-xs` | 12 / 16 | Badges, help text, table meta |
| `text-sm` | 14 / 20 | Body, table cells, form inputs, nav |
| `text-base` | 16 / 24 | Lead paragraphs, `.btn-lg` |
| `text-lg` | 18 / 28 | Card / section titles |
| `text-xl` | 20 / 28 | Sub-page titles |
| `text-2xl` | 24 / 32 | Page title (mobile) |
| `text-3xl` | 30 / 36 | Page title (`sm:` and up), dashboard greeting |

### Radii & elevation

| Element | Radius | Shadow |
|---|---|---|
| Button | `rounded-xl` (12px) | none |
| Input | `rounded-xl` | none; focus ring `2px brand-500` |
| Card | `rounded-2xl` (16px) | `--shadow-card` |
| Dropdown / popover | `rounded-2xl` | `--shadow-pop` |
| Modal / drawer | `rounded-2xl` (modal) / none (drawer) | `--shadow-modal` |
| Badge / pill | `rounded-full` | none |

---

## 4. Navigation — exact sidebar

Grouped, product-oriented. Replaces the flat list in
[`templates/components/_nav.html`](../../templates/components/_nav.html).

| Group label | Item | URL name (verify in Phase 2) | Icon |
|---|---|---|---|
| **OVERVIEW** | Dashboard | `dashboard` | `home` |
| **SEND** | Campaigns | `email:campaigns` | `send` |
| | Scheduled | `scheduled` | `clock` |
| | Templates | `email:templates` | `layout-template` |
| **AUTOMATE** | Automations | `automations` | `workflow` |
| **AUDIENCE** | Contacts | `contacts` | `users` |
| **CHANNELS** | Email | `email:domains` (channel overview) | `mail` |
| | WhatsApp *(if `WHATSAPP_ENABLED`)* | `whatsapp:numbers` | `message-circle` |
| **ANALYTICS** | Insights | `email:insights` | `bar-chart` |
| | Activity | `logs:messages` | `list` |
| **ACCOUNT** | Domains | `email:domains` | `globe` |
| | Mailboxes | `email:mailboxes` | `inbox` |
| | Billing | `billing:plans` | `credit-card` |
| | API | `docs:index` | `code` |
| | Settings | `settings-profile` | `settings` |
| **ADMIN** *(superuser)* | Customers | `manage:customers` | `building` |
| | Settings | `manage:settings` | `sliders` |

Rules:
- Every item has a **unique icon** — the current file reuses `inbox` for 3 items and
  `sparkles` for 3. Add missing glyphs to `components/icon.html`.
- "Email" under CHANNELS is a channel landing/overview (domains + sending health +
  recent email); the same domains list is also reachable under ACCOUNT. Reconcile the
  exact target in Phase 2 — one canonical URL, linked from both.
- Group labels: `px-3 pt-4 pb-1 text-[11px] font-semibold uppercase tracking-wider
  text-gray-400`. Hidden when the sidebar is collapsed (`x-show="!$store.ui.sidebarCollapsed"`).
- Collapsed sidebar (`lg:w-[4.75rem]`): icons only, group labels hidden, item label
  becomes a hover tooltip (`title` attr or `x-anchor` popover).
- Active state via a template tag, not string matching (§10).

### Command palette

`templates/components/command_palette.html`, included in `base.html` for authenticated
users. Alpine component.

- Trigger: `⌘K` / `Ctrl-K` (global `@keydown.window`), or click a topbar search stub.
- Content: every nav destination above + quick actions: **New campaign**, **Schedule a
  send**, **Add domain**, **Invite teammate**, **New template**.
- Client-side fuzzy filter over a static JSON list rendered from the same nav config
  (`apps/core` context or a JS constant). No new endpoint in v1.
- Uses the canonical modal overlay (`--z-modal`, `bg-gray-900/50`, `x-trap.noscroll`, Esc
  to close), but anchored top-center, `max-w-xl`.
- Keyboard: `↑`/`↓` move, `Enter` navigate, `Esc` close. Selected row `bg-brand-50`.

---

## 5. Product shell — exact topbar

[`templates/base.html`](../../templates/base.html) `<header>`, left → right:

```
[≡ (mobile)]  [Workspace ▾]        …flex-1…        [🔍 ⌘K]  [+ Send ▾]  [🔔]  [avatar ▾]
```

| Element | Spec |
|---|---|
| Mobile menu `≡` | `lg:hidden`, opens `$store.ui.drawerOpen`. Unchanged. |
| Workspace switcher | Replaces the static workspace badge. Dropdown: current account name + "Admin · all tenants" for staff; lists accounts the user can switch to; "Account settings" link. If only one workspace, render as a non-interactive label. |
| Search / `⌘K` stub | `hidden sm:flex` pill, `text-gray-400`, text "Search…" + `⌘K` kbd hint. Opens the command palette. |
| **`+ Send`** | `btn btn-primary btn-sm`. Opens the **channel chooser** (§6). This is the single most prominent action in the product — always visible, always amber. |
| Notifications `🔔` | `btn-ghost` icon button; dot when unread. v1 opens a simple dropdown of recent system events (domain verified, send finished, send failed). If no notification backend yet, ship the button disabled with a tooltip "Coming soon" — do **not** omit the slot. |
| User menu | Existing avatar dropdown + **theme toggle** row (Light / Dark / System), backed by `Alpine.store('theme')` (§8). Keep Profile, Team, Sign out. |

Topbar stays `sticky top-0 h-16 bg-white/90 backdrop-blur border-b border-border`,
`z-index: var(--z-sticky)`. Dark theme: `bg-brand-950/90 border-white/10`.

### Page header

New partial `templates/components/page_header.html`, used by every Phase 4 screen:

```django
{% include "components/page_header.html" with title="Campaigns"
   description="Create, schedule, and track your email and WhatsApp campaigns." %}
  {# actions slot via {% block page_actions %} or a named include arg #}
```

Layout: title (`text-2xl sm:text-3xl font-semibold tracking-tight`) + optional description
(`mt-1 text-sm text-gray-600`) on the left; actions (buttons) right-aligned, wrap below on
mobile. Bottom margin `mb-6`. Breadcrumbs (when nested) render above the title as
`text-xs text-gray-500` with `/` separators, in the `{% block breadcrumbs %}` slot.

---

## 6. `+ Send` channel chooser

Modal opened by the topbar `+ Send` button (and by "New campaign" / palette actions).

```
┌───────────────────────────────────────────────┐
│  What do you want to send?              [✕]    │
│                                               │
│  ┌───────────────┐   ┌───────────────┐         │
│  │  ✉  Email     │   │  ◉  WhatsApp  │         │
│  │  Send an      │   │  Send a       │         │
│  │  email        │   │  WhatsApp msg │         │
│  └───────────────┘   └───────────────┘         │
│         (SMS — greyed, "Coming soon")          │
│                                               │
│  ─────────────────────────────────────────    │
│  🕐  Schedule a message            →          │
└───────────────────────────────────────────────┘
```

- Two primary channel cards (`.card` + hover `border-brand-300`, large channel icon in the
  channel color). Email card → composer with `channel=email` preselected; WhatsApp card →
  `channel=whatsapp`. WhatsApp card is hidden entirely when `WHATSAPP_ENABLED` is false.
- SMS card rendered disabled with a "Coming soon" pill — signals the roadmap.
- "Schedule a message" is a secondary row → composer opened straight to the
  ScheduleSelector step (channel chosen inside).
- Reinforces that Email and WhatsApp are **channels of one product**, not separate apps.
- Canonical modal component (§7), `max-w-lg`.

---

## 7. Component inventory & tiers

Semantic classes in `assets/app.css` `@layer components` + Alpine partials in
`templates/components/`. **Screens compose these — never re-implement utility stacks.**
Every primitive gets a panel in `/styleguide/` (Phase 5).

### Tier 1 — build before touching product screens (Phase 3)

| Component | Form | Notes / current state |
|---|---|---|
| **Button** | `.btn` + `.btn-primary/-secondary/-ghost/-danger/-danger-ghost` + `.btn-sm/-lg/-block` | Exists. Add `.btn-icon` (square, icon-only) + loading state (`aria-busy`, spinner, disabled). |
| **Card** | `.card`, `.card-pad` | Exists. Add `.card-hover` (interactive cards). |
| **Input / Textarea / Select** | `.input`, `.input-error` | Exists. Add `.select` (native, chevron bg) and `.textarea` (min-height, resize-y). |
| **FormField** | `templates/components/form_field.html` | Exists. Add `templates/components/form.html` iterating a Django `Form` → `form_field.html`. |
| **Badge** | `.badge` + variants | Exists. Used for status vocabulary (§2). |
| **Table** | `.table-wrap`, `.table` | Exists. Add `.table--cards` responsive modifier (§9). |
| **Modal** | `templates/components/modal.html` (new) | **Canonical**: adopt `welcome_tour.html`'s pattern — `style="z-index: var(--z-modal)"`, backdrop `bg-gray-900/50`, `x-trap.noscroll`, `@keydown.escape`, `x-transition`. Slot-style include with `title` + body block + footer block. Migrate `workflow_list.html`, `email/_preview_modal.html`, `email/_api_code_modal.html` off their bespoke overlays. |
| **Dropdown** | `templates/components/dropdown.html` (new) | Extract the user-menu pattern from `base.html`: trigger + `x-show` panel, `@click.outside`, `@keydown.escape`, `role="menu"`, `z-index: var(--z-dropdown)`, `.card shadow-pop p-1.5`. |
| **Tabs** | `.tabs`, `.tab` (new) | Underline style, `box-shadow: inset 0 -2px 0 var(--color-brand-500)` on active. `role="tablist"`. |
| **EmptyState** | `templates/components/empty_state.html` | Exists. Standardise to icon + title + one-line message + one primary action (§9). |
| **Toast** | `templates/components/toast.html` + `window.toast()` | Exists. Keep. Verify variants map to `success/warning/danger/info`. |
| **StatusIndicator** | `.status-indicator` (new) + `templates/components/status.html` | Dot + label per §2. `status.html` takes `state="scheduled"`. |

### Tier 2 — build alongside Phase 4

| Component | Notes |
|---|---|
| **StatCard** | `templates/components/stat_card.html` exists — restyle: label (`text-xs uppercase tracking-wide text-gray-500`), value (`text-2xl font-semibold`), delta chip (teal ↑ / coral ↓), optional sparkline slot. |
| **Timeline** | `.timeline` — vertical rail + nodes; used on campaign detail for the lifecycle (§2) and on message detail for delivery events. |
| **Skeleton** | `.skeleton` exists. Add composed skeletons: `skeleton-table-row`, `skeleton-card`, `skeleton-stat`. |
| **Pagination** | `templates/components/pagination.html` — prev / next + page numbers, `.btn-sm btn-secondary`; Django `Paginator` aware. |
| **Drawer** | `templates/components/drawer.html` — right-side slide-over for detail/edit without leaving the list. Shares modal overlay mechanics; `z-index: var(--z-drawer)`. |
| **Avatar** | `.avatar` — the initials circle from `base.html`, sizes `avatar-sm/md/lg`, optional image. |
| **Tooltip** | `.tooltip` via `x-anchor` (alpine-focus already loaded? verify) or a tiny CSS `title` fallback. Used by collapsed sidebar. |

### Tier 3 — later

| Component | Notes |
|---|---|
| **DataTable** | Sort headers, row selection, bulk action bar, column visibility. Built on `.table`. |
| **CommandPalette polish** | Recent items, grouped results, action icons. |
| Specialized | Merge-tag input, audience segment builder, cron/schedule picker refinements. |

---

## 8. Dark mode

- Tokens: in `assets/app.css`, after `@theme`, add overrides:

  ```css
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { /* dark token values */ }
  }
  :root[data-theme="dark"] { /* same dark token values */ }
  ```

  Override only surface/border/text/shadow tokens — the color ramps stay. Dark values
  (from `docs/brand-colors.md` §6):

  | Token | Dark value |
  |---|---|
  | `--color-canvas` | `#0E1526` (brand-950) |
  | `--color-surface` | `#161F35` (gray-900 / ink-2) |
  | `--color-surface-muted` | `#1C2640` (ink-3) |
  | `--color-border` | `#26314A` (gray-800 / hairline) |
  | `--color-border-strong` | `#46516B` |
  | `--color-ink` | `#FAFBFC` |
  | secondary text | `gray-400 #A8B2C4` |
  | shadows | deepen alpha to ~0.4 |

- `base.html`: `<meta name="color-scheme" content="light dark">` (replace `content="light"`).
- `Alpine.store('theme')` in `static/js/app.js`, mirroring the `ui` store: `value` ∈
  `light | dark | system`, persisted to `localStorage` key `akilent:theme`, applied by
  setting/removing `data-theme` on `<html>`. Apply **before paint** via a tiny inline
  `<script>` in `<head>` to avoid a flash.
- Toggle lives in the user menu (segmented control: Light / Dark / System).
- Every component class must read tokens, never literal `bg-white` / `text-gray-900` where
  a surface/text token exists. Audit `.btn-secondary`, `.card`, `.input`, `.table`,
  `.nav-link`, topbar, sidebar.
- The marketing landing page is already dark by design — after token unification it simply
  uses the dark token set unconditionally (or `data-theme="dark"` on its root).

---

## 9. Empty / loading / error patterns

One recipe each. No screen ships without all three where applicable.

### Empty state (`components/empty_state.html`)

```
        [ icon in gray-300, 40px ]
        Title — "No campaigns yet"          text-base font-semibold text-gray-900
        One line — what this is / why empty  mt-1 text-sm text-gray-600
        [ + Primary action ]                 mt-4 .btn .btn-primary .btn-sm
```

Centered, `py-12`, inside the `.card` or `.table-wrap` that would have held content.
First-run empty states link to the onboarding step; filtered-to-nothing states offer
"Clear filters" instead.

### Loading state

- Full page / first load: skeletons that match the final layout
  (`skeleton-stat` row, `skeleton-table-row` ×5). Never a spinner for page content.
- In-place actions (button click, form submit): button enters loading state
  (`aria-busy="true"`, inline spinner, label unchanged, disabled).
- Background polling (DNS check, send progress): inline `.status-indicator` + subtle
  progress text, no blocking overlay.

### Error state

- Field errors: `.field-error` under the input, `.input-error` on the field, summary
  `.alert alert-danger` at the top of the form.
- Load failure (list/detail can't fetch): centered in the container — `alert-triangle`
  icon (coral), "Couldn't load X", one line, **Retry** button (`.btn-secondary btn-sm`).
- Destructive confirmations: `$store.confirm.ask()` (existing) — never a bare `window.confirm`.
- Toasts for transient outcomes only ("Campaign scheduled", "Send failed — check logs").

### `.alert` component (new, Tier 1-adjacent)

`.alert` + `.alert-info/-success/-warning/-danger` — tint bg + border + icon + text,
`rounded-xl p-4 text-sm`. Distinct from `.badge` (inline) and `toast` (transient).

---

## 10. Active-nav template tag

New `apps/core/templatetags/nav.py`:

```python
{% load nav %}
<a class="nav-link {% nav_active 'email:campaigns' %}" ...>
```

- `nav_active(context, *url_names, exact=False)` resolves each name with `reverse()`,
  compares against `request.path`; prefix match by default, exact match opt-in.
- Returns `"nav-link-active"` (+ sets a flag the template uses for `aria-current="page"`).
- Retires every `{% if '/x' in p %}` in `_nav.html`.
- Group partial `templates/components/_nav_group.html` renders a labelled section from a
  list of `(label, url_name, icon)` tuples so the nav config lives in one place
  (a context processor or the view).

---

## 11. Responsive behavior

| Breakpoint | Sidebar | Layout |
|---|---|---|
| `< md` (< 768) | Off-canvas drawer (`$store.ui.drawerOpen`), hamburger in topbar | Single column. Tables → `.table--cards`. Composer steps stack vertically, one visible at a time with a step bar. Page-header actions wrap under the title. |
| `md` (768–1023) | **Icon rail** (`md:w-[4.75rem]`), labels as tooltips | Content `md:pl-[4.75rem]`. 2-up card grids. Tables still `.table--cards` unless they fit. |
| `lg` (≥ 1024) | Full sidebar `lg:w-64`, collapsible to rail via `$store.ui.sidebarCollapsed` | Content `lg:pl-64` / `lg:pl-[4.75rem]`. 3–4-up grids. Full tables. Drawers/side-panels available. |

- `base.html` currently jumps `hidden lg:flex` → add the `md` rail tier.
- `main` gutter: `px-4 sm:px-6 lg:px-8`, `max-w-7xl mx-auto`, vertical `py-6 sm:py-8`.
- **`.table--cards`**: below `md`, each `<tr>` renders as a stacked `.card`; each `<td>`
  shows its column name via `td::before { content: attr(data-label) }` (templates add
  `data-label` per cell). Above `md`, normal table. Apply to `logs/messages.html`,
  `email/campaigns.html`, `contacts/list.html`, `automation/workflow_list.html` first.
- No horizontal page scroll at any width. Only code blocks / wide diagrams get their own
  `overflow-x-auto`.
- `prefers-reduced-motion`: move the handler into `@layer base` in `assets/app.css`
  (globally disable transitions/animations), remove the docs-layout-only copy.

---

## 12. Dashboard — exact layout

[`templates/accounts/dashboard.html`](../../templates/accounts/dashboard.html). Operational,
not statistical.

### Onboarding incomplete

```
Good morning, {{ first_name }} 👋
You're almost ready to send your first campaign.

┌ Get started ─────────────────────────────────────────────┐
│  ● Set up your account          ✓                         │
│  ● Add & verify your domain     ○   [ Verify domain → ]   │
│  ● Create your first template   ○                         │
│  ● Add contacts                 ○                         │
│  ● Send your first campaign     ○                         │
│  ● Connect WhatsApp             ○   (if WHATSAPP_ENABLED)  │
└──────────────────────────────────────────────────────────┘
```

Progress bar at the top of the card (`n / total`). Each incomplete row has a link to its
step; the first incomplete row shows a primary CTA. Data from
`apps.accounts.context_processors.onboarding_status` (already wired) — surface the same
signal in `onboarding_widget.html` (FAB) and here.

### Onboarding complete

```
Good {morning|afternoon|evening}, {{ first_name }} 👋
Here's what's happening with your communications.

┌ Messages sent ┐ ┌ Delivery rate ┐ ┌ Contacts ┐ ┌ Scheduled ┐   ← stat_card row
│  12,480       │ │  98.2%        │ │  3,204   │ │  3        │
│  ↑ 8% vs prev │ │  ↑ 0.4pt     │ │  +112    │ │  next 09:00│
└───────────────┘ └───────────────┘ └──────────┘ └───────────┘

┌ Upcoming sends ───────────────────┐  ┌ Needs attention ──────────┐
│ 🕐 Tomorrow 09:00  Newsletter  ✉ │  │ ⚠ Verify domain acme.com  │
│ 🕐 Tomorrow 14:30  Promotion   ◉ │  │ ⚠ WhatsApp not connected  │
│ 🕐 Friday   10:00  Reminder    ✉ │  │ ⚠ 82% of monthly email    │
│              [ View all → ]        │  │   quota used              │
└───────────────────────────────────┘  └───────────────────────────┘
       (2/3 width)                              (1/3 width)

┌ Recent activity ──────────────────────────────────────────┐
│ ✓ "October Newsletter" sent — 4,210 delivered   2h ago    │
│ ✓ Contact list "EU customers" imported (512)    5h ago    │
│ ✓ Domain acme.io verified                        1d ago   │
│                    [ View all activity → ]                 │
└──────────────────────────────────────────────────────────┘
```

- Greeting: `text-2xl sm:text-3xl font-semibold tracking-tight`, time-of-day aware.
- Stat row: 4× `stat_card` — `grid-cols-2 lg:grid-cols-4 gap-4`.
- "Upcoming sends" + "Needs attention": `grid lg:grid-cols-3 gap-6`; upcoming spans 2.
  Each upcoming row shows time, name, and a `.channel-tag`. Empty → empty state
  "Nothing scheduled — [Schedule a send]".
- "Needs attention": only renders rows that apply (unverified domain, WhatsApp not set up,
  quota ≥ 80%, failed sends in last 24h, expiring card). Each row links to the fix. If
  none: collapse the card or show a quiet "All clear ✓".
- "Recent activity": last ~6 events, icon + text + relative time. Links to Activity/Logs.

---

## 13. Onboarding

Redesign, do not rebuild — reuse `onboarding_status` context processor,
`components/onboarding_widget.html`, `components/welcome_tour.html`,
`accounts/onboarding.html`.

Canonical checklist (single source — the context processor):

```
1. Set up your account          (auto-complete on signup)   ✓
2. Add & verify your domain     → email:domains
3. Create your first template   → email:templates (new)
4. Add contacts                 → contacts (import)
5. Send your first campaign     → composer
6. Connect WhatsApp             → whatsapp:numbers   (only if WHATSAPP_ENABLED)
```

- **Dashboard card** (§12) is the primary surface.
- **FAB widget** (`onboarding_widget.html`): compact progress ring + expandable list;
  hidden once all steps complete; dismissible per session.
- **Welcome tour** (`welcome_tour.html`): first-login modal carousel, 3–4 slides
  (What Akilent does · Channels · Scheduling · Where to start), adapts copy to whether
  WhatsApp is enabled. Uses the canonical modal.
- Each step stores completion server-side; completing the last step fires a celebratory
  toast and reveals the full statistical dashboard.

---

## 14. Message composer architecture

The single flow behind `+ Send`, "New campaign", scheduled sends, and (later) WhatsApp
broadcasts. Extends the existing `static/js/campaign-wizard.js`.

```
MessageComposer                     (frame: step bar + body + footer nav)
├── ChannelSelector                 Email · WhatsApp · [SMS later]
├── TemplateSelector                pick / start blank / duplicate
├── AudienceSelector                contact lists, segments, filters, count preview
├── ContentEditor                   ← channel-specific:
│   ├── EmailContentEditor          subject, from-mailbox, HTML/blocks, preview, test send
│   └── WhatsAppContentEditor       template message, variables, preview bubble
├── ScheduleSelector                ◉ Send now   ◉ Schedule for [date/time/tz]
└── ReviewAndSend                   summary + lifecycle-aware CTA
```

- **Frame** = one component: horizontal step bar (`.tabs`-like, non-clickable ahead of
  progress), body slot, sticky footer (`Back` / `Continue` / final `Schedule` or `Send now`).
- **Channel-specific editors** plug into the same slot; the frame, TemplateSelector,
  AudienceSelector, and ScheduleSelector are shared. Adding **SMS** = one `ChannelSelector`
  entry + an `SmsContentEditor`; nothing else changes.
- Entry points preselect the channel and may deep-link to a step (e.g. "Schedule a
  message" opens at ScheduleSelector).
- On mobile: one step per screen, step bar becomes "Step 3 of 6" + progress.
- Output state maps to the lifecycle (§2): "Send now" → `Sending`; "Schedule" → `Scheduled`.
- Draft autosave via existing `static/js/draft-autosave.js`.
- v1 scope: fully wire **Email**; build the frame + shared steps so WhatsApp is a
  content-editor drop-in (backend wiring of WhatsApp send is follow-up, per plan).

---

## 15. Screen-by-screen (Phase 4 order)

For each: adopt `page_header.html`, the token/component layer, all three §9 states, and
`.table--cards` where a list is present.

1. **Dashboard** — §12 exactly.
2. **Onboarding** — §13.
3. **Campaigns** (`email/campaigns.html`, `campaign_detail.html`)
   - List: `.table--cards`; columns Name · Channel (`.channel-tag`) · Status
     (`.status-indicator`) · Audience · Sent/Scheduled time · metrics. Row → detail.
     Header action: `+ New campaign` (→ channel chooser). Filters: status, channel.
   - Detail: `page_header` with name + `.status-indicator` + sticky action bar
     (`Send now` / `Schedule` / `Duplicate` / `Archive` by state). Lifecycle `.timeline`.
     Metrics grid (delivered/opened/clicked/bounced) — only when `Sent`. Recipients table.
4. **Campaign composer** — §14, Email path complete.
5. **Scheduled** (`/scheduled/`)
   - First-class cross-channel list of everything in `Scheduled` state: time · name ·
     channel · audience · created-by. Group by day. Row actions: Edit, Reschedule,
     Send now, Cancel. Empty state → "Schedule a send".
6. **Contacts** (`contacts/list.html`, `contacts/detail.html`)
   - List `.table--cards`: name · email/phone · lists/segments · added. Bulk import CTA,
     search, segment filter. Detail: profile + membership + message history (lifecycle
     badges) + activity.
7. **Templates** (`email/templates.html`, `email/template_edit.html`)
   - Gallery of template cards (thumbnail, name, channel, updated). `+ New template`.
   - Editor: tighten `.split-gutter` layout, restyle `.device-frame` chrome, align the
     preview toolbar to the new `.btn` set, `.tabs` for HTML / Preview / Settings.
8. **WhatsApp** (`whatsapp/numbers.html`)
   - Channel overview: connected numbers, status, message templates. `+ Send` (WhatsApp
     preselected) routes through the composer frame. Shares status vocabulary + channel
     tag.
9. **Automations** (`automation/workflow_list.html`, `automation/workflow_editor.html`)
   - List: migrate off bespoke table/badge/modal → `.table--cards`, `.badge`,
     `.status-indicator` (Active/Paused/Draft), canonical modal for create.
   - Editor: keep the JSON-definition model; give steps a clearer visual hierarchy —
     indented vertical rail, typed step chips (Trigger / Wait / Branch / Send), the Send
     step opening the composer's content editor inline.
10. **Insights** (`email/insights.html`)
    - Responsive stat grid with `sm`/`md` steps; charts use the categorical palette from
      `docs/brand-colors.md` §10; shared status/channel vocabulary; date-range control.
11. **Activity / Logs** (`logs/messages.html`, `logs/message_detail.html`,
    `logs/requests.html`)
    - List `.table--cards`: time · channel · recipient · `.status-indicator` · subject.
      Filters: channel, status, date. Detail: `.timeline` of delivery events, raw
      payload in a `.code-tabs` block, "what next" links (resend, view contact).

---

## 16. Out of scope (this spec / plan)

- Visual node-graph canvas for the automation editor.
- JS bundler / build pipeline — staying with vendored Alpine + per-page vanilla JS.
- Server-backed command-palette search (v1 is client-side).
- SMS channel implementation — the composer is architected for it; not built now.
- End-to-end WhatsApp send backend parity.
- Notification backend — the topbar slot ships, wired if a backend exists, otherwise
  disabled-with-tooltip.

---

## 17. Acceptance checklist

- [ ] One font loads app-wide (no `fonts.googleapis.com` in Network); no inline `:root {`
      token blocks remain in `templates/`.
- [ ] Sidebar is grouped per §4; every item has a unique icon; active state comes from
      `{% nav_active %}`; collapsed rail shows tooltips.
- [ ] `md` icon-rail tier exists; drawer only `< md`; targeted lists reflow via
      `.table--cards`; no horizontal page scroll 360–1440px.
- [ ] `+ Send` opens the channel chooser; Email / WhatsApp / Schedule each enter the
      composer with the right channel/step.
- [ ] Campaign shows `Draft → Scheduled → Sending → Sent` identically on list, detail,
      Scheduled, and Dashboard (same badge + icon + label).
- [ ] Theme toggle (Light/Dark/System) persists, respects OS default pre-toggle, no flash;
      all surfaces/text use tokens.
- [ ] Every redesigned screen uses `.btn / .card / .badge / .table / .stat-card /
      .empty-state` — `rg 'bg-black/40|z-40' templates/` returns nothing in modal code.
- [ ] Every list/detail screen ships empty + loading + error states per §9.
- [ ] `/styleguide/` renders every Tier 1–2 primitive and all tokens.
- [ ] Keyboard-only traversal of sidebar, command palette, and one migrated modal works;
      visible focus ring everywhere; `prefers-reduced-motion` disables all motion globally.
- [ ] `tools/tailwindcss.exe -i assets/app.css -o static/css/app.css --minify` clean;
      `poetry run python manage.py collectstatic --noinput` clean.
