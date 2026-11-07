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
- Plan changes mid-term: unused time is valued per day from the current plan's
  `price_for` (`services.unused_value`), so a term paid at the old amount ($121.38) is now
  credited at the rounded one ($121). The difference is under one unit over a whole term.
  It can never go negative or exceed the new price. `test_payments
  .test_yearly_upgrade_mid_term_with_whole_unit_terms` covers a yearly upgrade at live
  prices.

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
