# Akilent Content Studio Design System

## Overview

This document extends the Akilent design system (`/docs/design/akilent-ui-spec.md`) with components and patterns specific to content publishing, approval workflows, multi-channel management, and performance measurement.

All typography, colors, spacing, and component foundations are inherited from the main Akilent design system. This guide defines additions and clarifications for the Content Studio workflows.

**Reference:**
- Main design spec: `/docs/design/akilent-ui-spec.md`
- Live styleguide: `/templates/manage/styleguide.html`
- Tailwind config: `/assets/app.css`

---

## 1. Status & State Indicators

### Content Status Lifecycle

Visual states for content as it moves through the approval and publishing pipeline.

**Status display component** (extends existing `.status-indicator`):

```html
<!-- Compact status dot + label, read-only -->
<div class="status-indicator" data-state="draft">
  <div class="status-dot"></div>
  <span class="status-label">Draft</span>
</div>

<!-- With timestamp, for publishing results -->
<div class="status-with-time">
  <div class="status-indicator" data-state="published">
    <div class="status-dot"></div>
    <span class="status-label">Published</span>
  </div>
  <span class="text-xs text-muted">Oct 15, 2:30 PM</span>
</div>
```

**CSS (add to `/assets/app.css`):**

```css
.status-dot {
  @apply w-2.5 h-2.5 rounded-full;
}

/* Draft: gray */
[data-state="draft"] .status-dot { @apply bg-gray-400; }
[data-state="draft"] .status-label { @apply text-gray-700 dark:text-gray-300; }

/* In review: blue (pending human action) */
[data-state="in-review"] .status-dot { @apply bg-blue-400; }
[data-state="in-review"] .status-label { @apply text-blue-700 dark:text-blue-300; }

/* Changes requested: amber (blocker, needs creator action) */
[data-state="changes-requested"] .status-dot { @apply bg-amber-400; }
[data-state="changes-requested"] .status-label { @apply text-amber-700 dark:text-amber-300; }

/* Approved: teal (ready to schedule/publish) */
[data-state="approved"] .status-dot { @apply bg-teal-500; }
[data-state="approved"] .status-label { @apply text-teal-700 dark:text-teal-300; }

/* Scheduled: blue (waiting for scheduled time) */
[data-state="scheduled"] .status-dot { @apply bg-blue-500; }
[data-state="scheduled"] .status-label { @apply text-blue-700 dark:text-blue-300; }

/* Publishing: amber with animation (in progress) */
[data-state="publishing"] .status-dot { @apply bg-amber-400 animate-pulse; }
[data-state="publishing"] .status-label { @apply text-amber-700 dark:text-amber-300; }

/* Published: teal (success) */
[data-state="published"] .status-dot { @apply bg-teal-500; }
[data-state="published"] .status-label { @apply text-teal-700 dark:text-teal-300; }

/* Failed: coral (error, action needed) */
[data-state="failed"] .status-dot { @apply bg-coral-500; }
[data-state="failed"] .status-label { @apply text-coral-700 dark:text-coral-300; }

/* Cancelled: gray (terminal state, not blocked) */
[data-state="cancelled"] .status-dot { @apply bg-gray-500; }
[data-state="cancelled"] .status-label { @apply text-gray-700 dark:text-gray-300; }
```

**Channel status** (per-platform publication result):

```html
<!-- Status for one channel/platform -->
<div class="channel-status">
  <div class="channel-status-header">
    <span class="channel-icon">📘</span>
    <span class="channel-label">Facebook</span>
  </div>
  <div class="channel-status-body">
    <div class="status-indicator" data-state="published"></div>
    <span class="text-sm text-muted">Published 2 hours ago</span>
  </div>
</div>

<!-- Partial success: one platform published, one failed -->
<div class="status-group">
  <div class="channel-status" data-outcome="success">
    <span class="channel-icon">📘</span>
    <span class="channel-label">Facebook</span>
    <div class="status-indicator" data-state="published"></div>
  </div>
  <div class="channel-status" data-outcome="failed">
    <span class="channel-icon">📸</span>
    <span class="channel-label">Instagram</span>
    <div class="status-indicator" data-state="failed"></div>
    <span class="text-xs text-coral-600">Media validation failed</span>
  </div>
</div>
```

---

## 2. Approval & Team Workflow Components

### Reviewer Assignment

```html
<!-- Assign reviewer to post (dropdown) -->
<div class="field">
  <label class="label">Assign for review</label>
  <select class="select" name="reviewer_id">
    <option value="">Unassigned</option>
    <option value="123">Sarah Chen (Marketing Lead)</option>
    <option value="456">Marcus Rodriguez (Brand)</option>
  </select>
  <p class="help">Post will move to "In Review" when submitted</p>
</div>

<!-- Reviewer status (read-only, shows who reviewed and when) -->
<div class="approval-info">
  <div class="approval-row">
    <span class="approval-label">Assigned to</span>
    <span class="approval-value">Sarah Chen</span>
    <span class="text-xs text-muted">2 hours ago</span>
  </div>
  <div class="approval-row">
    <span class="approval-label">Last reviewed</span>
    <span class="approval-value">Sarah Chen</span>
    <span class="text-xs text-muted">1 hour ago</span>
  </div>
</div>
```

### Feedback & Comments

```html
<!-- Review feedback (inline, not a separate tab) -->
<div class="feedback-panel">
  <div class="feedback-header">
    <span class="font-semibold text-sm">Changes requested</span>
    <span class="text-xs text-muted">Oct 15, 1:30 PM</span>
  </div>
  <div class="feedback-body">
    <p class="text-sm">Please use the brand voice more here. The CTA feels generic.</p>
  </div>
  <div class="feedback-actions">
    <button class="btn btn-sm btn-primary">Acknowledge and edit</button>
  </div>
</div>

<!-- Approval history (timeline) -->
<div class="approval-history">
  <div class="history-item">
    <div class="history-marker" data-state="approved"></div>
    <div class="history-content">
      <p class="text-sm font-medium">Approved by Sarah Chen</p>
      <p class="text-xs text-muted">Oct 15, 2:00 PM</p>
    </div>
  </div>
  <div class="history-item">
    <div class="history-marker" data-state="changes-requested"></div>
    <div class="history-content">
      <p class="text-sm font-medium">Changes requested by Sarah Chen</p>
      <p class="text-sm text-gray-700">Use brand voice in CTA</p>
      <p class="text-xs text-muted">Oct 15, 1:30 PM</p>
    </div>
  </div>
</div>
```

---

## 3. Multi-Channel & Per-Channel Components

### Channel Selector (Composer)

```html
<!-- Select target channels when creating post -->
<fieldset class="channel-selector">
  <legend class="label mb-3">Publish to</legend>
  <div class="channel-options">
    <label class="channel-option">
      <input type="checkbox" name="channels" value="facebook" checked />
      <span class="channel-option-icon">📘</span>
      <span class="channel-option-label">Facebook</span>
    </label>
    <label class="channel-option">
      <input type="checkbox" name="channels" value="instagram" checked />
      <span class="channel-option-icon">📸</span>
      <span class="channel-option-label">Instagram</span>
    </label>
  </div>
</fieldset>

/* CSS */
.channel-option {
  @apply flex items-center gap-3 p-3 border rounded-lg cursor-pointer transition;
  @apply border-gray-300 dark:border-gray-600;
}
.channel-option input:checked + span + span {
  @apply font-semibold text-brand-700;
}
.channel-option input:checked {
  @apply accent-brand-600;
}
```

### Channel-Specific Variant Indicator

```html
<!-- In composer, show which channel's variant is being edited -->
<div class="channel-variant-header">
  <span class="channel-icon">📘</span>
  <span class="channel-label">Facebook</span>
  <span class="variant-note text-xs text-muted">(1200 x 628 px recommended)</span>
</div>

<!-- Toggle between channel variants (tabs) -->
<div class="channel-tabs">
  <button class="channel-tab" data-channel="facebook" aria-selected="true">
    <span class="channel-icon">📘</span>
    <span class="channel-label">Facebook</span>
  </button>
  <button class="channel-tab" data-channel="instagram">
    <span class="channel-icon">📸</span>
    <span class="channel-label">Instagram</span>
  </button>
</div>
```

### Platform-Specific Warnings

```html
<!-- Warn when content won't work on a platform -->
<div class="alert alert-warning" role="alert">
  <svg class="alert-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
    <path d="M10 3.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM8.25 8.75a.75.75 0 11-1.5 0 .75.75 0 011.5 0zM9 11a.75.75 0 100-1.5A.75.75 0 009 11z" />
  </svg>
  <div>
    <p class="font-semibold text-sm">Reel not supported on Facebook</p>
    <p class="text-sm text-gray-700">Consider using a video post or static image instead.</p>
  </div>
</div>
```

---

## 4. Brief Context Panel

### Brief Display in Composer

```html
<!-- Show brief context while editing post (sticky panel or sidebar) -->
<aside class="brief-context">
  <div class="brief-header">
    <h3 class="brief-title">Campaign Brief</h3>
    <a href="#" class="link link-sm">View full brief</a>
  </div>
  <div class="brief-body">
    <div class="brief-field">
      <span class="brief-label">Campaign</span>
      <span class="brief-value">Q4 Product Launch</span>
    </div>
    <div class="brief-field">
      <span class="brief-label">Objective</span>
      <span class="brief-value">Drive pre-orders for new collection</span>
    </div>
    <div class="brief-field">
      <span class="brief-label">Target Audience</span>
      <span class="brief-value">Existing customers, age 25-45</span>
    </div>
    <div class="brief-field">
      <span class="brief-label">CTA</span>
      <span class="brief-value">Pre-order now for early access</span>
    </div>
  </div>
</aside>

/* CSS */
.brief-context {
  @apply bg-surface border border-gray-200 dark:border-gray-700 rounded-xl p-4;
}

.brief-field {
  @apply mb-3;
}

.brief-label {
  @apply block text-xs font-semibold uppercase tracking-wide text-muted mb-1;
}

.brief-value {
  @apply block text-sm text-ink;
}
```

---

## 5. Performance Metrics & Data States

### Metric Availability States

```html
<!-- Post performance showing different data states -->
<div class="metric-group">
  <div class="metric-card" data-state="available">
    <span class="metric-label">Reach</span>
    <span class="metric-value">2,847</span>
    <span class="metric-source">Last updated 2 hours ago</span>
  </div>
  
  <div class="metric-card" data-state="pending">
    <span class="metric-label">Engagement</span>
    <svg class="metric-spinner" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none" opacity="0.2" />
      <path d="M2 12a10 10 0 1 0 20 0a10 10 0 0 0-20 0" stroke="currentColor" stroke-width="2" fill="none" stroke-dasharray="15" opacity="0.8" />
    </svg>
    <span class="metric-note text-xs text-muted">Collecting data...</span>
  </div>

  <div class="metric-card" data-state="unsupported">
    <span class="metric-label">Video Views (IG)</span>
    <span class="metric-value">—</span>
    <span class="metric-note text-xs text-coral-600">Unsupported for this media type</span>
  </div>

  <div class="metric-card" data-state="stale">
    <span class="metric-label">Impressions</span>
    <span class="metric-value">5,102</span>
    <span class="metric-note text-xs text-amber-600">Data is 4 hours old</span>
  </div>

  <div class="metric-card" data-state="failed">
    <span class="metric-label">Saves</span>
    <svg class="metric-icon-error" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
      <path d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" />
    </svg>
    <span class="metric-note text-xs text-coral-600">Could not fetch (check permissions)</span>
  </div>
</div>

/* CSS */
.metric-card {
  @apply p-4 rounded-lg border bg-surface text-center;
}

[data-state="available"] {
  @apply border-teal-200 dark:border-teal-800 bg-teal-50 dark:bg-teal-900/20;
}

[data-state="pending"] {
  @apply border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20;
}

[data-state="unsupported"],
[data-state="failed"] {
  @apply border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-800 text-gray-600 dark:text-gray-400;
}

[data-state="stale"] {
  @apply border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20;
}

.metric-value {
  @apply block text-2xl font-bold text-ink my-1;
}

.metric-source {
  @apply block text-xs text-muted mt-2;
}
```

---

## 6. Calendar Components

### Calendar Status Legend

```html
<!-- Show what each color means (reference for calendar view) -->
<div class="status-legend">
  <div class="legend-item">
    <div class="legend-color" data-state="draft"></div>
    <span>Draft</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" data-state="in-review"></div>
    <span>In Review</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" data-state="approved"></div>
    <span>Approved</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" data-state="scheduled"></div>
    <span>Scheduled</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" data-state="published"></div>
    <span>Published</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" data-state="failed"></div>
    <span>Failed</span>
  </div>
</div>

/* CSS */
.status-legend {
  @apply flex flex-wrap gap-4 text-sm;
}

.legend-item {
  @apply flex items-center gap-2;
}

.legend-color {
  @apply w-3 h-3 rounded-full;
}
```

### Calendar Content Card

```html
<!-- Post preview on calendar grid/list -->
<div class="calendar-item" data-status="scheduled">
  <div class="calendar-item-header">
    <h4 class="calendar-item-title">Q4 Launch: Early Bird Offer</h4>
    <div class="status-badge" data-state="scheduled">
      <div class="status-dot"></div>
      <span>Scheduled</span>
    </div>
  </div>
  <p class="calendar-item-preview">Get early access to the new collection...</p>
  <div class="calendar-item-meta">
    <div class="calendar-meta-row">
      <span class="text-xs text-muted">Oct 17 at 2:00 PM</span>
    </div>
    <div class="calendar-channel-row">
      <span class="channel-indicator">📘</span>
      <span class="channel-indicator">📸</span>
    </div>
  </div>
</div>

/* CSS */
.calendar-item {
  @apply p-4 border rounded-lg cursor-pointer transition;
  @apply bg-surface border-gray-200 dark:border-gray-700;
}

[data-status="draft"] { @apply border-l-4 border-l-gray-400; }
[data-status="in-review"] { @apply border-l-4 border-l-blue-400; }
[data-status="scheduled"] { @apply border-l-4 border-l-blue-500; }
[data-status="published"] { @apply border-l-4 border-l-teal-500; }
[data-status="failed"] { @apply border-l-4 border-l-coral-500; }

.calendar-item:hover {
  @apply shadow-pop;
}
```

---

## 7. Error States & Recovery

### Recoverable Error Display

```html
<!-- Post failed to publish; show what went wrong and how to fix -->
<div class="alert alert-danger" role="alert">
  <svg class="alert-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
    <path d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" />
  </svg>
  <div class="alert-content">
    <p class="font-semibold text-sm">Failed to publish to Instagram</p>
    <p class="text-sm text-gray-700 dark:text-gray-300">Reason: Image aspect ratio must be between 0.8:1 and 1.91:1</p>
    <div class="alert-actions mt-3">
      <button class="btn btn-sm btn-danger">Fix and retry</button>
      <button class="btn btn-sm btn-ghost">View error details</button>
    </div>
  </div>
</div>

/* Error classified by type */
[data-error-type="validation"] {
  /* Blue: requires user action to fix field */
  @apply border-l-blue-500 bg-blue-50 dark:bg-blue-900/20;
}

[data-error-type="permission"] {
  /* Coral: requires account re-connection or permissions update */
  @apply border-l-coral-500 bg-coral-50 dark:bg-coral-900/20;
}

[data-error-type="rate-limit"] {
  /* Amber: temporary, can retry after delay */
  @apply border-l-amber-500 bg-amber-50 dark:bg-amber-900/20;
}

[data-error-type="provider-outage"] {
  /* Gray: external issue, try again later */
  @apply border-l-gray-500 bg-gray-50 dark:bg-gray-100;
}
```

---

## 8. Responsive Adaptations

### Composer Layout (Desktop vs. Mobile)

**Desktop (lg+):**
- Three-column: Brief context (sidebar) | Compose area (main) | Preview (sidebar)
- All visible at once; brief is sticky or scrolls with content

**Tablet (sm–lg):**
- Two-column: Compose area (full) | Preview (full)
- Brief accessible via collapse/expand or swipe

**Mobile (phone):**
- Single column: Compose area full width
- Brief and preview accessible via tabs or accordion

---

## 9. Validation & Feedback

### Form Field Validation

```html
<!-- Field with error (inline, beside input) -->
<div class="field">
  <label class="label" for="caption">Caption</label>
  <textarea class="textarea" id="caption" name="caption"></textarea>
  <p class="field-error">Caption must be at least 10 characters</p>
</div>

<!-- Field with success -->
<div class="field">
  <label class="label" for="title">Title</label>
  <input class="input" id="title" name="title" value="Q4 Launch" />
  <p class="field-success text-xs text-teal-600">Title is good to go</p>
</div>

/* CSS */
.field-success {
  @apply text-xs mt-1 text-teal-600 dark:text-teal-400;
}
```

### Unsaved Changes

```html
<!-- Indicator that draft has unsaved changes -->
<div class="unsaved-indicator">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
    <path d="M10.5 1.5H4a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2V7.5M17 3m-6 0a2 2 0 11-4 0 2 2 0 014 0z" />
  </svg>
  <span class="text-sm text-amber-600">Unsaved changes</span>
</div>

/* Auto-save confirmation */
.autosave-indicator {
  @apply text-xs text-teal-600 opacity-0 transition-opacity;
  /* Show for 3 seconds after save, then fade */
}
.autosave-indicator.show {
  @apply opacity-100;
}
```

---

## 10. Typography & Microcopy

### Headers & Headings

- **Page title** (e.g., "Content Calendar"): `text-3xl font-bold` on desktop, `text-2xl` on mobile
- **Section heading** (e.g., "Approval History"): `text-lg font-semibold`
- **Field label**: `label` class (small, semibold, uppercase tracking)
- **Error message**: `field-error` class (xs, coral color)
- **Help text**: `help` class (xs, muted color)

### Tone

- **Actionable**: "Please re-enter your caption" (not "Caption invalid")
- **Clear**: "Post published at 2:30 PM on Oct 15" (not "published")
- **Honest**: "Unsupported for this media type" (not "N/A" or "—")
- **Encouraging**: "You're ready to schedule!" (not "OK to schedule")

---

## 11. Animation & Transitions

- **Status transitions**: 200ms ease, color-change only (no movement)
- **Feedback animations**: 100ms fade-in/out for messages
- **Loading spinners**: Only in metric cards or data-loading areas (never spinners on main page)
- **Hover states**: Subtle shadow or background color shift (no scale/translate)

---

## Summary of New/Extended Components

| Component | Usage | Status |
|-----------|-------|--------|
| `.status-dot`, `.status-indicator` | Post/draft status (draft, in-review, approved, scheduled, published, failed) | Extend existing |
| `.channel-status` | Per-platform publication outcome | New |
| `.channel-selector`, `.channel-tabs` | Select channels or switch variants | New |
| `.brief-context` | Display brief while composing | New |
| `.metric-card` | Performance metrics with state (available, pending, unsupported, stale, failed) | New |
| `.approval-info`, `.approval-history` | Team review workflow | New |
| `.feedback-panel` | Reviewer comments | New |
| `.calendar-item`, `.status-legend` | Calendar/schedule display | Extend existing |
| `.alert[data-error-type]` | Error classification (validation, permission, rate-limit, outage) | Extend existing |
| `.unsaved-indicator` | Unsaved draft state | New |

---

## Next Steps

1. Add CSS for new components to `/assets/app.css` under a `/* Content Studio Components */` section
2. Create template components in `/templates/components/` (status.html, channel-status.html, metric-card.html, etc.)
3. Integrate into publishing views: `post_new.html`, `calendar.html`, `post_detail.html`
4. Test responsive behavior at mobile, tablet, and desktop widths
5. Validate accessibility (focus states, labels, color contrast)
