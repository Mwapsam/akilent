# Audience Intelligence: requirements specification and cost framework

Status: **candidate future capability.** The direction was reviewed and accepted on 2026-11-08. The pilot freeze stays in place, and each phase below needs its own go-ahead.
Internal name: Audience Intelligence. Owner-facing name: **"What people are saying"**. The UI never uses the word "sentiment".

> Guiding principle: Audience Intelligence becomes a trustworthy **measurement** capability before it becomes a recommendation or optimisation capability.

---

## 1. Objectives and outcomes

Audience Intelligence helps a business understand three things from comments and engagement:

1. **Audience demand**: what people want, ask about and need.
2. **Customer experience**: problems, unanswered questions and recurring frustrations.
3. **Commercial outcomes**: which engagement is associated with conversations, leads and paid orders.

It strengthens the existing chain **Marketing → Engagement → Conversation → Lead → Order → Revenue**. It is not a standalone analytics module.

Pilot test: a business should be able to answer these questions inside Akilent:
1. What are my customers repeatedly asking about?
2. What are people interested in buying?
3. What is frustrating my customers?
4. Which products or posts attract the most commercial interest?
5. What should I change in my marketing or customer communication?
6. Can I see measurable outcomes associated with these interactions?

There is also a strategic question: would an owner pay for this intelligence separately from the inbox?

## 2. Functional requirements

- **FR1.** Every inbound Instagram and Facebook comment gets audience signals. A comment can carry several signals; one of them is primary.
- **FR2.** Classification is deterministic and complete when AI is off.
- **FR3.** Moderation behaviour is unchanged. Moderation and audience classification are separate classifiers with separate outputs.
- **FR4.** Classification is idempotent: redelivered webhooks and repeated backfills never duplicate signals.
- **FR5.** History is kept. A new classifier version adds a new run and never overwrites an old one.
- **FR6.** An owner can add or reject a category on a comment, and that correction survives reprocessing.
- **FR7.** Per-post response: category counts, distinct commenters, real example comments, and unanswered buying intent.
- **FR8.** Per-account patterns, with trends over fixed windows, come only after the per-post metrics are validated.
- **FR9.** Every finding offers a contextual action. Actions are suggestions; Akilent never takes one automatically.
- **FR10.** Deleting a comment or erasing a customer removes their identifiable or reconstructable data from signals, examples and summaries.

## 3. Taxonomy

| Primary category | Code | Subcategories |
|---|---|---|
| Buying intent | `buying_intent` | `price`, `purchase`, `availability`, `ordering`, `dm_request` |
| Product enquiry | `product_enquiry` | `size`, `colour`, `specs`, `stock`, `delivery`, `location` |
| Complaint | `complaint` | `quality`, `delivery_delay`, `pricing`, `service`, `dissatisfaction` |
| Praise | `praise` | `product`, `brand`, `satisfaction` |
| Content feedback | `content_feedback` | `appreciation`, `criticism`, `more_content` |
| Spam / abuse | `spam_abuse` | `spam`, `promo`, `toxic` |
| General engagement | `general` | `greeting`, `chit_chat`, `other` |

**Multiple signals.** "I love this dress. Do you have it in size 16, and how much is it?" produces three signals: praise/product, product_enquiry/size and buying_intent/price.

**Primary category.** The primary category is used only for single-bucket views. Counts per category always use every signal. The precedence order is:

1. buying_intent
2. complaint
3. product_enquiry
4. content_feedback
5. praise
6. spam_abuse
7. general

Spam or abuse becomes primary only when the comment has no commercial or experience signal. A spam signal never removes a commercial or complaint signal; it stays attached alongside it. One exception: a spam or promo match drops praise, content feedback and general signals from the same comment, because bots flatter ("Nice pics! DM me for promo 🔥"), and that would inflate praise counts.

**Spam phrases consume their text.** A phrase such as "buy followers" is matched as spam, and its words are not matched again as buying intent. A commercial signal survives only when it comes from text outside the spam phrase.

**Language.**
- Version 1 uses deterministic rules for English.
- The term lists are keyed by language, so a new language means adding a list, not changing code.
- `language` is stored when one of the language lists matched.
- Non-English coverage comes from the optional AI pass in Phase 3.
- Before the taxonomy is signed off, Phase 2 checks the language mix of Samantha's real comments (§11). If non-English comments are a material share, rules for that language join version 1.

## 4. Data model and processing

### Models (`apps/audience`)
- **`AudienceSignalRun`**: one classification of one comment. It has:
  - `account`, and `channel` (`instagram` / `facebook`);
  - explicit nullable FKs `ig_comment` → `instagram.Comment` and `fb_comment` → `facebook.FacebookComment`, both CASCADE, with a check constraint that exactly one is set;
  - `source` (`rule` / `ai`), `classifier_version`, `language`;
  - `text_hash`, the SHA-256 of the body that was classified;
  - `occurred_at`, the comment time, used for reporting windows;
  - `metadata` (conflicts and diagnostics), `created_at`.
  - It is unique per (comment, source, classifier_version, text_hash), with one partial constraint per channel. This makes runs idempotent and history-preserving. An edit produces a new hash and therefore a new run.
- **`AudienceSignal`**: one category or subcategory within a run. It holds `category`, `subcategory`, `is_primary` and `matched_terms`. Matched terms come from Akilent's lists and are never free text. It is unique per (run, category, subcategory).
- **`AudienceSignalCorrection`**: an owner's correction. It holds the comment FK (same pattern as runs), `category`, `subcategory`, `action` (`add` / `reject`), the `text_hash` at the time of the correction, `created_by` and `created_at`. It is unique per (comment, category, subcategory), and a later correction updates the row in place.

The models use explicit FKs rather than a generic FK. That gives referential integrity, CASCADE erasure and efficient filtering. A new channel adds one nullable FK.

### Resolution: the "current" signals of a comment
1. **Rule signals** come from the latest rule run whose `text_hash` matches the current body. If none matches, the latest rule run is used.
2. **AI signals** come from the latest AI run for the same text, and apply only when the rules found nothing specific (only `general`). AI then replaces `general`. Where the rules found something specific, the rule result wins and AI is ignored. When AI disagrees (for example, rules say praise and AI says complaint), the AI writer records the conflict in that run's `metadata` for diagnostics in Phase 3. Owners never see it. AI never removes a rule signal.
3. **Owner corrections** apply when their `text_hash` matches the current body. `add` inserts a category and `reject` removes one. A correction belongs to the comment, not to a run, so it persists across classifier versions and always wins.
   - When a new classifier version disagrees with a correction, that disagreement becomes a metric used for rule tuning.
   - When the comment's text is edited, the correction stays as history but no longer applies.
4. The primary category is recomputed from the result using the precedence in §3.

### Processing
- **On ingest:** after moderation on both channels, inbound comments only. Classification never raises into the webhook pipeline.
- **On edit:** Facebook `edited` updates the body, which triggers reclassification as a new run. Instagram sends no edit webhooks.
- **Backfill:** the `audience_backfill` command is idempotent and bounded by account, days and limit. It classifies with the current version and has no side effects; it sends no messages, emails or leads.
- **Re-run:** a new `classifier_version` is processed through the same backfill.

### Edge cases
| Case | Behaviour |
|---|---|
| Duplicate webhook | The run already exists (unique key), so nothing happens |
| Outbound comment (the business replying) | Not classified |
| Empty body (sticker or media only) | Not classified |
| Reply (nested comment) | Classified like a root comment. Reports may show roots and replies separately |
| Hidden by moderation | Still classified and counted |
| Deleted (Akilent delete, or Facebook `remove`) | Run rows remain for the life of the comment row. Reports exclude it, and its text never appears as an example |
| Customer erased | The comment rows are deleted, and runs, signals and corrections cascade with them |
| No canonical contact | Classified. "Distinct commenters" counts the channel identity |
| Post not published by Akilent | Reported per raw `post_id`, with no attribution |
| Comment arrives before the post is linked | Reports join through the thread's `content_variant` when they are run, so a later link is picked up |

## 5. Existing vs modified vs new

| | Items |
|---|---|
| **Existing, reused as is** | `instagram.Comment` / `CommentThread`, `facebook.FacebookComment` / `FacebookCommentThread`, `ConversationEntryPoint`, `ConversationAttribution`, the funnel in `apps/conversations/api.py`, `ai_api.classify` and `unavailable_reason`, `billing.metering` reserve/commit/release, contact erasure |
| **Modified** | IG comment ingest (`apps/instagram/services/inbound.py`), FB comment ingest and edit (`apps/facebook/services/inbound.py`). Each gets one guarded call |
| **New** | `apps/audience` (classifier, models, services, backfill), report queries, UI tiles, and in Phase 3 the AI batch task |

## 6. AI requirements and cost controls (Phase 3)

- AI is optional, and all deterministic results stand on their own without it. AI is off whenever `ai_api.unavailable_reason()` returns a reason.
- **Which comments are sent:** only comments the rules placed in `general/other` **and** that pass an "informative" pre-filter. The filter skips comments that are emoji-only, mention-only, a single greeting, or below a minimum token count.
- **Batching:** N comments per call (default 25) on the fast tier. The output is a fixed JSON schema that maps each comment id to its signals.
- **Billing reconciliation:**
  - There is one reservation per batch, keyed by a deterministic batch id: a hash of the sorted comment ids plus the classifier version.
  - Success commits the actual usage. Total failure releases the reservation.
  - A partial result commits only the comments that were classified; the rest are requeued under a new batch id.
  - A retry of the same batch id reuses its reservation and never creates a second one.
  - A nightly reconciliation releases orphaned reservations that have no matching run.
- **Theme summaries** must cite source comment ids, the period, the count of **distinct commenters** and the posts involved. A theme with fewer than a minimum number of distinct commenters is dropped. One person repeating a question counts once.

### Cost framework
Prices are **read from the configured model at the time the estimate is made**. They are never hard-coded here.

```
cost per 1,000 comments =
    s × (1000 / B) × (T_in × P_in + T_out × P_out)

s      share of comments reaching AI (after rules and the informative filter)
B      batch size
T_in   input tokens per batch  ≈ prompt_template_tokens + B × avg_comment_tokens
T_out  output tokens per batch ≈ B × tokens_per_classification
P_in, P_out   the configured model's price per input/output token
```

Akilent's internal metering also charges `ai_action` units per batch (`apps/billing/costs.py`), and the work is subject to the `ai_actions_day` cap.

Sensitivity is reported for s = 10%, 25% and 50%. The Phase 2 data (the share left in `general/other` on real accounts) supplies the actual value of s before Phase 3 is approved.

## 7. Attribution and reporting methodology (Phase 4)

### Evidence levels
| Level | Evidence | What may be claimed |
|---|---|---|
| Comment | A `ConversationEntryPoint` of kind `ig_comment` / `fb_comment` created by a private reply to *this* comment, whose conversation has an attributed lead or order | "This comment led to this conversation and this outcome" |
| Post | `ConversationAttribution.source_post` / `source_variant` | "This post is associated with these conversations and outcomes" |
| Aggregate | Derived across posts | "Posts with more product enquiries were associated with more recorded purchases" |

A post-level result is never presented as a comment-level one.

### Coverage
Reports always show linked comments, comments with no linked conversation, and outcomes. They keep **three different states** apart:
- zero recorded outcomes;
- not attributable (no evidence link);
- not yet recorded (the window is still open).

Lower bounds and the untracked bucket follow the existing funnel.

### Counting
- The headline unit is **distinct commenters**, counted by channel identity.
- Comment counts and linked-conversation counts are secondary.
- Windows are 7 and 28 days, compared with the previous window of equal length.

### Comparative claims
There are no multiplier claims such as "3×" until rules exist for minimum sample size, comparable posts and coverage thresholds. Until then the wording is "associated with".

## 8. Privacy and Meta compliance

- Signals are scoped to one account. There is no cross-business profiling and no person-level profile beyond the business's own contacts.
- Customer erasure (`apps/contacts/erasure.py`) deletes IG/FB identities and their comments, and runs, signals and corrections cascade with them.
- **Removed** on comment deletion or erasure:
  - comment text and example snippets;
  - contact and commenter identifiers;
  - per-comment signal rows (on erasure);
  - AI payloads, and theme evidence citing the comment. Themes are recomputed, and a theme that falls below its evidence minimum disappears.
- **Retained:** only rolled-up counts per period, account, post and category. They hold no ids and no text. A minimum cell size prevents reconstruction, and closed periods are not recomputed downward.
- AI payloads are not retained beyond the existing AI audit. The Meta data-deletion callback follows the same erasure path.

## 9. UI/UX per surface

| Surface | Phase | Content |
|---|---|---|
| Comments inbox | 2 | Category chips on each comment, a filter by category, and one-click correction (add/reject) |
| Content Studio post detail | 2 | A "What people are saying" tile: counts by category (distinct commenters), 2–3 real examples per category, unanswered buying intent, and coverage |
| Business Health | after validation | An audience patterns pillar: windowed trends and recurring questions |

Each finding is paired with a contextual action:

| Finding | Contextual action |
|---|---|
| Repeated delivery questions | Improve the delivery answer in Knowledge |
| Frequent size questions | Add a sizing FAQ |
| Unanswered buying intent | Follow up with those customers (existing reply or DM paths) |
| Repeated complaints | Review the affected product |
| Praise | Possible social proof |
| Requests for demos | Consider demonstration content |

The UI shows no scores and no confidence percentages.

## 10. Phases, dependencies, estimates, acceptance

| # | Phase | Depends on | Estimate | Third party |
|---|---|---|---|---|
| 0 | Discovery and data validation | | done (2026-11-08) | none |
| 1 | Signal foundation: classifier, models, ingest hooks, backfill. No UI, no AI | 0 | built 2026-11-13 | none |
| 2 | Reporting and validation: inbox chips and filter, correction, post tile, internal diagnostics | 1 | built 2026-11-13, behind release `audience_insights` | none |
| 3 | AI-assisted: batch classification, multilingual coverage, grounded themes | 2 and its data | M–L | AI provider |
| 4 | Commercial outcomes: evidence levels, coverage | 2 | comment-level outcomes and coverage on the post summary and in `audience_report`, built 2026-11-13. Aggregate comparisons wait for Phase 5's sample-size rules | none (internal attribution) |
| 5 | Evidence-based optimisation: patterns across posts | 3, 4 and a validated measurement layer | L | optional AI |
| 6 | Cross-channel: the taxonomy applied to DMs (WA/IG/FB) | 2 | M | none |

### Acceptance
- **Technical:**
  - every eligible comment is processed;
  - classification is deterministic when AI is off;
  - moderation decisions are unchanged (compatibility tests);
  - processing is idempotent;
  - a comment can have several signals;
  - corrections survive reprocessing;
  - deletion and erasure cascade;
  - history stays explainable;
  - report figures reconcile with the source rows.
- **Data quality:**
  - the category distribution is measured on real data;
  - unclassified comments are visible in internal diagnostics;
  - repeated comments are distinguished from distinct commenters;
  - windows are consistent;
  - every example traces to its source;
  - coverage is explicit;
  - AI summaries cannot introduce unsupported claims.
  - A high `general` share **triggers an investigation**; it is not a gate.
- **Commercial:** the six questions in §1 can be answered in a pilot session, and the willingness-to-pay probe has been run.

## 11. Pilot validation

- **Candidate:** Samantha (an online store on WhatsApp and Instagram).
- **Before the Phase 2 sign-off**, run a read-only prod-shell sample of her comments to collect:
  - comment volume per post;
  - share by detected language;
  - share placed by the English rules;
  - the `general` share;
  - the coverage rate (comments with a linked conversation).
- **Session:** a guided walkthrough of the six questions, recording which ones the owner can and cannot answer.
- **Willingness to pay:** "If this were a separate add-on, would you pay for it, and what would it have to show?"

## 12. Exclusions and approval gates

Out of scope until separately approved:
- autonomous replies to comments;
- automatic customer outreach;
- automated marketing recommendations;
- cross-business audience profiling;
- automatic content generation from signals;
- new CRM functionality;
- additional social integrations;
- **insight rules** built on signals, such as a complaint spike or fan outreach, until the Phase 6 action loop is proven.

Gates:
- Every phase needs its own go-ahead.
- The pilot freeze stays.
- Under the messaging truth rule, there are no copy or landing claims until the feature is released and in the claim register.

---

## Appendix A: open decisions (recommended defaults; the owner confirms)

| Area | Decision | Recommended default |
|---|---|---|
| Signal granularity | Several signals per comment? | Yes, with one primary |
| Lifecycle | History and corrections | Append-only runs, plus a separate correction table |
| Language | Initial languages | English rules; others after the Samantha sample |
| Confidence | Trust threshold | Rules: matched or not. AI: only labels inside the schema; no confidence is shown |
| Reporting unit | Comments, commenters or conversations? | Distinct commenters as the headline |
| Trends | Windows | 7 and 28 days, against the previous equal window |
| Attribution | Evidence for a sale | Comment-level evidence only through entry point → attribution |
| AI billing | Reserve and retry | One reservation per deterministic batch id, plus a nightly reconcile |
| Privacy | Derived data deletion | Cascade, recompute themes, keep only aggregate counts with a minimum cell size |
| Ownership | Owner corrections? | Yes, add or reject per category |
| Scale | Historical vs new comments | Ingest hook for new comments; bounded backfill for history |
| Cost | Per 1,000 comments | The §6 formula, filled in with Phase 2 data |

## Appendix B: traceability to the 2026-11-08 review

| Review point | Spec section |
|---|---|
| §1 Tighten the objective (demand, experience, outcomes) | 1 |
| §2 Separate moderation from audience classification | 2 (FR3), 4 |
| §3 Hierarchy, content feedback, several signals | 3 |
| §4 History and versioning, idempotency, explicit FKs, deletion | 4, 8 |
| §5 Evidence levels, coverage, no "3×" | 7 |
| §6 AI prioritisation, multilingual, grounded themes, distinct commenters | 3, 6 |
| §7 Phase order | 10 |
| §8 Surfaces staged, contextual actions | 9 |
| §9 Technical, data-quality and commercial acceptance | 10 |
| §10 Open decisions and edge cases | 4 (edge cases), Appendix A |
| §11 Exclusions, freeze | 12 |
| Round 2: rule/AI coexistence, primary precedence, signal-level corrections, billing reconciliation, retained vs removed data, Samantha language check | 4, 3, 4, 6, 8, 11 |
