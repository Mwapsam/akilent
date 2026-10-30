# Onboarding spec: from signup to first paid order

Status: **Phase 1 spec approved 2026-10-30.** Nothing in this document is built yet. **Approving this spec doesn't authorise implementation:** each Phase 2 slice needs its own explicit go-ahead, and ships only behind its release gate (§11).

Date: 2026-10-30. Evidence: code read at HEAD `28dd988` (clean tree); live check of `/signup/` on the local server (1280×860, light); UI-quality screenshots `docs/design/qa/phase3/onboarding-390.jpg` and `signup-last-step-390.jpg` (390, dark).

## 0. The goal and the three measurements

**Activation** means a business has managed a real customer from conversation to paid order in Akilent:

> conversation → contact → lead → order → paid

Three measurements, never merged in the product UI, the operator console or pilot reports:

| Measure | Definition | Source |
|---|---|---|
| **Setup completion** | Every required "Get set up" step is done | `get_state(account)["complete"]` (`apps/accounts/onboarding.py:893`) |
| **Demonstrated activation** | The owner ran the chain on their **own test contact** (`Contact.is_test=True`) | `OnboardingMilestone` rows with `kind=self_test` (§3) |
| **Real activation** | The same chain on a contact with `is_test=False` | `OnboardingMilestone` rows with `kind=real` |

A self-test never counts towards real activation.

### Production constraints (as of 2026-10-30)

These override anything below that assumes otherwise.

| Constraint | Effect on this spec |
|---|---|
| **Email is not available in production.** AWS refused SES access, so the Email channel stays off. | Signup only offers channels from `public_channels()`, and onboarding drops channels the server can't offer (`_channel_plan`, `apps/accounts/onboarding.py:54-74`), so no code change is needed to hide it. Everything email-specific here (the address step §6, the Email column in §7, D2) is **parked until email returns**. Slice 2A must not show email copy on the auth shell, welcome page or checklist while it's off (messaging.md §9). |
| **Akilent doesn't handle businesses' payments** (product decision, 2026-10-30). Akilent covers marketing, conversations, leads, orders as a record and insights. It never collects money for a business. | **Order payment links are removed** (slice 2-0, §11). An order is marked paid **by the business**, by hand (cash, MoMo, bank, anything), and Akilent only records it. Activation's last step is "order marked paid". Revenue in Insights and post results keeps working, because it reads `Order.status=paid` / `paid_at`. PayPal stays as Akilent's own subscription billing only (`apps/billing/providers/paypal.py`). |
| **P1, a live truth bug, fixed by slice 2-0:** "Request payment" in production (no `FLUTTERWAVE_SECRET_KEY`) moves the order to *Awaiting payment*, saves a payment with no link, and still shows **"Payment link created."** (`apps/commerce/services.py:144`, `apps/commerce/views.py:154-167`). | Removing payment links removes the bug. Until 2-0 ships, nothing in onboarding may point at "Request payment". |

Scope: onboarding for **existing** features, inside the pilot freeze. Out of scope: new integrations, an AI onboarding agent, booking flows, seeded sample data, social sign-in, email digests. §12 lists the decisions this spec needs from you, including two that touch the freeze.

---

## 1. Current-state audit

### 1.1 Signup (`/signup/`)

| # | Finding | Evidence |
|---|---|---|
| S1 | The details step is one wall of **17 visible inputs** (personal, business, address). At 1280×860 the page is 1352px tall in a 639px viewport, about 2.1 screens. | live check; `templates/accounts/signup.html:136-184` |
| S2 | It asks for data Akilent doesn't use at that point: legal name, website, industry, company size, and the full address. The address is only needed to send an email campaign, and that is already enforced at a single choke point. | `apps/email/services/bulk.py:47-51`; `apps/accounts/views.py:256` (`_ACCOUNT_PROFILE_FIELDS`) |
| S3 | The Review step repeats what the user just typed and adds a click. | `signup.html:186-212` |
| S4 | "Continue" stays disabled until every required field has text, with no hint about what's missing, and it ignores format (a malformed email passes). | `static/js/signup-wizard.js:187-197` |
| S5 | No client-side per-field validation. Errors only appear after a full POST, which comes back as a 400 re-render. | `apps/accounts/views.py:501-527` |
| S6 | After a server error, nothing summarises the errors and focus doesn't move. Changing steps doesn't move focus or scroll either. | `signup-wizard.js:82-100, 199-207` |
| S7 | Only `password1`/`password2` have an `autocomplete` attribute, so name, organisation, phone and address can't be autofilled. Phone is `type="text"`. | live check; `templates/components/form_field.html:5-11` |
| S8 | Country is free text, so the stored data is inconsistent. | `apps/accounts/forms.py:115` |
| S9 | The required-field asterisk in the shared component is `text-red-500`, about 3.8:1 contrast, which UI-quality Entry 4 already flagged. | `form_field.html:4` |
| S10 | On a phone the stepper loses its labels (numbered circles joined by "—"). On a phone the cookie banner covers the terms line under "Create account". | `signup.html:19-33`; `signup-last-step-390.jpg` |
| S11 | Channel cards are text only. Plan cards don't show which of your chosen channels each plan covers. | `signup.html:58-71, 105-125` |
| S12 | Login and the password-reset pages are separate centred cards that don't look like part of the same flow. | `templates/auth/login.html`, `templates/auth/password_reset*.html` |

### 1.2 First minutes after signup

| # | Finding | Evidence |
|---|---|---|
| F1 | A free signup lands on the first setup page, and the welcome tour auto-opens on top of it, so there are two intros at once. | `apps/accounts/views.py:536-545`; `templates/components/welcome_tour.html:28-36` |
| F2 | **Email verification doesn't gate anything.** Accounts are usable straight after signup, and verification is only a checklist step, so its "Next up" spotlight competes with connecting a channel. **Decision (2026-10-30): a user can't use the workspace until the email is verified** (§2.2a). | `apps/accounts/views.py:387-399` (docstring: "surfaced as an onboarding step rather than gating access"); `apps/accounts/onboarding.py:466-477`; `email_verified` is read nowhere else except the resend view (`views.py:673, 699`); `onboarding-390.jpg` |
| F3 | Every channel picked at signup is a **required** essential. A business that ticked WhatsApp and Instagram can't finish setup while Instagram is pending, even though WhatsApp alone already gives value. | `onboarding.py:115-157` (`advance_onboarding`), `:478-513` |
| F4 | Business discovery ("Tell Akilent about your business") exists and is used by AI and automation, but it sits in the Build area as an optional recommendation. | `onboarding.py:681-692`; `apps/automation/build_views.py:95-130` |
| F5 | No record of *when* anything first happened. Every "done" is a live query, so there's no time-to-value and no funnel. | `onboarding.py:220-229` (`_exists`), `:893-1008` |

### 1.3 The journey after setup

| # | Finding | Evidence |
|---|---|---|
| J1 | The conversation → lead → follow-up → paid nudges already exist, one at a time and dismissible per workspace. | `onboarding.py:1063-1111` (`NEXT_ACTIONS`) |
| J2 | **An order can only become paid through the Flutterwave payment-link webhook**, and Flutterwave isn't configured in production. There's no way for a business to mark an order paid for cash, MTN MoMo, Airtel Money or bank transfer (the methods the business profile offers). **Decision (2026-10-30):** payment links go; the business marks orders paid by hand (slice 2-0). | only `apps/billing/providers/flutterwave.py:256` → `apps/commerce/services.py:165` (`mark_paid`); `apps/commerce/views.py:148-187` has only `request_payment`; `apps/accounts/profile.py:12-18` |
| J3 | Leads can open automatically when a customer's message asks to buy, so `first_lead` can happen without the owner doing anything. | `apps/conversations/services.py:405-440` (`capture_opportunity`) |
| J4 | Message-yourself **already exists for WhatsApp**: the setup console asks the owner to message the number from their phone, the test send is pre-filled with that phone, and the recipient is stored. Nothing marks that contact as a test. | `apps/whatsapp/setup.py:100-185`; `apps/whatsapp/models/verification.py:22` (`recipient`, E.164); `apps/whatsapp/models/tenant.py:148-164` |
| J5 | Email isn't an inbox channel, so an **email-only** business can't take the conversation journey at all. | `docs/marketing/messaging.md` §7 |
| J6 | `Message` has no author field, so a reply can't be credited to the member who typed it. | `apps/conversations/performance.py` docstring |

### 1.4 Not walked in this pass (needs a seeded workspace)

The signed-in first-run screens (inbox, contact, Sales, Orders, Insights with zero data) were reviewed from their templates (§8), not walked live. A Phase 2A gate (§11) covers them with `tools/qa/accounts_qa.mjs`.

---

## 2. Screens and journeys

The stages run in order. Every screen must meet the existing UI-quality bar: tokens only, both themes, 390 and 1280, no field under 16px on phones, focus visible, 0 contrast failures.

### 2.1 Auth shell (signup, login, reset, 2FA, verify-email)

- New `templates/auth/_shell.html`, which extends `base.html` (public shell). At `lg+` it's two columns.
  - **Left:** the form column, max-w 28rem (signup 36rem), with the brand mark top-left and the "Already have an account? / New here?" link top-right.
  - **Right:** the brand panel, block `auth_panel`. Default content is the lost-enquiries promise "Stop losing customers in your inbox." (claim register `lost-enquiries`) plus three journey lines (Engage / Convert / Measure, messaging.md §3). Channel names appear only while that channel is on.
- Below `lg` the panel is hidden. Signup shows a collapsible summary bar instead (§2.2).
- Pages moved onto the shell, markup only (forms, URLs and views unchanged): `auth/login.html`, `auth/password_reset.html`, `password_reset_done.html`, `password_reset_confirm.html`, `password_reset_complete.html`, `auth/2fa_verify.html`, `accounts/resend_verification.html`, `verify_email*.html`.
- **Acceptance:** the claims test covers the shell copy; the cookie banner never covers a primary action or the terms line (S10).

### 2.2 Signup wizard: three steps

| Step | Fields | Notes |
|---|---|---|
| 1 Channels | channel cards, 2-column grid, with icons (`chat` WhatsApp, `instagram`, `facebook`, `mail`) | Same `toggleChannel` logic; only channels from `signup_offer()` are shown |
| 2 Plan | segmented term toggle, equal-height cards | "Most popular" ribbon; chips for the chosen channels each plan covers; trial badge |
| 3 Your account | first name, last name, email, phone, password, confirm password, business name | Terms line; **Create account** (a paid plan opens the existing confirm dialog) |

- **Removed from signup:** legal name, website, industry, company size and the address. They stay editable in Settings → Business (`BusinessAddressForm`, `apps/accounts/forms.py:314`). Industry and size move to discovery (§2.3); the address to §6.
- **Right panel = live summary:** channels with icons, plan, price per term, savings, trial, and "What happens next" (free: set up your first channel; paid: you pay, then set up).
- **Progress:** a bar plus "Step 2 of 3 · Plan" at every width, with an `aria-live` announcement. Done steps stay clickable.
- **Validation:** Continue checks the current step. Missing or invalid fields get inline errors and focus moves to the first one; fields re-check on blur once touched.
  - Rules: email shape; password ≥ 8 and not all digits; passwords match; phone has ≥ 7 digits.
  - The server stays the authority.
  - After a server re-render: an error summary with links to each field, focused. Error routing: `selected_channels` → 1, `plan`/`billing_period` → 2, everything else → 3.
- **Fields:**
  - `autocomplete` tokens: `given-name`, `family-name`, `email`, `tel`, `organization`, `new-password`
  - phone is `type="tel"` with the help text "Include your country code, e.g. +260 97 123 4567"
- **Server:** `SignupForm.clean_phone` normalises to E.164 with `phonenumbers` (already a dependency). If parsing fails, the value is kept as typed rather than rejected. E.164 is needed to recognise the owner's own messages (§4).
- **Draft:** non-secret values, channels, plan and period go to `sessionStorage` (wrapped in try/catch) and are restored when the server sent no form data. Passwords are never stored, and the draft is cleared on submit.
- **Submit:** spinner and "Creating your account…", using the existing 15s lock.
- **Unchanged:** `?plan=` / `?channels=` / `?period=` deep links (`startStep` 2 → step 3), the Enter-key behaviour, and the paid confirm dialog.
- **Component changes** (`form_field.html`): optional `autocomplete`, `inputmode`, `help` params; the asterisk changes to `tone-coral-fg` (S9). Every caller benefits.

### 2.2a Email verification gate and payment order (decided 2026-10-30)

**Rule:** until `Account.email_verified` is true, the owner can't use the workspace. Signup still creates the user, account and membership in one POST and signs them in. Every workspace page then shows one **"Check your email"** screen instead.

**Journeys**

| Free plan | Paid plan |
|---|---|
| 1. Sign up | 1. Sign up |
| 2. Check email | 2. Pay (PayPal) |
| 3. Verify | 3. "Payment received. One last step: confirm your email." |
| 4. Welcome + discovery (`/welcome/`) | 4. Verify |
| 5. Setup | 5. Welcome + discovery |
| | 6. Setup |

Payment and verification are **separate gates**: paying never verifies, verifying never activates a plan.

**Who owns what**

| Akilent owns | PayPal owns |
|---|---|
| Signup, workspace creation, starting the payment (`billing:pay` → `billing:checkout` → `provider.start_checkout`), handling the return, **confirming payment state on the server**, email verification, onboarding access control | The checkout UI, payer details, authorisation and capture, and notifying Akilent (webhook and the Orders/Subscriptions API) |

Akilent builds no checkout of its own. **Akilent's database is the source of truth** for whether a payment succeeded (`BillingPayment.status`) and whether the plan is active (`Subscription.status`).

**A PayPal return URL is not proof of payment.** That's already how `payment_return` works (`apps/billing/views.py:486-539`): it looks only at the caller's own account's payment, asks the provider to verify it with Akilent's credentials (`apps/billing/providers/paypal.py:10-13`, "Nothing PayPal sends is trusted as-is"), and treats it as paid only when the stored payment is `SUCCEEDED`. Otherwise it shows the pending page, which polls `billing:payment-status`. The check-email screen says "Payment received" **only** when the account's subscription is active and its latest `BillingPayment` is `SUCCEEDED`, read from the database. Query strings, the referrer and client-side state never count.

**Middleware.** `VerifiedEmailMiddleware`, next to `SuspendedAccountMiddleware` and using the same pattern (`apps/core/middleware.py:249-280`): reuses `_open(path)` and `_wants_json` (JSON 403 `{"error": "Verify your email address to continue."}`); operators are exempt; requests with no signed-in user are never touched (so webhooks and public pages pass).

**Allowed while unverified (allowlist by URL name, not by prefix):**

| Always | Only while the subscription is INCOMPLETE (paid plan not yet paid) |
|---|---|
| `verify-email`, `resend-verification`, the new `change-email`, `logout`, legal pages, `/help/`, static/media, `ux-event` | `billing:pay`, `billing:checkout`, `billing:return`, `billing:payment-status`, `billing:manual-submit` (only if the manual method is on) |

- **Never allowed while unverified**, even though they sit under `/billing/`: `billing:cancel` (cancels a *subscription*, not a checkout), `billing:resume`, `billing:plans`, invoices, payment-method and plan changes. Provider webhooks (`billing:webhook-provider`, `billing:webhook`, `billing:stripe-webhook`) are server-to-server with no session, so the gate never sees a user on them. A test proves they're processed for an unverified account.
- **Cancelled or failed checkout:** PayPal's `cancel_url` and the failed-payment redirect both point at `/billing/plans/` today (`paypal.py:211`, `views.py:516-523`), which the gate blocks. For an unverified account with an INCOMPLETE subscription, `billing:plans` redirects to `billing:pay` with "The payment didn't go through. You haven't been charged." Nothing about a cancel or failure changes `Subscription.status`. It stays INCOMPLETE, and the existing return handling already guarantees that.
- **After payment succeeds:** `_after_payment` (`views.py:477-482`) keeps redirecting to `first_setup_url`; the gate intercepts and shows the check-email screen with "Payment received", and once verified the user goes to `/welcome/`.
- **Abandoned payment:** signing in again goes to `billing:pay` (as today), never to the check-email screen.

**The check-email screen** (auth shell, §2.1):
- "We sent a link to **{email}**. Open it to start using {app}."
- **Resend link**, with a **new** rate limit (5 per hour per account; none exists today), reusing `resend_verification` (`apps/accounts/views.py:~650-700`).
- "Open Gmail / Outlook" shortcuts when the domain matches.
- A light poll (every 5s, for up to 10 minutes) moves on automatically once the link is opened on another device.
- **Wrong address? Change it.** Rules:
  1. The new address is validated as an email and normalised the same way as at signup.
  2. It must not belong to another user (`email__iexact`, the same check as `SignupForm.clean_email`); otherwise "An account with this email already exists."
  3. One transaction updates `User.email`, `User.username` (`email[:150]`, as signup does) and `Account.billing_email`, logs an `account.email_changed` event (old and new address, actor), and resets `email_verified` to false.
  4. A new link goes to the **new** address. Links already sent to the old address stop working: the token hashes the email (`apps/accounts/tokens.py`).
  5. **Changing the address never counts as verifying it.**
  6. Ownership and billing don't move: same `User`, `Account`, `Membership`, `Subscription` and `BillingPayment` rows; nothing is created or re-linked. PayPal's own receipt went to the payer's PayPal email, so nothing is lost there.
  7. Allowed only while unverified, and from this screen; after verification, email changes go through the existing profile settings.

**Opening the link:**
- signed in: continue to `/welcome/`, or to `billing:pay` if the plan is still unpaid
- signed out, or on another device: mark verified (as today, `views.py:608-635`), then "Email confirmed — sign in to continue", with no auto-login (the token isn't a login credential)

**Other rules:**
- **Invited teammates** aren't gated: accepting an invitation already proves they own their address, and the workspace they join was verified by its owner.
- **Checklist:** `verify_email` leaves "Get set up", because nobody reaches the checklist unverified. `account` stays the first done step. This replaces the earlier D3 recommendation.
- **Milestone:** `email_verified` (new key, `kind=real`, `first_at = email_verified_at`), recorded in `_mark_email_verified` (`views.py:244-253`).

**Prerequisite (release blocker): production email delivery.** System mail (verification, password reset, payment and billing mail) goes through `SYSTEM_EMAIL_PROVIDER_BACKEND`, falling back to `EMAIL_SEND_PROVIDER_BACKEND` (default `smtp`; `automator/settings.py:468-473`, `apps/email/providers/__init__.py:135-145`). That's separate from the tenant Email channel, which stays off. Sending successfully from the application doesn't prove delivery: SES in the sandbox (AWS refused production access) "sends" but only delivers to pre-verified addresses. Before 2A ships, **all of these must be true, checked by hand and logged in `docs/design/ui-quality-log.md`:**
- [x] The production system-mail backend is named: **SMTP (`apps/email/providers/smtp.py`) through Mailtrap Email Sending** (confirmed by you on 2026-10-30). Not SES, so the SES sandbox lockout doesn't apply. The tenant Email channel stays off; system mail doesn't depend on it.
- [x] Production `EMAIL_HOST` is `live.smtp.mailtrap.io` (confirmed by you, 2026-10-30) (or `bulk.smtp.mailtrap.io`), **not** `sandbox.smtp.mailtrap.io`. The sandbox is Mailtrap's testing inbox: it accepts mail and delivers nothing, which would lock out every new signup. (The local `.env` already uses `live.`.)
- [ ] The Mailtrap account is cleared to send to any recipient (domain compliance review passed, no sending restriction), and its monthly plan quota covers signups plus resends.
- [ ] The sending domain shows **Verified** in Mailtrap (its SPF, DKIM and DMARC DNS records in place), and SPF, DKIM and DMARC pass in the headers of a received message. `DEFAULT_FROM_EMAIL` uses that domain.
- [ ] A verification email reaches a fresh **Gmail** and **Outlook** inbox (not spam), and its link works.
- [ ] Password reset and the PayPal payment-received mail still arrive the same way.
- [ ] Failures are visible: a send error leaves the user on the check-email screen with "We couldn't send the email just now", is logged, and Resend works. Bounces and spam complaints are suppressed **by Mailtrap**, not by Akilent: Akilent's suppression list doesn't see them (no Mailtrap webhook exists; adding one is out of scope). So a user whose address bounced once will silently get nothing on Resend. The check-email screen says "Not arriving? Check spam, or change the address", and support checks Mailtrap's suppression list when someone reports no email.

### 2.3 Welcome and business discovery (`/welcome/`, new)

- **Shown once.** Reached from the free branch of `after_signup_url()`, and from the post-payment redirect (`apps/billing/views.py:481`) for paid plans. It redirects to the dashboard once setup is complete or discovery has been answered or skipped.
- **Content:**
  - headline "Welcome, {first name}. Let's get {company} ready."
  - one line on what happens next
  - **discovery**, every question skippable:
    - business type: Retail / e-commerce · Services · Food & hospitality · Other
    - what do you sell? (`BusinessProfile.what_you_sell`)
    - team size: Just me · 2–5 · 6–20 · 20+ (`Account.company_size`)
    - which channel do customers use most? (only when more than one was picked)
  - Buttons: **Continue to setup** (saves, then `ob.first_setup_url`) and "Skip for now".
- **Model:** `BusinessProfile.business_type` and `BusinessProfile.primary_channel` (blank defaults), saved by a new `profile.save_discovery()` that **never** sets `completed_at`, so the full questionnaire step stays honest.
  - `primary_channel` goes first in `SETUP_ORDER` for that account.
  - `business_type` picks the wording of the "Your first customer" section (§2.4); retail is the default.
- **Tour:** visiting `/welcome/` marks the welcome tour as seen (`Membership.welcome_tour_seen_at`), so it no longer pops up over setup (F1). It stays replayable from the setup widget.
- **Milestone:** `discovery_done` when at least one discovery answer is saved. Skipping doesn't record it.

### 2.4 Checklist: setup and activation side by side

`/onboarding/` and the dashboard show **two progress indicators**, never combined:
1. **Get set up** (setup completion), as today, with these changes:
   - **At least one channel required (F3):** connecting any one chosen channel completes the channel requirement. The other chosen channels become optional "Connect {channel}" steps in the same section.
     - `advance_onboarding` moves to COMPLETED once one channel is ready, with no paused channel *among those still required*.
     - If the only chosen channel is paused, the existing paused behaviour is unchanged.
   - **Email verification** is no longer a checklist step: it gates the workspace instead (§2.2a).
2. **Your first customer** (activation): a new section at the top of `SECTIONS`. Each step is backed by a milestone (§3) and worded by `business_type`:

| Step (retail wording) | Services wording | Done when |
|---|---|---|
| Get your first message | Get your first enquiry | `first_conversation` |
| Reply to it from the inbox | same | `first_reply` |
| Mark them interested | Track them as a potential customer | `first_lead` |
| Record their order | Record what they've agreed to buy | `first_order` |
| Mark it paid | same | `first_paid_order` |

  - On accounts with WhatsApp, each step shows "Practise with your own phone" until a real customer arrives (§4, D5). Steps done on the test contact show a "Practice" tag, and the ring reads "Practised · 5/5" vs "Real customers · 2/5".
  - The dashboard leads with this card until **real** activation. After that it folds into the usual work queue.
  - Email-only accounts: see decision D2 (§12).

### 2.5 Contextual guidance (Phase 2E)

`components/guide_hint.html`: an inline card on the page, never a modal, with **Show me** and **Later**. Dismissed per user (`Membership.dismissed_guides`, a JSON list of keys).

| Trigger (from milestone state) | Where | Says |
|---|---|---|
| `first_conversation` recorded, no `first_reply` | Inbox | This is the customer's conversation; reply here and the team sees it |
| A contact opened, no `first_lead` | Contact panel | Mark them interested so they're not forgotten |
| `first_lead`, no follow-up | Conversation | Set a reminder to follow up |
| `first_order`, not paid | Order detail | When the customer pays you, mark the order paid so it counts in your results |
| First visit to Insights | Insights | What each tile counts, and that it's lower bounds |

Hints never block an action and never appear for a feature the plan lacks (the same `ctx.usable` check as `NEXT_ACTIONS`).

---

## 3. Milestones

### 3.1 Model

`accounts.OnboardingMilestone`:

| Field | Notes |
|---|---|
| `account` | FK, CASCADE |
| `key` | one of the keys below |
| `kind` | `real` / `self_test` |
| `first_at` | when the event happened (the event's own timestamp, not the insert time) |
| `source` | `event` / `observed` / `backfill` |
| `version` | int, starts at 1. A new definition bumps the version, and old rows stay valid. |
| `metadata` | JSON: `user_id` when known, `subject` (public id), `channel`, `auto` (e.g. an auto-opened lead) |

`UniqueConstraint(account, key, kind)`.

### 3.2 Recording rules

- `record_milestone(account, key, *, kind, at, source, **meta)` in `apps/accounts/milestones.py`:
  - runs inside `transaction.on_commit` from the caller, so it only fires after the triggering state is saved
  - `get_or_create`, catching `IntegrityError`, so concurrent emits leave exactly one row
  - catches and logs every exception; never raises into the caller
- `kind` comes from the contact: `self_test` if `contact.is_test`, else `real`.
- Workspace-owned: any member's action counts and every member sees it. `metadata.user_id` is filled where the request has a user. Replies can't be credited per person (J6), so it stays empty there.

### 3.3 Definitions

| Key | Emitted at | Only when | Never when |
|---|---|---|---|
| `discovery_done` | `profile.save_discovery()` (new) | at least one answer is non-empty | skipped |
| `channel_connected` | **observed**: in `advance_onboarding` / `get_state`, the first time the channel's existing done-check is true (`_has_ready_whatsapp_number` / `_whatsapp_step` done, `_has_ready_instagram`, `_has_facebook_page`, `_has_sending_domain`, `onboarding.py:77-112`) | the check is true | a connection attempt failed. Readiness is computed (token expiry, registration), and no single write marks "became ready", so it's observed rather than emitted. `source=observed`, `kind=real`. |
| `first_conversation` | `record_inbound_whatsapp_message` / `_instagram_` / `_facebook_` (`apps/conversations/services.py:97, 199, 550`) | the `Message` row was **created** (not a replay; the `if not created` check at `:143`) | a replayed webhook (`created=False`) |
| `first_reply` | `record_message_status` (`services.py:719`) when a person's outbound message (no `metadata.sent_by`) first reaches `sent`/`delivered`/`read` | provider acceptance | automation, AI, campaign or form sends; status `failed`; a status replay |
| `first_lead` | `crm.services.create_lead` (`apps/crm/services.py:22`) after the Lead row is created | a new row (not the existing open lead returned) | `create_lead` returning an existing lead. `metadata.auto=True` when it came from `capture_opportunity`. |
| `first_order` | `commerce.services.create_order` (`apps/commerce/services.py:20`) after commit | always (new row) | — |
| `first_paid_order` | `commerce.services.mark_order_paid` (new in slice 2-0, §11.1), on the path where the order actually moves to PAID | the stored status changed from an open state to PAID under `select_for_update` | a second click or a second member marking the same order (already PAID: no-op); an order already refunded or cancelled; "Mark as not paid" afterwards (the milestone stays, because it happened) |

After slice 2-0 the only way an order becomes paid is the business marking it, through `mark_order_paid`. The post-transition work `mark_paid` does today (the `order.paid` event, `order.paid` workflows, the note in the conversation, `_record_order_paid_outcome_signal`, `apps/commerce/services.py:215-245`) moves into a shared `_after_order_paid(order)` that `mark_order_paid` calls, so Insights, post results and automations behave as they do now.

**Real activation** = `first_paid_order` with `kind=real`. **Demonstrated activation** = `first_paid_order` with `kind=self_test`.

### 3.4 Backfill

A management command, `backfill_onboarding_milestones`, idempotent and safe to re-run:
- For each account, it finds the earliest qualifying row per key (inbound `Message`, person outbound with status sent+, `Lead`, `Order`, `Order` with `status=paid` → `paid_at`) and records it with `source=backfill` and `kind` from the contact.
- `channel_connected` is recorded with `first_at` = the earliest `WhatsAppConnectionTest` sent, IG/FB connection `created_at`, or `EmailDomain.created_at`, whichever applies.

---

## 4. Message yourself

### 4.1 Normal path

1. Connect a channel (setup completion).
2. "Your first customer" offers **Practise with your own phone**, **on WhatsApp only** (D5). It reuses the setup console's "Message this number from your phone" (`apps/whatsapp/setup.py:100-127`, with the `wa.me` link and `status_url` polling). An account without WhatsApp sees no practice option; its "Your first customer" steps wait for a real customer.
3. The inbound message creates a conversation and contact through the normal pipeline. The contact is marked `is_test=True`:
   - **WhatsApp, automatic:** the sender's phone equals the owner's signup phone (E.164, §2.2), or the recipient of any of the number's connection tests (`verification.py:22`).
   - **WhatsApp, manual correction:** "This is me (practice)" / "Not me" in the contact's menu, on WhatsApp contacts only, for the cases auto-detection misses (a second phone) or gets wrong. There's no practice labelling on Instagram or Facebook: an owner who DMs their own account anyway is counted as a real contact. That's accepted (D5).
4. The owner replies, marks themselves interested, records an order, then marks it paid (slice 2-0).
5. Milestones are recorded with `kind=self_test`, and the checklist shows "Practised".

### 4.2 Recovery

| Problem | What the user sees | Behaviour |
|---|---|---|
| The message never arrives (polling times out after 2 minutes) | "We haven't seen your message yet." Then a checklist: is it the right number (show the display number); was it sent to the business number, not a personal chat; webhook status from the setup console; a link to `/help/whatsapp-setup/` | A **Try again** button restarts polling. Nothing is recorded. |
| The test send fails | The console's existing error, plus "Send test message" again | Uses the existing `numbers_verify` (`apps/whatsapp/numbers.py:533`); failed tests never mark `is_test` or record a milestone |
| Messaging from a second phone | It becomes a new contact | It isn't auto-marked (doesn't match). The owner can use "This is me". |
| Duplicate contacts | — | Can't happen per phone: `UniqueConstraint(account, phone)` on Contact (`apps/contacts/models.py:120-121`); IG/FB are matched by their channel identity |
| Contact marked test by mistake, or un-marked | "Not me" / "This is me" toggles it | Re-kinds that contact's milestones: deletes the rows that came only from it and re-derives them from the remaining evidence with the backfill logic, never creating duplicates (unique constraint) |
| A test contact later becomes a real customer (a friend testing) | — | Un-mark it, as above; its history then counts as real |

### 4.3 Benchmark timing (blocking for 2C)

The Starting-point benchmark measures the **first 7 days after WhatsApp connects** and is never recomputed (`apps/conversations/benchmarks.py`). The weekly snapshots are also immutable (`apps/conversations/snapshots.py`). Self-testing happens exactly in that window, so **`is_test` has to be set when the message arrives**, not later. That's why WhatsApp auto-marking is required, and why the benchmark and snapshot capture must exclude test contacts **at capture time**. A manual "This is me" made after capture can't correct an already-frozen benchmark. The UI says so: "Practice messages from before you marked this contact may still appear in your Starting point."

---

## 5. Analytics exclusion audit

**Rule:** business-performance numbers exclude `is_test` contacts and everything that hangs off them (conversations, messages, leads, deals, orders, payments). **Operational lists keep them**, with a "Practice" badge, so the owner can still find and close their own test thread.

Files that aggregate contacts, messages, leads, orders or payments, from a grep for `.count()/.aggregate()/.annotate()` over modules touching those models (counts per file in brackets):

| File | Use | Exclude test? |
|---|---|---|
| `apps/conversations/reporting.py` [36] | Business Health / Insights: response, resolution, recovery, sales, revenue, funnel, Instagram funnel | **Yes**, every function |
| `apps/conversations/api.py` [7] | `funnel`, `revenue_by_channel`, `pilot_scorecard`, `window_metrics`, `first_reply_waits` | **Yes** |
| `apps/conversations/benchmarks.py` | Starting point | **Yes, at capture** (§4.3) |
| `apps/conversations/snapshots.py` | Weekly momentum | **Yes, at capture** |
| `apps/conversations/weekly_report.py` | Owner email | **Yes** (reads `reporting`) |
| `apps/conversations/performance.py` [4] | Follow-up completion, team table | **Yes** |
| `apps/conversations/state.py` [7] | Waiting / missed states | No (operational), but the work-queue *counts* exclude |
| `apps/conversations/views.py` [18] | Inbox tab counts and lists | Lists: no (badge). Tab counts: no (the owner should see their open test thread) |
| `apps/accounts/views.py` [8] | Dashboard work queue and panels | Panels' stats: **yes**. Work-queue counts: no |
| `apps/accounts/pilot.py` [5] | Pilot setup score, baseline, outcome | **Yes** |
| `apps/publishing/results.py` [14] | Post → chats/leads/orders/revenue | **Yes** |
| `apps/conversations/attribution.py` [1] | Which conversation led to a record | No (attribution stays stored and true); reports filter downstream |
| `apps/crm/analytics.py` [3] | Pipeline history | **Yes** |
| `apps/insights/rules.py` [7] + `apps/insights/executor.py` | Insight triggers and outcome signals | **Yes** (a test chat must not generate an insight) |
| `apps/core/console/business.py` [5], `apps/core/console/attention.py` [2] | Operator console per-business numbers | **Yes**, shown as "real", with test counts beside them |
| `apps/whatsapp/api.py` [8], `campaigns.py` [4], `campaign_ui.py` [1] | Campaign audience and stats | Audience: **yes** (never campaign to the owner's test contact by default). Delivery stats: no |
| `apps/contacts/views.py` [11], `segments.py` [1], `segment_views.py` [2], `tags.py` [2], `reachability.py` [3] | Contact list, segments | List: no (badge). Segment *counts used for campaigns*: **yes** |
| `apps/api/contact_views.py` [4], `apps/api/views.py` [2] | Public API | No (the API returns records); add `is_test` to the serializer |
| `apps/automation/views.py` [4], `api.py` [4], `workflow_engine.py` [1] | Run stats | No (automation really ran); stats screens label practice runs |
| `apps/contacts/erasure.py`, `merge*.py`, `apps/billing/credit.py`, `apps/whatsapp/delivery.py`, `verification_codes.py`, `apps/core/middleware.py`, `apps/email/*` | Housekeeping / unrelated | No |
| `apps/accounts/onboarding.py` [1] (`NEXT_ACTIONS`, `_done_contacts`) | Nudges | Real/self-test split per §2.4 |

**Implementation pattern:** a single `Contact.objects.real()` queryset method and a `real_contacts_q(prefix)` helper for joins (`conversation__contact__is_test=False` etc.), so each report changes one filter, not hand-written conditions.

**Historical behaviour:** reports read the **current** flag, so marking a contact as a test removes its past rows from live reports, and un-marking restores them. Attribution totals recalculate the same way. Frozen benchmarks and snapshots don't change (§4.3).

**Gate test:** a parametrised test seeds a test contact with a conversation, a lead, an order and a payment, then asserts that every "Yes" row above returns the same numbers as without that contact.

---

## 6. Business address

**Parked while email is off in production.** No account can choose email, so this step never appears; build it when email returns.

**Why:** CAN-SPAM requires the sender's postal address in every campaign email footer. `bulk.create_campaign` refuses a campaign without one; that's the single choke point for the UI and the API (`apps/email/services/bulk.py:47-51`, `has_postal_address` at `apps/email/services/compliance_footer.py:47`).

**Decision:** the address leaves signup. It becomes an **optional, explained** step, "Add your business address", in "Get set up" **for accounts with the email channel only**:
- Description: "Required by law in marketing emails. We print it in every campaign footer."
- Done when `account.has_postal_address`; links to Settings → Business (`settings-business`, `BusinessAddressForm`).
- It isn't a required essential, and it never appears for WhatsApp/Instagram/Facebook-only accounts.
- When a campaign is blocked for a missing address, the existing `MissingPostalAddressError` message links to the same settings page.

---

## 7. Channel connection content

One component, `components/channel_connect_explainer.html`, on each setup page, filled from a per-channel dictionary in `apps/core/channels.py` (beside `CHANNEL_REGISTRY`). It's shown only for channels in `public_channels()` or already connected. Locally only WhatsApp and Email are currently offered at signup; Instagram and Facebook are restricted pending Meta App Review.

| Block | WhatsApp | Instagram | Facebook Page | Email |
|---|---|---|---|---|
| What it enables | Shared inbox, auto-replies, campaigns | DMs in the inbox, comment replies, publishing | Page messages and comments in the inbox, publishing | Sending from your domain, templates, campaigns, API/SMTP. **Not** an inbox. |
| What we ask Meta / you for | WhatsApp Business account + phone number via Meta's guided setup | Instagram professional account login | Page access via Facebook login | DNS records at your domain host |
| What Akilent reads | Messages sent to the number, delivery status | DMs, comments on your posts, post insights | Page messages, comments, post insights | Bounces, opens and clicks on mail you send |
| Steps | Mirror `build_setup_console` steps (`apps/whatsapp/setup.py:54`) | Connect → choose account → confirm | Connect → choose Page → confirm | Add domain → add DNS → check |
| Success | "Connected. Send yourself a message to practise." | same | same | "Verified. You can send now." |
| If it fails | registration failed / token expired / no numbers returned (existing messages) | not a professional account; tester role during review | no Page access granted | DNS not propagated yet; wrong host |
| Help | `/help/whatsapp-setup/` | the existing channel guide | the existing channel guide | the domain wizard |

The exact permission names (`whatsapp_business_messaging`, `instagram_business_manage_messages`, …) are confirmed against the live Meta app configuration during 2D, not copied from memory. Copy follows messaging.md §7 ("Facebook is a Page…", "Email is sending and campaigns").

---

## 8. Empty-state inventory

70 uses of `components/empty_state.html` across 42 templates, plus the hand-built contacts state. Most already follow "what it is + when it fills". Only the **journey-critical** ones change, mostly by adding the one action they're missing:

| Screen | Current | Replacement |
|---|---|---|
| Inbox, no conversations (`conversations/_inbox_body.html:183`) | "No conversations yet. Conversations from every connected channel will appear here as customers get in touch." No action. | Same message, plus action **Practise with your own phone** (→ the §4 flow) while not really activated and WhatsApp is connected (D5); **Connect a channel** if none is connected |
| Inbox "Waiting on you" (`:185`) | Good; no change | — |
| Contacts, none (`contacts/_list_results.html:76-80`, hand-built) | "Your customers will appear here" | Move to the component: "Everyone who messages you is saved here automatically, with their conversations, leads and orders." Action **Import a spreadsheet** |
| Sales, none (`crm/sales.html:40`) | Good copy, no action | Add action **Open the inbox** ("Mark someone interested from their conversation") |
| Orders, none (`commerce/orders.html:53`) | "Turn conversations into orders. When a customer decides to buy, create the order here and send them a payment link…" No action. | "When a customer decides to buy, record the order here, and mark it paid when they pay you. Orders from the inbox or the API show up here too." Action **Create an order** (→ `commerce:create-order`). Ships in 2-0. |
| Orders "Nothing needs your attention" (`:49`) | Good | — |
| Follow-ups due (`conversations/followups_due.html:39`) | Good | — |
| Content Studio library (`publishing/library.html:149`) | Good, with action | — |
| Insights (`insights/_proof.html`, `_momentum.html`, `_goals.html`) | Per-tile "not enough data" text | Each tile says what will fill it and when ("after your first full week of conversations"), plus "Practice conversations aren't counted." |
| WhatsApp numbers (`whatsapp/numbers.html:434`) | Good | — |

Out of scope (fine as is, or not first-run): filter-empty variants, logs, webhooks, forms, routing, teams, assets, campaign recipients, the styleguide.

---

## 9. Metrics (operator console, Phase 2F)

A new console page, **Activation**, cohorted by signup week. Demo workspaces (`seed_demo_workspace` slug), operator-owned workspaces and accounts with no owner login after signup are left out and listed separately.

| Metric | Definition | Reconciliation query |
|---|---|---|
| Signup → email verified | % of signups with `email_verified`; median time to verify; % that used "change email" | `Account.email_verified_at` |
| Signup → discovery | % of accounts with `discovery_done` | `BusinessProfile` with `business_type` or `what_you_sell` set |
| Channel connected | % with `channel_connected` | live `_channel_connected` per account |
| First conversation / reply / lead / order / paid | % with each key, shown **separately for `real` and `self_test`** | earliest matching row per account, filtered on `contact__is_test` |
| Setup completion | % `get_state()["complete"]` | — |
| Time to value | median `first_at − account.created_at` per key, real only | — |
| D7 / D30 retention | % of accounts with an **active day** in days 7–13 / 30–36 after signup. Active day = at least one person outbound message (no `metadata.sent_by`, status sent+) to a non-test contact. | `Message` query |
| Signup step drop-off | Optional, decision D4: views per wizard step | `UxEvent` |

Each metric has a management-command check (`check_activation_metrics`) that recomputes from the raw tables and fails if it differs from the milestone-based number by more than 0. That's the 2F gate.

---

## 10. Failure scenarios

| Scenario | Expected | Test |
|---|---|---|
| Duplicate inbound webhook | One `Message`, one milestone | replay `record_inbound_*` twice → `created=False` second time, 1 row |
| "Mark as paid" double-clicked, or two members mark it at once | One PAID transition, one `order.paid` event, one milestone | two concurrent `mark_order_paid` calls |
| Order marked paid by mistake | "Mark as not paid" returns it to pending and takes it out of revenue; the milestone and the `order.paid` event stay (history) | `mark_order_unpaid` test |
| Old order still *Awaiting payment* from a payment link | The 2-0 data migration moves it back to *Pending* and withdraws its pending payment; old paid payments stay read-only | migration test |
| Concurrent `record_milestone` | Exactly one row | two threads / `IntegrityError` path |
| Milestone recording fails | The caller's message/order/payment is still saved | patch `record_milestone` to raise |
| Channel disconnects mid-journey | `channel_connected` stays (it happened); the setup step re-opens as today | token expiry fixture |
| Chosen channel paused | Existing paused behaviour; activation steps that need it show locked | pause fixture |
| Plan downgrade mid-journey (e.g. orders removed) | Steps for unusable features hide; recorded milestones stay | `ctx.usable` fixture |
| Two members act at once | Milestones belong to the workspace; both see them; `user_id` is whoever was first | two clients |
| Test contact later becomes real | Un-mark it, and its milestones re-kind with no duplicates | toggle test |
| Owner messages from a second phone | Not auto-marked; manual "This is me" works | fixture |
| Lead auto-created by intent | `first_lead` recorded with `auto=True` | `capture_opportunity` path |
| Email-only account | No "Your first customer" section (D2) | fixture |
| Signup server error | Lands on step 3 (or 1/2 by field); error summary focused; draft kept | form errors |
| Paid signup abandoned at payment | Account kept (current behaviour); `/welcome/` only after payment | billing tests |
| Never verifies | Workspace stays behind the check-email screen; signing in again shows the same screen; account kept | middleware test |
| Mistyped email at signup | "Change it" on the check-email screen; uniqueness enforced; new link sent; old link stops working (the token covers the email) | change-email test |
| Link opened on another device | Verified there; the original tab moves on via the poll; the other device asks to sign in | token test |
| Link opened twice / expired | Existing `verify_email_invalid.html` with Resend | existing tests |
| Verification email can't be sent | Screen says so and offers Resend; logged | patch `_send_verification_email` |
| JSON / HTMX request while unverified | 403 JSON, never a redirect loop | middleware test |
| Existing unverified account in production | Gated from release (D6); a fresh link was emailed at release; Resend works; subscription, data and PayPal renewals are untouched while gated | gate test with a pre-existing paid account; renewal webhook test while gated |
| **Direct URL access while unverified** (typed or bookmarked, not clicked) | Unverified + unpaid: `billing:pay`, `checkout`, `return`, `payment-status` load. Unverified + paid: payment URLs redirect to the check-email screen. Unverified (any): `billing:cancel`, `resume`, `plans`, invoices, every workspace URL → check-email screen (HTML) or 403 JSON | parametrised test over every URL name in `urls.py`, asserting each is either on the allowlist or gated |
| PayPal webhook arrives for an unverified account | Processed as usual; subscription becomes ACTIVE; the gate isn't involved | webhook test with an unverified owner |
| PayPal checkout cancelled, or the buyer returns without paying | Subscription stays INCOMPLETE; back on `billing:pay` with "You haven't been charged"; never "Payment received" | return test with a non-SUCCEEDED payment; plans → pay redirect test |
| Forged return (`/billing/return/paypal/?token=…` for someone else's or a made-up payment) | Pending/not-finished page; nothing activates | existing `payment_return` scoping, plus an unverified variant |
| Change email to an address another user has | Refused with the signup message; nothing changes | change-email test |
| **Full paid journey with a mistyped email** (the end-to-end regression) | 1. Sign up with a wrong address → 2. pay through PayPal (sandbox) → 3. return: "Payment received" only once the payment is SUCCEEDED → 4. change the address on the check-email screen → 5. the link goes to the corrected address; the old link is dead → 6. verify → 7. `/welcome/` → 8. finish onboarding → 9. the same `User`, `Account`, `Subscription` (ACTIVE, same plan and period) and `BillingPayment` (SUCCEEDED, same reference) as at step 2; `billing_email` is the corrected address | `tests/e2e` Django test with the PayPal provider stubbed at its HTTP boundary, plus one manual run in the PayPal sandbox on staging, logged in the QA log |

---

## 11. Phase 2 slices and release gates

Each slice ships and is reviewed on its own. All of `pytest -n 6`, `ruff check`, `ruff format --check` and `mypy .` must be green at every gate.

| # | Slice | Files (main) | Migrations | Release gate |
|---|---|---|---|---|
| 2-0 | **Remove order payment links; mark orders paid by hand** (§11.1). Also fixes P1. | see §11.1 | `Order.paid_method`, `paid_reference` + data migration | No "payment link" / "Request payment" left in templates, code paths, nudges, starter workflows, help or marketing copy (grep check in the test suite); revenue in Insights and post results unchanged for orders marked paid; claims test updated; existing paid orders and payments still display |
| 2A | Signup trim, **email verification gate** (§2.2a), auth shell, `/welcome/` + discovery, address step, verify-email ordering | `accounts/forms.py`, `accounts/views.py`, `accounts/urls.py`, `accounts/profile.py`, `accounts/onboarding.py`, `billing/views.py:481`, `templates/auth/_shell.html`, `templates/accounts/{signup,welcome}.html`, auth pages, `components/form_field.html`, `static/js/signup-wizard.js`, `assets/app.css` | `BusinessProfile.business_type`, `primary_channel` | **Production system mail confirmed to reach Gmail and Outlook (not SES sandbox)**; free and paid signup journeys (free: signup → verify → welcome; paid: signup → pay → verify → welcome) and billing tests pass; §2.2a email-delivery checklist all ticked; the direct-URL access test, PayPal webhook/cancel/forged-return tests and the full mistyped-email paid journey in §10 pass (plus one manual PayPal-sandbox run on staging); D6 dry-run list shown to you before release, and release-day verification emails sent; deep links; `accounts_qa.mjs` green at 390/1280 in both themes with the auth pages and signed-in first-run screens added to its scan; claims test covers the new copy |
| 2B | Milestones, "Your first customer", one-channel essentials, backfill | `accounts/milestones.py`, `accounts/models.py`, hook sites in §3.3, `accounts/onboarding.py`, `templates/accounts/{onboarding,dashboard}.html`, the backfill command | `OnboardingMilestone` | §10 idempotency/concurrency tests; workspace isolation; backfill on a copy of prod data reconciles with live checks |
| 2C | Message yourself, `is_test`, exclusion | `contacts/models.py`, `whatsapp` inbound path, the inbox contact panel, every "Yes" row in §5 | `Contact.is_test` (+ index) | §5 parametrised exclusion test passes for every row; benchmark/snapshot capture excludes test; recovery paths in §4.2 tested |
| 2D | Channel connection guidance | `core/channels.py`, `components/channel_connect_explainer.html`, the 4 setup templates | — | Each offered channel's content is reviewed against the live Meta and DNS flows; copy passes the claims test |
| 2E | Contextual guidance + empty states | `components/guide_hint.html`, `accounts/models.py` (`Membership.dismissed_guides`), the §8 templates | `Membership.dismissed_guides` | Hints never block an action; per-user dismissal; hidden when the feature is unusable |
| 2F | Operator activation funnel | `core/console/` view + template, `check_activation_metrics` command | — | Reconciliation command reports 0 differences on staging data |
| 3 | Pilot validation (no code) | `docs/design/ui-quality-log.md` | — | Real retailers from Wave 1/2 go through the journey; drop-offs interviewed; findings logged before any further change |

### 11.1 Slice 2-0: remove payment links, mark paid by hand

**Status (2026-10-30): built, uncommitted, not released.** Two deliberate deviations from the plan below: no `mark_order_paid` registry action was added (a workflow already reacts to `order.paid`, and marking paid stays a person's statement); the WhatsApp starter templates "Payment link", "Payment failed" and "Payment overdue" were kept, because the business fills in its own link and Akilent takes no money through them.

**Why:** product decision (2026-10-30). Akilent covers marketing, conversations, leads, orders as a record, and insights; it never collects money for a business. This also removes P1.

**Add:**
- `commerce.services.mark_order_paid(order, *, method="", reference="", actor)`. Locks the order (`select_for_update`); only an open order (pending, awaiting payment, failed) becomes PAID with `paid_at=now`; anything else is a no-op. Then it calls `_after_order_paid` (§3.3). `method` is one of the business's `BusinessProfile.payment_methods` or "Other"; `reference` is optional free text. Both go on the order (two new blank fields, `paid_method` and `paid_reference`) rather than in a `Payment` row, because no money passed through Akilent.
- `mark_order_unpaid(order, *, actor)` for mistakes: PAID → PENDING, `paid_at` cleared, an `order.unpaid` event. Workflows that already ran aren't undone.
- Order detail: the Payment card becomes **"Mark as paid"** (method + optional reference, one click), then "Paid on {date} · {method} · Mark as not paid". The Orders list's "needs attention" view filters on *Pending* only.
- A `mark_order_paid` registry action, so a workflow can mark paid or react to `order.paid`, but nothing can request money.

**Remove:**

| Where | What |
|---|---|
| `apps/commerce/services.py:95-163` | `request_payment`, `PaymentNotRequestable`; then `mark_paid` and `mark_failed` once their shared work lives in `_after_order_paid` |
| `apps/commerce/actions.py:40` | the `request_payment` registry action |
| `apps/commerce/views.py:154-167` | the `request_payment` POST branch, with the false "Payment link created." toast |
| `apps/billing/providers/flutterwave.py:256` | commerce routing in the shared webhook (Akilent's own subscription handling stays) |
| `apps/accounts/onboarding.py:1085-1099` | the `payment_link` next action, replaced by "Mark the order paid when the customer pays" |
| `apps/verticals/registry.py:37` | the restaurant pack's "Confirm order & request payment" step (its other workflows stay) |
| `apps/whatsapp/starter_templates.py` | any payment-link template or variable |
| `apps/accounts/management/commands/seed_demo_workspace.py` | demo paid orders switch from `mark_paid` to `mark_order_paid` |
| `templates/commerce/order_detail.html`, `orders.html` | "Request payment", link states, payment-link empty-state copy |
| `templates/components/welcome_tour.html` | the journey slide's "send a payment link" |
| `templates/help/articles/getting-started.html` | payment-link steps |
| `templates/accounts/landing/_hero.html`, `_features.html`, `_journey.html`, `art/_order.html`, `art/_inbox.html`, `art/_customer.html` | payment-link claims and illustrations |
| `docs/marketing/messaging.md` §2, §3 (Convert), §4, §8 | "payment links" go. Convert becomes "marks the customer interested, records the order, and marks it paid when they pay". The claim register and the landing claims test change together. |
| 11 test files | payment-link tests removed or rewritten against `mark_order_paid` |

**Keep:** the `Payment` model and its existing rows (read-only history, still shown as "Paid via Flutterwave" where present); `Order.Status.AWAITING_PAYMENT` as a historic value (no new writes); PayPal and Flutterwave billing for Akilent's own subscriptions.

**Data migration:** orders in *Awaiting payment* → *Pending*; their pending payments → failed with `error="withdrawn"`. Backwards it's a no-op.

---

## 12. Decisions needed before Phase 2

| # | Decision | Why it matters | Recommendation |
|---|---|---|---|
| **D1** | ~~How does an order get paid?~~ **Decided 2026-10-30.** | Akilent doesn't handle businesses' payments. | Payment links are removed and the business marks orders paid by hand (slice 2-0). Activation ends at "order marked paid". |
| **D2** | **What's activation for email-only accounts?** | They have no inbox (J5). **Parked:** email is off in production (AWS refused SES access). | Decide when email returns. Until then, email-only accounts can't exist. |
| **D3** | ~~Should verify-email stay required?~~ **Decided 2026-10-30.** | — | Verification gates the workspace (§2.2a); it's no longer a checklist step. |
| **D6** | ~~Existing unverified accounts~~ **Decided 2026-10-30: everyone, immediately.** | — | Every account whose owner hasn't confirmed their email is gated from release day, paying customers and pilots included. Safeguards that don't change the decision: (1) a dry-run command lists every affected account (plan, paid or not, pilot or not, last activity) for you to see before release; (2) at release, each affected owner is sent a fresh verification link, so the first thing they see on signing in is a working link, not a dead end; (3) the check-email screen's Resend and Change-it work for them exactly as for new signups. No `verification_grace_until` field. |
| **D4** | ~~Signup step tracking~~ **Decided 2026-10-30: yes.** | Signup-to-setup is the first funnel step; today only finished signups are known. | Add a `signup_step` type to the existing anonymous `UxEvent` intake (`apps/core/views.py:32`, rate-limited and session-scoped): step index, plan slug and channel keys only, **never field values**. Completion is counted server-side (an `Account` created), so blocked beacons only under-count drop-off. The console shows step-to-step drop-off for the last 30 days as a lower bound. Recovery: the non-secret draft in `sessionStorage` (§2.2); a server error lands on the step holding the error; no abandoned-signup emails. Ships in 2A (tracking call) and 2F (console view). |
| **D5** | ~~Instagram/Facebook practice contacts~~ **Decided 2026-10-30: WhatsApp-only practice.** | Akilent can't tell an owner's personal Instagram/Facebook profile from a customer's. | Practice is offered only on WhatsApp, where the owner's number is detected automatically (§4.1). No practice option, "This is me" button or "Is this you?" prompt on Instagram or Facebook. Accepted consequence: an owner who DMs their own IG/FB account anyway is counted as real. |
