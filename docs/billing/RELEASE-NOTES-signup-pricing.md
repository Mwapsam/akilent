# Release notes: signup flow and whole-unit term prices (2026-11)

## 1. Longer terms are charged in whole currency units (billing change)

`billing.pricing.price_for` now rounds quarterly, annual and two-year totals to a whole unit
(half up). Monthly prices keep their cents.

| Starter at $19/month | Before   | After  |
| -------------------- | -------- | ------ |
| Quarterly (−5%)      | $54.15   | $54    |
| Annually (−15%)      | $193.80  | $194   |
| Every 2 years (−25%) | $342.00  | $342   |

Pricing cards on the landing page, the signup wizard and Billing → Plans now lead with the
monthly equivalent ("$16/month") and show the exact charged total under it ("Billed $194 a
year"). The per-month figure (`pricing.monthly_equivalent`) is for display only. The
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

## 2. Google Analytics loader (site-wide)

`templates/analytics/google.html` now sets up the `dataLayer`, `gtag('js')` and
`gtag('config')` straight away. Only the gtag.js download still waits until the browser is
idle. As a result, events sent before the script arrives (signup steps, `signup_created`)
are queued behind the config instead of being lost. Each page still sends one `page_view`.
`landing.js` sends only `cta_click`. There is no Consent Mode, and the cookie bar only
records that it was dismissed, so nothing has to run before `config`.

Not changed by this release: GA loads for every visitor behind a cookie bar that only
informs. With USA/UK/EU positioning, that needs its own consent decision.

## 3. Signup (Phase 1, already committed)

The trial signup asks for name, email, password and company only. Fields are checked as
they are left. Progress survives a reload and the Back button. A plain `/signup/` starts on
the trial, and the Package step shows only for paid plans.
