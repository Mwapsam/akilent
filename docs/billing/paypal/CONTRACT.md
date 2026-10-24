# PayPal contract (Phase 0, step 1)

The request and response shapes the PayPal provider is built against. Each item is marked:

- **Schema**: confirmed in the PayPal OpenAPI schema saved in `schemas/` (downloaded 2026-10-23 from `developer.paypal.com/api/<api>/schema.json`).
- **Sandbox**: not in the schema, or ambiguous there. It must be confirmed in the sandbox run (Phase 0, step 2), and the recorded payload saved in `fixtures/`.

The plan is at `C:\Users\hp\.claude\plans\make-a-plan-for-compiled-pillow.md`. Its rule: no provider code until the checkout items are confirmed, and no vault code until the vault items are.

| File | API |
|---|---|
| `schemas/orders-v2.json` | Orders v2 (`/v2/checkout/orders`) |
| `schemas/payments-v2.json` | Payments v2 (captures, refunds) |
| `schemas/vault-v3.json` | Payment Method Tokens v3 (`/v3/vault/payment-tokens`) |
| `schemas/webhooks-v1.json` | Webhooks (signature verification, event types) |
| `schemas/transaction-search-v1.json` | Transaction Search (`/v1/reporting/transactions`) |
| `schemas/subscriptions-v1.json` | Subscriptions v1 (Phase 5 only) |

## 1. Checkout order (no vault)

`POST /v2/checkout/orders` with header `PayPal-Request-Id: order-{reference}` **[Schema]**.

```json
{
  "intent": "CAPTURE",
  "purchase_units": [{
    "custom_id": "{reference}",
    "invoice_id": "{reference}",
    "description": "{payment_label}",
    "amount": {"currency_code": "USD", "value": "12.00"}
  }],
  "payment_source": {"paypal": {"experience_context": {
    "return_url": "{return_url}", "cancel_url": "{plans_url}",
    "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"
  }}}
}
```

- The customer is redirected to the link with `rel=payer-action` **[Sandbox]**.
- **`invoice_id` is unique per merchant account by default** **[Schema]**: "invoice_id values are required to be unique within each merchant account by default. Although the uniqueness validation is configurable…". The account setting must stay on, and `paypal_check` verifies it. The exact issue code returned for a duplicate is **not in the schema** **[Sandbox]**; it's expected to be `DUPLICATE_INVOICE_ID`.
- Capture: `POST /v2/checkout/orders/{id}/capture` with `PayPal-Request-Id: capture-{reference}` **[Schema]**. The issue code for an already-captured order is **not in the schema** **[Sandbox]**; it's expected to be `ORDER_ALREADY_CAPTURED`.
- Fields verified before any result is accepted, all **[Schema]**: `purchase_units[0].payee.merchant_id`, `purchase_units[0].payments.captures[0].{id,status,amount,custom_id,invoice_id}`.

## 2. Capture and refund statuses [Schema]

| Resource | Status enum | Our mapping |
|---|---|---|
| capture | `COMPLETED` | SUCCEEDED |
| capture | `PENDING` | PENDING (`status_details.reason`, e.g. `PENDING_REVIEW`, `TRANSACTION_APPROVED_AWAITING_FUNDING`) |
| capture | `DECLINED`, `FAILED` | FAILED |
| capture | `REFUNDED`, `PARTIALLY_REFUNDED` | cross-check only; each refund is recorded from its own refund resource |
| refund | `COMPLETED` | record a `BillingRefund` row |
| refund | `PENDING` | wait; record when COMPLETED |
| refund | `FAILED`, `CANCELLED` | ignore (log) |
| any | anything else | PENDING, never SUCCEEDED |

The capture resource has no `DENIED` status. `DENIED` exists only as a webhook event name (see §6).

A refund resource carries its own `invoice_id` and `custom_id` **[Schema]**, so a refund maps back to our reference without a second lookup.

## 3. Initial checkout that saves the wallet (vault): gated

This adds to the §1 body, under `payment_source.paypal` (all **[Schema]** unless marked):

```json
"attributes": {
  "customer": {"merchant_customer_id": "{account public id}"},
  "vault": {
    "store_in_vault": "ON_SUCCESS",
    "usage_type": "MERCHANT",
    "customer_type": "CONSUMER",
    "usage_pattern": "RECURRING_PREPAID",
    "description": "Akilent {plan} — {amount} every {period} until you cancel"
  }
}
```

- `usage_type` is the only required vault field. `store_in_vault` takes only `ON_SUCCESS`.
- **`usage_pattern` choice (for sign-off):**
  - `RECURRING_PREPAID` = fixed or variable amounts at regular intervals, paid up front.
  - `SUBSCRIPTION_PREPAID` = a fixed amount.

  Akilent's renewal amount can vary: account credit and proration reduce `amount_due`. So **`RECURRING_PREPAID`** is the accurate description. The same value must be used in §4.
- `vault.description` is shown to the payer during approval and in their PayPal token management. This is the consent text, and it is copied into `docs/marketing/messaging.md`.
- `customer.merchant_customer_id` is our own identifier for the customer **[Schema]**. It is the correlation key for vault tokens (§5).

Response: `payment_source.paypal.attributes.vault = {id, status, customer}` with `status ∈ VAULTED | CREATED | APPROVED` **[Schema]**. PayPal's own capture sample returns `status: "CREATED"` **together with an `id`**, and a `rel=payment-token` link. So the rule is:

- an id is present (any status) → `GET /v3/vault/payment-tokens/{id}` → save only if that authoritative fetch succeeds and its `customer.merchant_customer_id` equals the paying account;
- no id (`APPROVED`) → wait for the token (§5).

## 4. Merchant-initiated renewal charge: gated

`POST /v2/checkout/orders` with `PayPal-Request-Id: charge-{reference}`. Shape from PayPal's own sample (`orders.create` sample 1) **[Schema]**:

```json
{
  "intent": "CAPTURE",
  "purchase_units": [{"custom_id": "{reference}", "invoice_id": "{reference}",
                      "amount": {"currency_code": "USD", "value": "12.00"}}],
  "payment_source": {"paypal": {
    "vault_id": "{token}",
    "stored_credential": {
      "payment_initiator": "MERCHANT",
      "usage": "SUBSEQUENT",
      "usage_pattern": "RECURRING_PREPAID"
    }
  }}
}
```

- `stored_credential.payment_initiator` is required (`CUSTOMER | MERCHANT`). `usage` ∈ `FIRST | SUBSEQUENT | DERIVED`. **[Schema]**
- **[Sandbox]** PayPal's sample uses `intent: AUTHORIZE` and returns `status: COMPLETED` with an authorization. With `intent: CAPTURE`, confirm whether create returns the capture directly or a separate capture call is needed.
- **[Sandbox]** Whether the initial checkout (§3) should also send `stored_credential {payment_initiator: CUSTOMER, usage: FIRST, usage_pattern}` alongside the vault attributes.
- `PAYER_ACTION_REQUIRED` appears in the Orders schema's error catalogue **[Schema]**. The decline issue code (expected `INSTRUMENT_DECLINED`) is **not in the schema** **[Sandbox]**.

## 5. Vault token resource and correlation

`GET /v3/vault/payment-tokens/{id}` returns `payment_token_response` **[Schema]**:

- `id`;
- `customer.id` (PayPal's customer id) and `customer.merchant_customer_id` (ours);
- `payment_source.paypal.{email_address, payer_id, usage_pattern, usage_type, customer_type, description}`.

**The token resource has no order id.** Correlation is therefore by `customer.merchant_customer_id` (our account public id, sent at checkout), never by email and never by "most recent checkout".

- **[Sandbox]** Confirm that the `VAULT.PAYMENT-TOKEN.CREATED` webhook resource is this same shape and carries `merchant_customer_id`. If it doesn't, the webhook only triggers a re-fetch of that account's open PayPal payments (`GET /v2/checkout/orders/{provider_operation_id}`).
- Listing by customer: `GET /v3/vault/payment-tokens?customer_id=` **[Schema]**. `DELETE /v3/vault/payment-tokens/{id}` exists **[Schema]**.
- The Vault API keeps `PayPal-Request-Id` keys for only **3 hours** **[Schema]** (on `POST /v3/vault/payment-tokens`).

## 6. Webhooks

Verification: `POST /v1/notifications/verify-webhook-signature` **[Schema]**. All of these are required: `auth_algo`, `cert_url`, `transmission_id`, `transmission_sig`, `transmission_time` (from the `PAYPAL-*` headers), `webhook_id` (our `PAYPAL_WEBHOOK_ID`), and `webhook_event` (the parsed body). The response `verification_status` must be `SUCCESS`.

Event names found in the schemas:
- `PAYMENT.CAPTURE.COMPLETED`, `.PENDING`, `.DECLINED`, `.DENIED`, `.REFUNDED`
- `CHECKOUT.PAYMENT-APPROVAL.REVERSED`

**[Sandbox]**, to confirm with `GET /v1/notifications/webhooks-event-types` and record: `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.REVERSED`, `VAULT.PAYMENT-TOKEN.CREATED`, `VAULT.PAYMENT-TOKEN.DELETED`.

Every event is only a trigger. The provider re-fetches the order, capture, refund or token before acting.

## 7. Idempotency and reconciliation

- **Orders v2:** `PayPal-Request-Id` is "a unique ID identifying the request header for idempotency purposes" **[Schema]**. The **retention window is not in the schema**. PayPal's docs give 6 hours by default, extendable to 72 by arrangement. `PAYPAL_REQUEST_ID_TTL_HOURS` defaults to 6, and the sandbox run records whether the account has an extension.
- **Transaction Search** `GET /v1/reporting/transactions` **[Schema]**:
  - Filters: `transaction_id`, `transaction_type`, `transaction_status`, `transaction_amount` (a range), `transaction_currency`, `start_date`/`end_date` (at most 31 days), `fields`, `balance_affecting_records_only`, paging.
  - **There is no `invoice_id` filter.** Results include `transaction_info.invoice_id` and `custom_field`, so reconciliation searches by date window, amount and currency, then matches `invoice_id == reference` locally.
  - The reporting delay is **not in the schema** **[Sandbox]**; PayPal's docs say up to 3 hours.
- **Final guard:** `invoice_id` is unique by default (§1). A re-sent charge for the same reference is refused rather than charged twice.

## Decision (2026-10-24): no Vault / MIT renewals

The merchant's PayPal business account is South African (sandbox Business account country `ZA`), and saving PayPal wallets (Vault) is only available to US merchants. This matches the silently dropped vault request below. §3–§5 are **parked**. PayPal ships as Orders v2 checkout, with renewals by emailed pay link (`off_session_charge = False`). Revisit only if the business gets a US PayPal merchant account.

## 8a. Sandbox findings so far (2026-10-24, read-only probe, `fixtures/sandbox/`)

No money moved: orders were created but not approved or captured.

| Check | Result |
|---|---|
| OAuth | OK. Scopes include `vault/payment-tokens/readwrite`, `billing-agreements`, `subscriptions`, `refund` (`oauth_scopes.json`, no token stored). |
| Create order (§1) | 200, `status: PAYER_ACTION_REQUIRED`, links `self` + `payer-action`. |
| `PayPal-Request-Id` replay | **Confirmed.** The same request id returned the same order id. |
| Duplicate `invoice_id` at **create** | **Not refused.** A second order with the same invoice id was created, so the uniqueness check, if on, happens at capture. Must be confirmed by capturing both orders. Until then the duplicate-invoice guard is unproven. |
| Create order with vault (§3), incl. `merchant_customer_id` and `usage_pattern: RECURRING_PREPAID` | Accepted (200, `PAYER_ACTION_REQUIRED`). |
| Vault setup token | 201, echoes `usage_pattern`/`usage_type`/`customer_type`. The sandbox app may vault PayPal wallets. A hint only, not approval. |
| Event names | Present: `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.{COMPLETED,DECLINED,DENIED,PENDING,REFUNDED,REVERSED}`, `VAULT.PAYMENT-TOKEN.{CREATED,DELETED,DELETION-INITIATED,UPDATED}`. **`CHECKOUT.PAYMENT-APPROVAL.REVERSED` is not in this app's list** (it appears in the schema docs only). Treat it as optional; the stale-checkout path covers an approval that never gets captured. |
| Our webhook | Subscribed to `*` (all events). Fine for sandbox; for live, subscribe only to the list above. |
| Order `COMPLETED`, capture `PENDING` | **Confirmed and important.** Both captured orders came back `status: COMPLETED`, but their capture was `PENDING` with `status_details.reason: PENDING_REVIEW`. Order status must never be read as paid; only the capture status counts (§2). |
| Vault on the order | **Silently dropped.** The create response echoed `payment_source.paypal: {}` (no `attributes`); after approval and capture there was no `attributes.vault` and no token. No error was returned, which confirms "checkout succeeded" must never imply "method saved". Cause not yet known (merchant account country or eligibility suspected); the next test is the setup-token flow. |
| Payee check | The payee `merchant_id` on the captured orders did **not** match `PAYPAL_MERCHANT_ID` in `.env`. The planned check would correctly refuse these payments, so the env value needs fixing. |
| Capture of an order the buyer never approved | 422 `ORDER_NOT_APPROVED`. |
| Duplicate `invoice_id` at **capture** | **Confirmed guard.** The buyer approved link 1, but capturing it was refused with 422 `DUPLICATE_INVOICE_ID` because an order with the same invoice id had already been captured. No money moved. A retry with the same or a new request id is refused the same way. Handling: never treat it as a decline; look up the payment that already holds that reference (§7) instead. |
| Capture of an already-captured order | 422 `ORDER_ALREADY_CAPTURED` (same or new request id). Handling: `GET` the order and read its capture. |

## 8. Still to confirm in the sandbox (Phase 0, step 2)

Each one is saved as a fixture in `fixtures/` (secrets and payer details redacted):

1. Approve → capture (no vault); a duplicate capture (the already-captured issue code); a duplicate `invoice_id` (the issue code).
2. Capture with vault: the vault status and id in the response; the `rel=payment-token` link; the token GET with `merchant_customer_id`.
3. MIT charge with `intent: CAPTURE`: the response shape; a declined charge (issue code); `PAYER_ACTION_REQUIRED` (with the payer-action link).
4. A full refund and a partial refund: capture status and refund resources.
5. Token delete: the webhook body.
6. Webhook bodies for every event in §6, and the `webhooks-event-types` list.
7. Whether the request-id retention is 6h or extended on this account.
