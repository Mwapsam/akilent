# Akilent Content Studio Wireframes

Stage 1 wireframes for the critical journey: Calendar → Plan/Brief → Composer → Publish Result.

Each screen shows desktop (left) and mobile (right) layouts. Components reference the design system at `docs/design/content-studio-design-system.md`.

---

## Screen 1: Calendar (Main Publishing Center)

### Desktop Layout (lg: 1024px+)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Content Studio                                    📅 Calendar        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ← October 2026 →  [ View: Week ] [ View: Month ]                   │
│                                                                       │
│  Filters: [Channel ▼] [Campaign ▼] [Pillar ▼] [Owner ▼] [Status ▼] │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ MON 14      TUE 15      WED 16      THU 17      FRI 18        │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │                                                                 │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │  │
│  │  │Q4 Launch │  │Offer     │  │Email: NL │  │Black Fri │       │  │
│  │  │◆ Draft   │  │◆ Schedul.│  │◆ Drafts  │  │◆ Publish │       │  │
│  │  │📘 📸     │  │📘 📸 📧  │  │📧       │  │📘 📸    │       │  │
│  │  │Oct 15    │  │Oct 17    │  │Oct 17    │  │Oct 18   │       │  │
│  │  │2:00 PM   │  │10:00 AM  │  │9:00 AM   │  │9:00 AM  │       │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │  │
│  │                                                                 │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │  │
│  │  │Giveaway  │  │          │  │          │  │          │       │  │
│  │  │◆ Failed  │  │          │  │          │  │          │       │  │
│  │  │📸        │  │          │  │          │  │          │       │  │
│  │  │Oct 14    │  │          │  │          │  │          │       │  │
│  │  │(Retry)   │  │          │  │          │  │          │       │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │  │
│  │                                                                 │  │
│  │  [+ Create post]                                               │  │
│  │                                                                 │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Legend: ◆ = status dot (gray=draft, blue=review, teal=approved)    │
│          📘 📸 = channels (Facebook, Instagram, WhatsApp, Email)    │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘

Status Indicator Key:
  ◆ Draft (gray)
  ◆ In Review (blue)
  ◆ Changes Requested (amber)
  ◆ Approved (teal)
  ◆ Scheduled (blue)
  ◆ Publishing (amber, animated)
  ◆ Published (teal)
  ◆ Failed (coral)
```

### Mobile Layout (phone: <640px)

```
┌──────────────────────────┐
│ < Calendar              │
│ 📅 October              │
├──────────────────────────┤
│ [Week] [Month]           │
│                          │
│ [Channel ▼] [Status ▼]  │
│                          │
│ MON 14                   │
│ ┌────────────────────┐   │
│ │Q4 Launch           │   │
│ │◆ Draft             │   │
│ │📘 📸               │   │
│ │2:00 PM             │   │
│ └────────────────────┘   │
│                          │
│ ┌────────────────────┐   │
│ │Giveaway            │   │
│ │◆ Failed (Retry)    │   │
│ │📸                  │   │
│ │Oct 14              │   │
│ └────────────────────┘   │
│                          │
│ TUE 15                   │
│ ┌────────────────────┐   │
│ │Offer               │   │
│ │◆ Scheduled         │   │
│ │📘 📸 📧            │   │
│ │10:00 AM            │   │
│ └────────────────────┘   │
│                          │
│ [+ Create post]          │
│                          │
└──────────────────────────┘
```

---

## Screen 2: Plan Item & Content Brief

### Desktop Layout (lg: 1024px+)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Q4 Launch Campaign                                [Edit] [Delete]   │
├─────────────────────────────────────────────────────────────────────┤
│ < Back to Calendar                                                  │
│                                                                       │
│  Campaign           │ Q4 Launch: Early Access                        │
│  Pillar             │ Product Launch                                 │
│  Objective          │ Drive pre-orders for new collection            │
│  Target Channels    │ 📘 Facebook    📸 Instagram    📧 Email        │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ BRIEF DETAILS                                               │    │
│  ├─────────────────────────────────────────────────────────────┤    │
│  │                                                              │    │
│  │  Target Audience                                             │    │
│  │  Existing customers, age 25-45, high purchase history       │    │
│  │                                                              │    │
│  │  Problem They Have                                           │    │
│  │  Missing out on exclusive early access to new collection    │    │
│  │                                                              │    │
│  │  What We're Offering                                         │    │
│  │  24-hour early access, 10% discount, free shipping          │    │
│  │                                                              │    │
│  │  Message / Hook                                              │    │
│  │  "Be the first to own [collection name]. Early access       │    │
│  │  starts tomorrow at noon."                                   │    │
│  │                                                              │    │
│  │  Call-to-Action                                              │    │
│  │  "Pre-order now for early access"                            │    │
│  │                                                              │    │
│  │  Desired Formats                                              │    │
│  │  ☑ Facebook: Feed post      ☑ Instagram: Reel               │    │
│  │                                                              │    │
│  │  Required Assets / Notes                                      │    │
│  │  - Hero image of new collection                              │    │
│  │  - 15-second demo video (for IG Reel)                        │    │
│  │                                                              │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  Owner           │ Sarah Chen                                        │
│  Reviewer        │ Marcus Rodriguez                                  │
│  Deadline        │ Oct 20, 5:00 PM                                  │
│  Status          │ ◆ Planned                                         │
│                                                                       │
│  [Create post from brief] [View linked posts]                       │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

### Mobile Layout (phone: <640px)

```
┌──────────────────────────┐
│ < Back      Q4 Launch    │
├──────────────────────────┤
│                          │
│ Campaign                 │
│ Q4 Launch: Early Access  │
│                          │
│ Pillar                   │
│ Product Launch           │
│                          │
│ Objective                │
│ Drive pre-orders...      │
│                          │
│ Channels                 │
│ 📘 🌐   📸 🌐            │
│                          │
│ ─────────────────────    │
│ BRIEF DETAILS            │
│ ─────────────────────    │
│                          │
│ Audience                 │
│ Existing customers,      │
│ age 25–45                │
│                          │
│ Problem                  │
│ Missing early access     │
│                          │
│ Offer                    │
│ Early access + 10% off   │
│                          │
│ Message                  │
│ Be the first to own...   │
│                          │
│ CTA                      │
│ Pre-order now for        │
│ early access             │
│                          │
│ ─────────────────────    │
│ Owner      Sarah Chen    │
│ Reviewer   Marcus R.     │
│ Deadline   Oct 20, 5 PM  │
│ Status     ◆ Planned     │
│                          │
│ [Create post]            │
│ [View linked posts]      │
│                          │
└──────────────────────────┘
```

---

## Screen 3: Content Composer

### Desktop Layout (lg: 1024px+)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Create Post: Q4 Launch Campaign                        [Save] [Exit] │
├──────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ┌───────────────────────────┐ ┌──────────────────┐ ┌──────────────┐ │
│  │ BRIEF CONTEXT             │ │ COMPOSER         │ │ PLATFORM:    │ │
│  ├───────────────────────────┤ ├──────────────────┤ │ 📘 Facebook  │ │
│  │ Campaign                  │ │ [📘] [📸] [📧]  │ │              │ │
│  │ Q4 Launch                 │ │                  │ │ Format       │ │
│  │                           │ │ Caption *        │ │ Feed Post    │ │
│  │ Audience                  │ │ ┌──────────────┐ │ │ (1200×628)   │ │
│  │ Existing customers        │ │ │Be the first  │ │ │              │ │
│  │ 25-45                     │ │ │to own new    │ │ │ Media        │ │
│  │                           │ │ │collection... │ │ │ ┌──────────┐ │ │
│  │ Problem                   │ │ │              │ │ │ │ (Click   │ │ │
│  │ Missing early access      │ │ └──────────────┘ │ │ │  to add) │ │ │
│  │                           │ │ ⚠️ Validation:   │ │ │ └──────────┘ │ │
│  │ Offer                     │ │ Caption must be  │ │ │              │ │
│  │ 24-hr early access        │ │ ≥ 10 chars       │ │ │ Hashtags     │ │
│  │ 10% discount              │ │ ✓ Media added    │ │ │ #q4launch    │ │
│  │                           │ │                  │ │ │ #newcollection
│  │ Message                   │ │ AI Suggestions   │ │ │              │ │
│  │ "Be the first to own..."  │ │ ✨ Hook variant: │ │ │ Link         │ │
│  │                           │ │ "Join VIPs now"  │ │ │ ─────────────│ │
│  │ CTA                       │ │ [Use] [Regen]    │ │ │ Preview      │ │
│  │ Pre-order now             │ │                  │ │ └──────────────┘ │
│  │                           │ │ [Save draft]     │ │                  │
│  │ [View full brief] ↗       │ │ [Request review] │ │ [Schedule] [Pub] │
│  │                           │ │ [Schedule]       │ │                  │
│  └───────────────────────────┘ └──────────────────┘ └──────────────┘  │
│                                                                        │
│  Status: Unsaved changes · Last auto-saved 2 min ago                  │
│                                                                        │
└──────────────────────────────────────────────────────────────────────┘

Note: User can click [📘] [📸] [📧] tabs to switch channel variants
      Each variant keeps separate caption, media selection, links
      Brief context is sticky (scrolls with page)
```

### Mobile Layout (phone: <640px)

```
┌──────────────────────────┐
│ < Create Post    [Save]  │
│                          │
│ Q4 Launch Campaign       │
│ ◆ Unsaved changes        │
├──────────────────────────┤
│                          │
│ CHANNEL SELECTOR         │
│ ☑ 📘 Facebook           │
│ ☑ 📸 Instagram          │
│ ☐ 📧 Email              │
│                          │
│ ─────────────────────    │
│ 📘 FACEBOOK              │
│ ─────────────────────    │
│                          │
│ Format: Feed Post        │
│ (1200 × 628 px)          │
│                          │
│ Caption *                │
│ ┌──────────────────────┐ │
│ │ Be the first to own  │ │
│ │ new collection...    │ │
│ └──────────────────────┘ │
│                          │
│ ⚠️ Caption must be ≥10ch│
│ ✓ Media added            │
│                          │
│ Media                    │
│ [Click to change]        │
│ ┌──────────────────────┐ │
│ │ (Hero image preview) │ │
│ └──────────────────────┘ │
│                          │
│ Hashtags                 │
│ #q4launch #newcollection │
│                          │
│ Link                     │
│ www.example.com/...      │
│                          │
│ AI Suggestions           │
│ ✨ Hook variant:         │
│ "Join VIPs now"          │
│ [Use] [Regen]            │
│                          │
│ BRIEF                    │
│ [Tap to show/hide]       │
│ ▼ Campaign: Q4 Launch    │
│ ▼ Audience: 25-45...     │
│ ▼ CTA: Pre-order now     │
│                          │
│ ─────────────────────    │
│ [Save draft]             │
│ [Request review]         │
│ [Schedule]               │
│ [Publish now]            │
│                          │
└──────────────────────────┘
```

---

## Screen 4: Publish Result / Post Detail

### Desktop Layout (lg: 1024px+)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Q4 Launch: Early Access Post              [Edit] [Reschedule] [Menu]│
├─────────────────────────────────────────────────────────────────────┤
│ < Back to Calendar                                                   │
│                                                                       │
│  Status: ◆ Published (Oct 17, 2:00 PM)                              │
│  Campaign: Q4 Launch    │ Pillar: Product Launch                    │
│  Owner: Sarah Chen      │ Created: Oct 15, 10:30 AM                 │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ PUBLISHING RESULTS (per channel)                               │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │                                                                 │  │
│  │  📘 FACEBOOK                                                    │  │
│  │  ◆ Published (Oct 17, 2:00 PM)                                 │  │
│  │  Post ID: 12345_67890                                           │  │
│  │  View on Facebook ↗                                             │  │
│  │  [Refresh insights]                                             │  │
│  │                                                                 │  │
│  │  📸 INSTAGRAM                                                   │  │
│  │  ◆ Published (Oct 17, 2:15 PM)                                 │  │
│  │  Media ID: abcd_ef123                                           │  │
│  │  View on Instagram ↗                                            │  │
│  │  [Refresh insights]                                             │  │
│  │                                                                 │  │
│  │  📧 EMAIL                                                       │  │
│  │  ◆ Scheduled (Oct 18, 9:00 AM)                                 │  │
│  │  Recipients: 12,847                                             │  │
│  │  [Reschedule] [Cancel]                                         │  │
│  │                                                                 │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ PERFORMANCE (as of 2 hours ago)                                │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │                                                                 │  │
│  │ FACEBOOK                                                        │  │
│  │ ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │  │
│  │ │ Reach    │  │Impressns │  │ Engagmnt │  │ Saves    │        │  │
│  │ │ 2,847    │  │ 4,102    │  │ 156      │  │ 28       │        │  │
│  │ │(Available)│ │(Available)│ │(Available)│ │(Available)│       │  │
│  │ │2 hrs ago │  │2 hrs ago │  │2 hrs ago │  │2 hrs ago │        │  │
│  │ └──────────┘  └──────────┘  └──────────┘  └──────────┘        │  │
│  │                                                                 │  │
│  │ INSTAGRAM                                                       │  │
│  │ ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │  │
│  │ │ Reach    │  │Impressns │  │ Likes    │  │Comments  │        │  │
│  │ │ ⏳        │  │ ⏳        │  │ 34       │  │ 8        │        │  │
│  │ │(Pending) │  │(Pending) │  │(Available)│ │(Available)│       │  │
│  │ │Collecting...│ │           │             │            │       │  │
│  │ └──────────┘  └──────────┘  └──────────┘  └──────────┘        │  │
│  │                                                                 │  │
│  │ [Refresh all] [View campaign report] [Share results]           │  │
│  │                                                                 │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ RELATED CONTENT                                                │  │
│  ├────────────────────────────────────────────────────────────────┤  │
│  │                                                                 │  │
│  │ Linked Conversations (0)                                        │  │
│  │ No conversations yet. [View inbox]                              │  │
│  │                                                                 │  │
│  │ Linked Leads (0)                                                │  │
│  │ No leads traced back to this post. [View leads]                 │  │
│  │                                                                 │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘

Color Legend:
  ◆ Published (teal)
  ◆ Scheduled (blue)
  ⏳ Pending (pulsing animation)
  Available (teal card)
  Pending (blue card, animated)
```

### Mobile Layout (phone: <640px)

```
┌──────────────────────────┐
│ < Post    Q4 Launch      │
│            [Menu]        │
├──────────────────────────┤
│                          │
│ ◆ Published              │
│ Oct 17, 2:00 PM          │
│                          │
│ Campaign: Q4 Launch      │
│ Pillar: Product Launch   │
│ Owner: Sarah Chen        │
│ Created: Oct 15, 10:30AM │
│                          │
│ ─────────────────────    │
│ PUBLISHING RESULTS       │
│ ─────────────────────    │
│                          │
│ 📘 FACEBOOK              │
│ ◆ Published              │
│ Oct 17, 2:00 PM          │
│ Post ID: 12345_67890     │
│ [View on Facebook] ↗     │
│ [Refresh]                │
│                          │
│ 📸 INSTAGRAM             │
│ ◆ Published              │
│ Oct 17, 2:15 PM          │
│ [View on Instagram] ↗    │
│ [Refresh]                │
│                          │
│ 📧 EMAIL                 │
│ ◆ Scheduled              │
│ Oct 18, 9:00 AM          │
│ Recipients: 12,847       │
│ [Reschedule] [Cancel]    │
│                          │
│ ─────────────────────    │
│ PERFORMANCE              │
│ ─────────────────────    │
│                          │
│ FACEBOOK                 │
│ Reach: 2,847 ✓           │
│ Impressions: 4,102 ✓     │
│ Engagement: 156 ✓        │
│ Saves: 28 ✓              │
│ Data from 2 hours ago    │
│ [Refresh]                │
│                          │
│ INSTAGRAM                │
│ Reach: ⏳ (pending)      │
│ Impressions: ⏳ (pend)   │
│ Likes: 34 ✓              │
│ Comments: 8 ✓            │
│ [Refresh]                │
│                          │
│ ─────────────────────    │
│ CONVERSATIONS (0)        │
│ [View inbox]             │
│                          │
│ LEADS (0)                │
│ [View leads]             │
│                          │
└──────────────────────────┘
```

---

## Screen 5: Overview (Optional P0 Extension)

### Desktop Layout (lg: 1024px+)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Akilent Content Studio                               👤 Settings   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  Welcome back, Sarah! Here's what needs attention.                  │
│                                                                       │
│  PRIMARY ACTION                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │  [+ Create content]  [or import brief from Q4 Launch campaign]  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  URGENT (Needs attention now)                                        │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐ │
│  │ Giveaway post – FAILED       │  │ Q4 Launch – IN REVIEW        │ │
│  │ ◆ Failed (Oct 14)             │  │ ◆ In Review (since Oct 16)   │ │
│  │ 📸 Instagram                  │  │ 📘 Facebook, 📸 Instagram    │ │
│  │ Error: Image validation       │  │ Assigned: Marcus Rodriguez   │ │
│  │ [Fix and retry] [Details]     │  │ [Approve] [Request changes]  │ │
│  └──────────────────────────────┘  └──────────────────────────────┘ │
│                                                                       │
│  UPCOMING SCHEDULED                                                  │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐ │
│  │ Offer – Scheduled tomorrow    │  │ Email NL – Scheduled in 3 dy │ │
│  │ ◆ Scheduled (Oct 17, 10 AM)   │  │ ◆ Scheduled (Oct 20, 9 AM)  │ │
│  │ 📘 📸 📧                      │  │ 📧 Email                    │ │
│  │ [View] [Reschedule]           │  │ [View] [Reschedule]         │ │
│  └──────────────────────────────┘  └──────────────────────────────┘ │
│                                                                       │
│  RECENT PERFORMANCE                                                  │
│  ┌──────────────────────────────┬──────────────────────────────────┐ │
│  │ Campaign: Q4 Launch           │ Last 7 days                      │ │
│  ├──────────────────────────────┼──────────────────────────────────┤ │
│  │ Posts published: 4            │ Reach: 18,234                    │ │
│  │ Engagement rate: 4.2%         │ Engagement: 892                  │ │
│  │ Best performer: Reel (35K)    │ Top platform: Instagram          │ │
│  │ [View campaign report]        │                                  │ │
│  └──────────────────────────────┴──────────────────────────────────┘ │
│                                                                       │
│  QUICK ACTIONS                                                       │
│  [View calendar] [Manage campaigns] [Connect channels] [Settings]   │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Interaction Flow

### Creating a Post from Plan Item (Desktop)

```
1. User is on Calendar or Plan view
2. Clicks "Create post from brief" or [+ Create post]
3. System opens Composer with brief context pre-filled
   ├─ Brief panel visible on left (sticky)
   ├─ Composer area in center (caption, media, links)
   ├─ Platform tabs showing [📘] [📸] [📧]
   └─ Preview on right
4. User selects channels to publish to
   ├─ If 📸 selected, defaults to "Reel" format
   ├─ If 📘 selected, defaults to "Feed post"
   └─ Can change format per channel
5. User enters/edits caption for first channel
   ├─ AI suggestions shown inline
   ├─ Validation errors displayed beside field
   └─ Platform-specific warnings shown (e.g., "Reel max 90 min")
6. User switches to second channel tab [📘]
   ├─ Caption remains unchanged (shared by default)
   ├─ Can override caption per channel
   └─ Media selection is per-channel (can differ)
7. User saves draft
   ├─ Auto-save happens every 30 sec
   └─ Unsaved indicator shows on page
8. User submits for review
   ├─ Status changes to IN_REVIEW
   ├─ Assigned reviewer gets notification
   └─ Creator sees feedback panel
9. Reviewer approves
   ├─ Status changes to APPROVED
   └─ Creator can now schedule
10. Creator schedules post
    ├─ Selects date/time (default to brief deadline)
    ├─ Shows timezone
    ├─ Status changes to SCHEDULED
    └─ Confirmation shown
11. Publishing happens automatically
    ├─ Status changes to PUBLISHING, then PUBLISHED
    ├─ Per-channel outcomes shown (one may fail)
    ├─ Metrics collection starts
    └─ Partial success handled gracefully
```

### Error Recovery (Composer → Detail)

```
1. User publishes post
2. One channel succeeds, one fails (partial success)
   ├─ Facebook: Published ✓
   └─ Instagram: Failed (validation error)
3. Detail view shows:
   ├─ 📘 Published (Oct 17, 2:00 PM)
   └─ 📸 Failed – "Image aspect ratio invalid" [Fix and retry]
4. User clicks [Fix and retry]
5. Composer re-opens with error context
   ├─ Instagram channel pre-selected
   ├─ Error message highlighted
   ├─ User adjusts media/aspect ratio
   └─ User saves
6. User confirms to retry publishing
   ├─ Status changes back to PUBLISHING
   └─ Only Instagram is retried (FB already published)
7. Instagram publishes
   ├─ Detail view updates
   └─ Metrics collection includes both channels
```

---

## Responsive Breakpoints & Adaptations

### Calendar View
- **Desktop (lg+)**: Week or month view, drag-to-reschedule enabled
- **Tablet (sm–lg)**: Day/week view only, click-to-reschedule
- **Mobile**: Day view, swipe to navigate, tap card to detail

### Composer View
- **Desktop (lg+)**: Three-column (brief | editor | preview)
- **Tablet**: Two-column (editor | preview), brief in collapse
- **Mobile**: Single column with tabs (editor | brief | preview)

### Post Detail View
- **Desktop (lg+)**: Publishing results and performance side-by-side
- **Tablet**: Stacked, but same components
- **Mobile**: Stacked, performance in accordion, links prominent

---

## Notes for Implementation

1. **Channels are selective**: User picks 📘 and 📸, not 📧? Composer shows only those tabs.
2. **Brief context is smart**: If brief has required assets but post has none, validator shows warning.
3. **Approval is optional**: Solo creators skip the approval step; form shows "Save draft" + "Schedule" buttons only.
4. **Scheduling shows timezone**: "Oct 17, 2:00 PM EDT" — no guessing.
5. **Error messages are specific**: Not "Error" but "Image aspect ratio must be 0.8:1 to 1.91:1".
6. **Metrics are honest**: Unsupported = "—", not "0". Pending = spinner, not blank.
7. **Mobile is full-featured**: Not a stripped-down view. Composer, approval, metrics all available on phone.

---

## Next Steps

1. Create reusable template components in `/templates/components/`
2. Build mockups in HTML/Alpine to test interactivity
3. Run task-based usability tests with representative user
4. Gather feedback on brief visibility, channel switching, error recovery
5. Proceed to Stage 2: Implement screens with real persistence
