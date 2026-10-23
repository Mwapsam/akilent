# Adding a payment provider

Akilent owns the subscription lifecycle. A payment provider only moves money, so adding one
(Paystack, PayPal, DPO, ...) means writing one adapter and changing nothing in the billing core.

## How billing works

| Piece | File | Job |
|---|---|---|
| Payment record | `BillingPayment` in `apps/billing/models.py` | One attempt to pay for one plan period. `reference` is ours and every provider carries it back. |
| Core | `apps/billing/payments.py` | `create_payment` takes unused time and account credit off the price first; `record_result` is the only code that marks a payment paid or failed. |
| Credit | `apps/billing/credit.py` | Grants, the ledger, earliest-expiring-first use, restore on failure. |
| Renewals | `apps/billing/renewals.py` | Hourly: credit, then charge the method saved with the active provider, else past due with a pay link, then expiry after the grace period. |
| Providers | `apps/billing/providers/` | Adapters behind `PaymentProvider` (`base.py`), found through `registry.py`. |

## Write the adapter

Create `apps/billing/providers/<name>.py` with a subclass of `PaymentProvider`:

```python
class PaystackProvider(PaymentProvider):
    code = "paystack"              # used in URLs and stored on payments
    display_name = "Paystack"
    off_session_charge = True      # can a saved card be charged without the customer?
    supported_currencies = frozenset({"NGN", "GHS", "ZAR", "KES", "USD"})

    def is_configured(self): ...                       # env keys present?
    def start_checkout(self, request, payment): ...    # redirect to the hosted page
    def reference_from_return(self, request): ...      # our reference from the return URL
    def handle_return(self, request, payment): ...     # verify with the provider API
    def parse_webhook(self, request): ...              # verify signature, return ProviderEvents
    def charge_saved_method(self, method, payment): ...
```

Rules:

- **Charge `payment.amount_due` in `payment.currency`.** Never recompute the price; credit and
  unused time are already taken off.
- **Send `payment.reference`** as the provider's transaction reference or metadata, and return
  to `return_url(request, payment)`.
- **Return provider-neutral `ProviderResult`s.** Amounts are in major units, the currency is
  ISO uppercase, and the status is `succeeded`, `failed`, `pending` or `requires_action`.
  Include `saved_method` when the provider gives a reusable token.
- **Never trust a redirect or webhook body on its own.** Re-fetch the transaction from the
  provider API, or verify the webhook signature with `hmac.compare_digest`. Raise
  `InvalidSignature` (the view answers 401) and `ProviderError` for transient failures (the
  view answers 500 so the provider retries).
- **Give each `ProviderEvent` a stable `key`** (event or transaction id) for de-duplication.
- **Make `charge_saved_method` idempotent on the provider side** where the provider supports
  it (an idempotency key, or `payment.reference` as the transaction reference).

## Register it

1. Add the dotted path to `BILLING_PROVIDERS` in `automator/settings.py`.
2. Add its keys to `automator/settings.py`, `.env.example` and the deploy workflow.
3. Point the provider's webhook at `https://<domain>/billing/webhooks/<code>/`.

It then appears in `/manage/settings/` and can be chosen once its keys are set and it supports
`BILLING_CURRENCY`.

## Test checklist

- Use the fake provider in `apps/billing/tests/conftest.py` as the model.
- Write adapter tests with mocked HTTP:
  - checkout sends `amount_due`, the currency and the reference;
  - a bad signature returns 401;
  - a replayed event is ignored;
  - an underpayment or a wrong currency doesn't activate;
  - a return carrying another account's reference does nothing;
  - a saved-method charge covers success, decline and `requires_action`.
- Run the test suite with `poetry run pytest apps/billing`.
- Do a test-mode checkout, then run `renew_due_subscriptions` with `current_period_end` set in
  the past.

## Switching providers in production

When the operator switches providers, customers keep the time they've paid for. At their next
renewal the engine finds no saved method for the new provider. It emails a pay link, and paying
it saves a method with the new provider. Run `manage.py migrate_legacy_billing --dry-run` once
to see any old provider-side recurring subscriptions, then run it without `--dry-run`.
