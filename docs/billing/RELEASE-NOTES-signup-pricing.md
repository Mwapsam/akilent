# Release notes: signup flow and whole-unit term prices (2026-11)

## 1. Longer terms are charged in whole currency units (billing change)

`billing.pricing.price_for` now rounds quarterly, annual and two-year totals to a whole unit
(half up). Monthly prices keep their cents.

Production prices (live on akilent.com since commit c8d4dd9):

| Plan (monthly)        | Yearly before | Yearly after | Shown per month |
| --------------------- | ------------- | ------------ | --------------- |
| Starter ($11.90)      | $121.38       | $121         | $10 (real $10.08) |
| Professional ($25.90) | $264.18       | $264         | $22 (real $22.00) |
| Business ($49.90)     | $508.98       | $509         | $42 (real $42.42) |

Quarterly and two-year terms round the same way. For example, Starter quarterly goes from
$33.92 to $34, and Starter every 2 years goes from $214.20 to $214.

Pricing cards on the landing page, the signup wizard and Billing → Plans now lead with the
monthly equivalent ("$10/month") and show the exact charged total under it ("Billed $121 a
year"). The per-month figure (`pricing.monthly_equivalent`) is for display only and is rounded,
so the cheapest plan shows the biggest gap ($10 for a real $10.08). The billed total beside it
is always exact. Each term is worded one way on every card (`pricing.PERIOD_BILLED`: "a
month", "every 3 months", "a year", "every 2 years"). The
"save 15%" badges are still the multiplier's label, not worked out from the rounded totals.

**Existing subscribers**
- Renewals that Akilent runs (credit, Flutterwave, manual transfer) call `price_for` again at
  renewal. An annual, quarterly or two-year subscriber's next renewal therefore changes by
  less than one currency unit, up or down. Monthly subscribers see no change.
- Existing PayPal subscribers keep paying their PayPal plan's stored price
  (`PayPalPlan.price`, see `paypal_subscriptions.recurring_amount`). Their renewal payment
  rows are created at that same amount, so the amount check in `payments.record_result`
  matches. New PayPal subscriptions get a new PayPal plan at the rounded price.
- Checkouts already started keep the `amount_due` saved when they began.
- Plan changes mid-term: unused time is valued per day at what the customer was charged for
  their current term (the latest paid payment for that plan and period, less any refund;
  `services._term_price_paid`). Someone who paid $121.38 gets credit on $121.38, not on
  today's $121. Today's price is used only when no payment is on record, such as a plan an
  operator set by hand. Covered by `test_payments`: a yearly upgrade at live prices, credit
  at the recorded amount, and a refunded term leaving nothing unused.

## 2. Google Analytics: only after consent (site-wide)

Decided 2026-11-07: Google Analytics loads only after the visitor clicks **Accept**.

- **Cookie bar.** The old informational bar ("OK", `cookie_notice=1`) is replaced by an
  Accept / Decline bar (`components/cookie_consent.html`, `static/js/consent.js`). It now
  appears on the landing page, which had none, on the public shell and on signed-out
  `base.html` pages. The choice is kept for a year in the `analytics_consent` cookie
  (`granted` / `denied`). Everyone sees the bar once more, because the old flag only meant
  "dismissed".
- **Loader.** `templates/analytics/google.html` still sets up `dataLayer` and the config at
  once, so events from the page wait in order. gtag.js itself is fetched only when consent
  is granted, either already on record or the moment Accept is clicked. Without that, nothing
  is sent to Google.
- **Signed-in app pages show no bar** (unchanged). Someone who never accepted on a public page
  isn't tracked in the app. The cookie policy shows the bar to signed-in readers, so they can
  change their choice.
- **Cookie policy** (`legal/cookies.html`, updated 7 November 2026) now says analytics
  cookies are set only after Accept. It lists `analytics_consent` as necessary and links to
  "open the cookie bar again".

Still open: Cloudflare Insights (performance beacon) is unchanged and isn't behind the bar.

## 3. Signup (Phase 1, already committed)

The trial signup asks for name, email, password and company only. Fields are checked as
they are left. Progress survives a reload and the Back button. A plain `/signup/` starts on
the trial, and the Package step shows only for paid plans.

## 4. Confirm email by code; campaigns wait for it (Phase 4, needs its own release)

This changes sign-in code limits and blocks campaign sends, so it ships as a separate,
deliberate release.

**Deploy order:** run migrations before the new code serves traffic.
- `accounts/0028` adds the `verify` code purpose.
- `accounts/0029` marks **every existing account** email-confirmed, so no pilot loses campaign
  sending. It only marks accounts present when it runs; one created by the old code after
  that would have to confirm.

**What changes**
- **Signup sends a 6-digit code** (with the old link as a fallback in the same email) and
  sends free and trial signups to `/signup/verify/`. That page offers: enter code, send a new
  code, use a different email, skip for now. Paid signups still go to checkout first; the
  setup checklist's first step is now "Confirm your email address → Enter code".
- **Limits:** codes use `email_mfa`, with 5 guesses per code, 10 guesses and 5 sends per hour.
  The verify purpose has its **own** hourly allowance, so wrong verify guesses can't lock
  anyone out of sign-in codes. The sign-in purposes still share theirs (no extra guesses at a
  sign-in code).
- **Different email:** it's checked for uniqueness. The user's email and username and the
  account's billing email change together. Old codes are voided, and old links stop working
  (the token hashes the email).
- **Campaigns wait for a confirmed owner email:** email campaigns (`bulk.py`, composer and
  API; the API returns `403 email_not_confirmed`) and WhatsApp campaigns (`start_campaign`; a
  scheduled one fails with the reason). Nothing else is gated: inbox replies, Embedded Signup
  and channel setup work at once. The composer says up front what's missing, with links:
  "Enter code", and "Add address", which comes back to the composer once saved.
- **Dashboard welcome card** (optional) for industry and team size. Choices are on the form
  only, old free-text values are kept, and "No thanks" hides it for good.
- The first-run tour and setup widget don't open on the code page; they start on the next
  page.

- **Country is a select** in Settings → Business (list from `django-countries`, a new
  dependency: run `poetry install` on deploy). The column stays text and stores the English
  name, which the email footer prints. No data migration: a stored code (`ZM`) shows as its
  name, and an old typed value that isn't a country stays as its own option, so saving the
  page never loses it.

## 5. Signup polish (Phase 5, no migrations, safe to ship on its own)

- **No blank screen before the scripts run.** The signup wizard used to be hidden until
  Alpine started: an empty page, navy for anyone whose system is in dark mode. The step the
  server picks now renders straight away (with its channel cards and Continue); only steps
  and buttons that start hidden wait for Alpine.
- **Side panel on wide screens:** a product screenshot, three lines from the claim register
  (inbox, interested/follow-ups, orders) naming only channels that are on, and the Meta Tech
  Provider line when a Meta channel is live. Hidden on phones and on the Package step.
- **Phone stepper:** "Step 2 of 3 · Account" with a progress bar below `sm`; the labelled
  dots from `sm` up.
- **Autofill** keeps the theme's field colours (light and dark) instead of the browser's.
- **Branded 404** on the public shell (home or dashboard, and Help centre), and a
  self-contained **500** page (inline CSS, no template tags; exception added to the drift
  guard). Only seen with `DEBUG=False`.
- **Redirects:** `/login/` → `/auth/login/` and `/register/` → `/signup/`, permanent, query
  string kept.
