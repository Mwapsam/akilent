# Content Performance Recovery

**Status:** Strategy adopted 2026-11-13. Phase 0 and Phase 1 built (uncommitted). Phases 2–3 are not authorised.  
**Scope:** Published Instagram and Facebook posts in Content Studio  
**Code:** `apps/publishing/metrics.py` (measurement truth), `apps/publishing/visibility.py` (diagnosis), flag `cs_visibility`

> The pilot feature freeze applies. Phase 2 and Phase 3 below each need their own owner approval before any work starts. Nothing here goes into public copy until it ships and is verified (`docs/marketing/messaging.md`).

## 1. The idea

A business needs answers to four separate questions, not another likes counter:

1. Why isn't anyone seeing my posts?
2. Why are people seeing my posts but not responding?
3. Why are people responding but not buying?
4. What should I do differently?

Content Performance Recovery answers them in three stages:

| Stage | Question | Recovery |
|---|---|---|
| **Visibility** | Are potential customers seeing the content? | Get the content in front of people |
| **Engagement** | Are people responding to what they see? | Make content more relevant |
| **Conversion** | Is interaction turning into chats, leads and paid orders? | Turn attention into sales |

**Rule: always diagnose the earliest broken stage before recommending improvements to later stages.** There is no point in suggesting better captions when nobody has seen the post.

| Observed situation | What to work on |
|---|---|
| Zero reported reach | Visibility |
| Very low reach (vs comparable posts) | Visibility opportunities |
| Reach, no interactions | Content relevance and appeal |
| Interactions, no chats | Turning attention into conversations |
| Chats, no paid orders | Sales conversion |
| Paid orders | Commercial performance |

## 2. What the data can and can't say

### Four reach states. Missing data is never zero.

| State | Meaning | Stored as |
|---|---|---|
| Real zero | The channel returned reach = 0 | value `0`, state `available` |
| Not yet available | No number yet, and the read was within 48 h of publish | state `pending` |
| Not reported / unsupported | No number on a read made more than 48 h after publish, or the channel doesn't report it for this kind of post | `not_reported` (computed by `effective_state`, never stored), `unsupported` |
| Read failed | The read didn't work (error, permission) | `failed`, `needs_permission` |

- Every screen that shows a metric state goes through `metrics.effective_state()`, so the post page and the Library can't disagree.
- A failed read doesn't erase what was observed before. `metrics.last_known()` returns the newest number that was actually read, and the screens show it with an "as of" date. `metrics_latest` still records exactly what the latest read returned.
- Exception, on purpose: Facebook leaves `shares` out of a post nobody has shared, so after a successful field read a missing `shares` is stored as 0.
- Account-level numbers (`account_metrics.py`) are per day and not part of this rule set yet.

### Observed vs interpreted

- "Zero reported reach" is an observation. "Meta isn't showing your post" is an interpretation Akilent can't verify, so it never says it.
- Akilent doesn't know how Meta ranks or shows content, and it never claims to (see `meta-growth-optimisation.md` §7).
- Meta doesn't share Instagram or Facebook account restrictions with apps. Akilent always shows this check as **unknown** and points the owner to Account Status in the app.
- **24 hours is Akilent's check window,** not a promise from Meta that numbers or views arrive by then.
- A healthy connection doesn't prove a post's numbers can be read, and an expired token doesn't explain zero reach. Connection and readability are separate checks, and neither is presented as a cause.
- A post with reach but no interactions isn't necessarily bad content. The audience may be small, the post recent, or people may not have responded yet. Akilent states the funnel condition and lists such factors only as possibilities.

### Comparable baselines

"Low reach" compares a read only with reads of the **same age** (the same planned read: 1 h, 6 h, 1 d, 3 d, 7 d or 28 d), from the same account, channel and kind of post (feed posts, reels or Facebook posts), over the last 90 days. It needs at least 5 comparable posts, and stories are left out. "Low" means under a quarter of the median. The card always shows what it compared with.

## 3. Phases

| Phase | What | Status |
|---|---|---|
| **0. Measurement truth** | `NOT_REPORTED`, `effective_state` everywhere, `last_known` + "as of", explicit Library labels, best times use the newest read that has numbers, a post-missing marker from the collector | Built 2026-11-13, always on |
| **1. Visibility diagnosis** | Post page Visibility card: stage, what Akilent checked (ok / problem / unknown), and manual next steps. Read-only | Built 2026-11-13, flag `cs_visibility` (off) |
| **2. Recovery actions** | Owner-approved actions: share to Story, send to opted-in WhatsApp customers, create another version, a paid check backed by `promote.py` evidence. An action log and reach-after-action monitoring. Nothing automatic | Needs go-ahead, after Phase 1 is validated with pilots |
| **3. Engagement and conversion recovery** | Build on audience signals (`apps/audience`) and `results.results_for()` | Needs go-ahead |

### Phase 1 stages (`visibility.diagnose`)

They are checked in this order, and the first match wins:

1. `not_published`: the channel isn't live (failed or still going out).
2. `post_missing`: the channel said the post no longer exists (error 100/33).
3. `cant_read`: no number, and none read before (failed, permission, not reported, unsupported).
4. `too_early`: under Akilent's 24 h check window with no number or with zero.
5. `zero_reported_reach`: reach 0 on a read at least 24 h after publish.
6. `low_reach`: under 25% of the comparable baseline (reads at least 1 day old).
7. `reached`: a plain funnel condition, e.g. "Seen by 140 · 12 interactions · 3 chats · no orders yet". Chats are mentioned only when they are tracked for that post.

Next steps are suggestions the owner carries out themselves. A paid boost is mentioned only for zero reported reach, as "you choose and pay Meta directly". Akilent never handles that money and takes no share of ad spend.

## 4. Long-term direction (not a claim)

Akilent could come to understand **where a business loses potential customers**: before they see the content, after they see it, before they start a conversation, during the conversation, and before they buy. This direction connects Content Studio, the inbox and attribution. It isn't approved copy, and it authorises nothing beyond the phases above.

## 5. Validating Phase 1

Release `cs_visibility` to pilot workspaces and check:

- How many published posts land in each stage.
- Whether owners understand the card.
- Whether `cant_read` is rare once channels are reconnected with insights permission.

Phase 2 is proposed only when owners act on zero or low reach posts.
