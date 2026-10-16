# Step 4.4: Per-Channel Result UI

**Objective:** Display each variant's publication result independently while showing parent post status.

**Acceptance Criteria (from user guidance):**
- ✅ Parent status and per-channel statuses shown separately
- ✅ Each channel shows its own publication result and error
- ✅ Published links displayed only when valid URLs exist
- ✅ Retry controls appear only for eligible variants
- ✅ Reconciliation and in-progress states distinguishable from failures
- ✅ UI handles legacy posts without variants
- ✅ Tests cover partial success, failed publication, successful publication

---

## Implementation Plan

### 1. Update `post_detail.html` Template

**New Section:** Per-Channel Publishing Status (after parent status display)

**Layout:**
```
Parent Status: [Published | Failed | Partial Success | Scheduled | Publishing]

Per-Channel Status:
├─ Facebook: [Published → ID + URL | Failed → Error | Scheduled | Publishing | Reconciling]
└─ Instagram: [Published → ID + URL | Failed → Error | Scheduled | Publishing | Reconciling]
```

**Key Design:**
- Show parent status first (context)
- List each channel separately
- Display distinct visual indicators for each state
- Preserve existing error/metrics display below

### 2. Add Template Helpers (filters/tags)

**Status Badge:**
```django
{{ variant|variant_status_badge }}  {# Returns: <span class="...">Published</span> #}
```

**Variant Card:**
```django
{% for variant in post.variants.all %}
  {% include "publishing/variant_result_card.html" %}
{% endfor %}
```

### 3. Create `variant_result_card.html` Partial

Displays:
- Platform name (bold)
- Status badge (color-coded)
- Publication URL (if successful)
- Error message (if failed)
- Last attempt time
- Retry button (if eligible)

### 4. Handle Legacy Posts (No Variants)

**Logic:**
```python
if post.variants.exists():
    # Show per-variant status
else:
    # Synthesize from post fields: fb_post_id, ig_media_id, error, status
    # Display as virtual variants for backward compat UI
```

---

## UI State Display

| Variant State | Icon | Color | Show URL? | Show Error? | Show Retry? |
|---|---|---|---|---|---|
| `PUBLISHED` | ✓ | Green | ✓ | — | — |
| `FAILED` | ✗ | Red | — | ✓ | ✓ |
| `SCHEDULED` | ⏱ | Gray | — | — | — |
| `PUBLISHING` | ⏳ | Amber | — | — | — |
| `RECONCILING` | ❓ | Yellow | — | ✓ (ambiguous) | Manual only |
| `CANCELLED` | ⊘ | Gray | — | — | — |

---

## Implementation Steps

1. Create `variant_result_card.html` partial
2. Add template filter `variant_status_badge`
3. Update `post_detail.html` to include per-variant section
4. Add helper method to synthesize legacy post variants
5. Write tests for partial success UI
6. Test with both variant-based and legacy posts

---

## Test Coverage

**Scenarios to verify:**
- [ ] Both channels published successfully → Show 2 green badges
- [ ] One published, one failed → Show green + red, parent says "PUBLISHING"
- [ ] One published, one reconciling → Show green + yellow, distinct states
- [ ] Both failed → Show 2 red badges, parent says "FAILED"
- [ ] Scheduled → Show gray badges
- [ ] Legacy post with only fb_post_id → Synthesize and display correctly
- [ ] Retry button appears only on FAILED variants
- [ ] Reconciliation variant shows no automatic retry option

---

## Files to Create/Modify

- `templates/publishing/post_detail.html` (update)
- `templates/publishing/variant_result_card.html` (create)
- `apps/publishing/templatetags/publishing_filters.py` (create)
- `apps/publishing/tests/test_post_detail_ui.py` (create)

---

**Status:** Ready for implementation  
**Blocking Issues:** IG video async regression (tracked separately)  
**Parallel Track:** Step 4.5 (variant-aware retry logic)
