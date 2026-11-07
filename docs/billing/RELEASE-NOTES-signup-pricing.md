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
