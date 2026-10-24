# Pricing review

Status: **6.1 audit done (2026-10-24). 6.2 unit costs are waiting on figures. 6.3 margins have formulas and break-even points, with final numbers waiting on 6.2.**
Nothing here changes a plan. Prices, quotas and entitlements change only after the matrix in 6.4 is approved.

The $19 / $49 / $99 prices are reference points, not decisions.

Sources:
- `apps/billing/management/commands/seed_plans.py`, `features.py`, `limit_catalog.py`, `costs.py`, `metering.py`, `pricing.py`, `api.py`.
- The local development database. Before deciding anything, check production on the Operator Console: `/manage/plans/`, `/manage/plans/features/`, `/manage/plans/limits/`, `/manage/plans/costs/`.

---

## 6.1 Audit

### How billing decides things today

| Question | Where it's answered |
|---|---|
| Can they use it? | `PlanFeature` rows (catalog keys in `features.py`) plus `AccountFeatureOverride`, read via `billing_api.entitled()` / `usable()` |
| How much? | `PlanLimit` rows (keys in `limit_catalog.py`) plus `AccountLimitOverride`, read via `billing_api.limit()` / `require_room()` / `reserve()` |
| What does it cost us? | `UnitCost` (only `email` and `ai_action`) plus `CostSettings.fixed_monthly_cost_per_business`, via `costs.plan_economics()` (worst case at full use) |
| What does a longer period cost? | `pricing.py`: quarterly ×0.95, annual ×0.85, two-year ×0.75 of the monthly price |
| Warnings | `metering.warnings()` and the owner notice at 80% and 100% (`WARN_AT = 0.8`) |

The old `max_*` columns on `Plan` are dead. Nothing outside billing may read them (there is an AST test), and limits come only from `PlanLimit`.

### Current plans (local database)

| Plan | Price/mo | Service | Conversations/mo | Emails/mo | Recipients per email campaign | AI actions/day | Automations | Customers | Seats |
|---|---|---|---|---|---|---|---|---|---|
| Trial (14 days) | $0 | Email & WhatsApp | 100 | 500 | 500 | unlimited* | unlimited | unlimited | unlimited |
| Starter | $19 | Email | 1,000 | 10,000 | 1,000 | unlimited* | unlimited | unlimited | unlimited |
| Professional | $49 | WhatsApp | 5,000 | 50,000 | 5,000 | unlimited* | unlimited | unlimited | unlimited |
| Business | $99 | Email & WhatsApp | unlimited | unlimited | unlimited | unlimited* | unlimited | unlimited | unlimited |

\* "Unlimited" AI is still capped by the site-wide `AI_DAILY_CALL_LIMIT` (500 a day per business by default). That is up to **15,000 AI actions a month on every plan, the free trial included**.

On every plan:
- WhatsApp numbers = 1.
- WhatsApp marketing, utility and verification-code messages are unlimited. That is fine: Meta bills them to the business, not to Akilent.
- `emails_day` is unlimited.

Features per plan (local):

| Feature | Trial | Starter | Professional | Business |
|---|---|---|---|---|
| inbox, whatsapp, customers, follow-ups, history, dashboard (CORE tier, every plan) | ✓ | ✓ | ✓ | ✓ |
| automations, ai_assistant, sales, orders | ✓ | ✓ | ✓ | ✓ |
| whatsapp_campaigns, verification_codes | ✓ | ✓ (but an email-only plan) | ✓ | ✓ |
| email_sending, email_templates | ✓ | ✓ | ✓ (but a WhatsApp-only plan) | ✓ |
| email_campaigns | – | ✓ | ✓ | ✓ |
| email_tracking, insights (Business Health), priority_support | – | – | ✓ | ✓ |
| **instagram** | – | – | – | – |
| **content_studio** | no key exists; every plan gets it implicitly | | | |

### Where each limit is enforced

| Limit | Enforced at | When reached |
|---|---|---|
| `conversations_month` | `apps/whatsapp/services/inbound.py:203` → `billing_api.count_conversation` | SOFT: counted and warned, never blocked. **WhatsApp only. Instagram conversations are not counted.** |
| `whatsapp_numbers` | `apps/billing/limits.py:47` | REFUSE |
| `whatsapp_*_msgs`, `verification_codes_month` | `apps/whatsapp/tasks.py:1098`, `verification_codes.py:211` | HOLD |
| `whatsapp_campaign_recipients` | `apps/whatsapp/campaigns.py:123` | REFUSE |
| `emails_month`, `emails_day` | `LimitChecker.check_email` reservations (`apps/billing/limits.py`), `apps/accounts/views.py:1111` | HOLD |
| `email_campaign_recipients` | `apps/api/services.py:388` | REFUSE |
| `ai_actions_day` | `apps/ai/tasks.py:34` `_over_daily_limit`: proposals, drafts (captions, templates, knowledge, website import) and conversation memory | HOLD. Not metered: the website chatbot responder (`apps/chatbot/services/responder.py`), which is unreleased. |
| `automation_rules` | `apps/automation/api.py:68` | REFUSE |
| `contacts` | `apps/contacts/services.py:71`, CSV import `:236`, list building `apps/contacts/list_building.py:354` | **REFUSE: a new customer is not recorded** |
| `team_members` | `apps/accounts/settings_views.py:560` | REFUSE |
| Publishing (posts, connected accounts, media) | – | **No limit exists** |

### Findings

1. **The ladder changes channel instead of growing.** Starter is email-only and Professional is WhatsApp-only. A business going from $19 to $49 loses email in signup and onboarding, even though Professional carries every email feature. The features also disagree with the service:
   - Starter (email) carries `whatsapp_campaigns`.
   - Professional (WhatsApp) carries `email_*`.
   - The signup cards hide this by stripping channels.
2. **No active plan sells Instagram** (local). Migration `0024_plans_include_instagram` only covered plans that existed then, and `seed_new_plan_features` doesn't add `instagram`. Instagram DMs, half of the "Talk" story, can't be bought. Check production.
3. **Content Studio isn't a sellable feature.** It has no catalog key, so it can't appear on a pricing card or be gated. (Phase 1 adds `content_studio` on all plans.)
4. **The cost guardrail is switched off in practice.** `UnitCost` is empty and the fixed cost is $0, so `plan_economics()` reports every plan at a 100% margin and `loss_reasons()` never fires.
5. **AI usage is effectively uncapped on every plan, Trial included.** `ai_actions_day = -1` everywhere, leaving only the 500/day site ceiling, or 15,000 a month. It is the largest cost risk (see 6.3).
6. **Business ($99) has no ceiling on email** (`emails_month = emails_day = -1`), so the email cost has no upper bound.
7. **Instagram conversations and publishing are unmetered.** `conversations_month` depends only on WhatsApp in `LIMIT_CHANNEL_DEPS`, so it disappears from the pricing card when WhatsApp is off.
8. **`contacts` is REFUSE.** On a capped plan, a new customer's record isn't created, which conflicts with "never block recording your own customers". (Every plan is unlimited today, so nobody is affected yet, but the setting is armed.)
9. **There are no capacity levers between tiers.** Seats, automations and customers are unlimited everywhere, so only conversations and emails differ. The tiers differ by channel and a few features, not by capacity.
10. **`seed_plans` disagrees with the real limits.** It writes `max_automation_rules = 2/10/50`, but `automation_rules` has no `legacy_column`, so the real `PlanLimit` is unlimited. Running the seed gives a false picture.
11. **AI token usage isn't stored.** Providers return `input_tokens` / `output_tokens` (`apps/ai/providers/base.py:27`), but nothing records them. The cost per AI action can only be estimated, not measured.
12. **Free Trial exposure.** 14 days × up to 500 AI actions a day = up to 7,000 AI actions, plus 500 emails, per free signup.

---

## 6.2 Unit costs (fill in)

Only costs Akilent pays per unit count toward a plan's margin.

| Driver | What Akilent pays | Your figure | How to get it |
|---|---|---|---|
| AI action, main model (`AI_MODEL`) | input + output tokens per call | **$ ___ / action** | provider pricing × typical tokens per call (see note) |
| AI action, fast model (`AI_MODEL_FAST`), used by memory and simple replies | same | **$ ___ / action** | same |
| Email (SES) | per 1,000 emails, plus attachments/data | **$ ___ / 1,000** | AWS bill, SES line |
| Media storage and egress (S3) | GB-month stored, GB transferred | **$ ___ / GB-month**, **$ ___ / GB out** | AWS bill, S3 lines; average stored per business |
| Payment processing | % + fixed per charge | **___ % + $ ___** per provider (Stripe / Flutterwave / PayPal) | provider dashboard, for your country and card mix |
| Infrastructure per active business | servers, database, Redis, monitoring ÷ businesses | **$ ___ / business / month** | monthly hosting bill ÷ active accounts |
| Support time per business | optional | **$ ___ / business / month** | your estimate |
| WhatsApp messages | **not an Akilent cost.** Meta bills the business directly; Akilent must not resell or mark it up | – | – |
| Meta Graph API (Instagram, Facebook publishing) | free | – | – |

**AI per-action estimate.** cost = input tokens × input price + output tokens × output price.
- Output is capped by `max_tokens`: inbox agent 1,500, drafts 1,500 (subjects 400), knowledge import 3,000.
- Input is the prompt plus business knowledge plus conversation history. Measure it on a few real calls until finding 11 is fixed.

Where the figures go in the app:
- `/manage/plans/costs/` sets `UnitCost` (email, ai_action) and `CostSettings` (fixed cost per business, target margin).
- Storage and payment fees have no driver yet. Add them to the fixed cost per business until they do.

---

## 6.3 Margins

**Formula** (per business, per month):

```
revenue      = price_monthly × period multiplier        (annual 0.85, two-year 0.75)
             − payment fee (percentage × revenue + fixed fee ÷ months in period)
variable     = AI actions × AI cost + emails × email cost + storage
contribution = revenue − variable − fixed cost per business
margin %     = contribution ÷ revenue × 100              (target: CostSettings.target_margin_pct, 50% today)
```

**Worst case** (every capped allowance fully used, current limits):

| Plan | Revenue (monthly / annual) | AI actions/mo (max) | Emails/mo (max) | Ceiling on cost? |
|---|---|---|---|---|
| Trial | $0 | 7,000 over 14 days | 500 | yes, but every unit is a loss |
| Starter | $19 / $16.15 | 15,000 | 10,000 | yes, through the site AI ceiling |
| Professional | $49 / $41.65 | 15,000 | 50,000 | yes, through the site AI ceiling |
| Business | $99 / $84.15 | 15,000 | **none** | **no: email is uncapped** |

**Break-even AI cost per action** at a 50% target margin, assuming email, storage, fees and fixed costs are zero (so the real break-even is lower):

| Plan | Revenue for AI at 50% (monthly) | Break-even per action at 15,000/mo |
|---|---|---|
| Starter | $9.50 | **$0.00063** |
| Professional | $24.50 | **$0.0016** |
| Business | $49.50 | **$0.0033** |

How to read it:
- If one AI action costs more than these figures (multiply your model's per-token prices by the tokens in one call), a heavy user loses money on that plan.
- Example: an action with 3,000 input tokens at $3 per million input tokens costs $0.009 before output. That is about 14× Starter's break-even.

**Typical vs heavy user.** Fill in once 6.2 is done, using real usage from `/manage/plans/costs/`, "Businesses by margin".

| Plan | Typical: AI/mo, emails/mo | Typical margin | Heavy: AI/mo, emails/mo | Heavy margin | Loses money? |
|---|---|---|---|---|---|
| Starter | ___ , ___ | ___ % | 15,000, 10,000 | ___ % | ___ |
| Professional | ___ , ___ | ___ % | 15,000, 50,000 | ___ % | ___ |
| Business | ___ , ___ | ___ % | 15,000, ___ | ___ % | ___ |

---

## 6.4 Proposed matrix (to be written once 6.2 and 6.3 are filled in, then approved)

Agreed principles:
- Every plan has every supported channel (WhatsApp, Instagram, Facebook publishing, Email), plus Content Studio, AI answers and core workflows. Higher tiers raise allowances; they never remove a channel.
- **Records** (customers, leads, orders) are never refused. `contacts` becomes SOFT (warn only) or is removed.
- **Cost-bearing use** (AI actions, emails, storage) gets a stated monthly allowance per tier, sized from 6.3. "Included" never means uncapped cost.
- **Capacity** (seats, active workflows or workflow runs, publishing volume or connected social accounts, advanced reporting) scales by tier only where it costs more or is worth more.
- At a limit: warn at 80% and 100% (already built), explain the impact, offer the next step. Customer conversations never stop silently, and there are no surprise overages.

Questions the matrix must answer per tier:
1. What can I accomplish on this plan?
2. What runs out first as I grow?
3. Why is the next plan worth the difference?

**Gate:** no change to `seed_plans`, `PlanFeature`, `PlanLimit` or prices before this section is approved.
