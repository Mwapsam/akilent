import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.flutterwave.com/v3"


class FlutterwaveError(Exception):
    pass


class FlutterwaveClient:
    def __init__(self, secret_key: str):
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {secret_key}"})

    def initialize_payment(
        self,
        *,
        tx_ref: str,
        amount,
        currency: str,
        customer_email: str,
        customer_name: str,
        redirect_url: str,
        payment_plan_id=None,
        meta: dict | None = None,
    ) -> str:
        payload = {
            "tx_ref": tx_ref,
            "amount": str(amount),
            "currency": currency,
            "redirect_url": redirect_url,
            "customer": {"email": customer_email, "name": customer_name},
            "payment_options": "card",
        }
        if payment_plan_id:
            payload["payment_plan"] = payment_plan_id
        if meta:
            payload["meta"] = meta

        r = self._session.post(f"{BASE_URL}/payments", json=payload, timeout=15)
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            raise FlutterwaveError("Invalid JSON response from Flutterwave")

        if r.status_code != 200 or data.get("status") != "success":
            raise FlutterwaveError(data.get("message", "Payment initialization failed"))

        return data["data"]["link"]

    def create_payment_plan(
        self, *, name: str, amount, interval: str = "monthly", currency: str = "USD"
    ) -> dict:
        """Create a recurring payment plan on Flutterwave; returns its data (incl. id)."""
        payload = {
            "amount": str(amount),
            "name": name,
            "interval": interval,
            "currency": currency,
        }
        r = self._session.post(f"{BASE_URL}/payment-plans", json=payload, timeout=15)
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            raise FlutterwaveError("Invalid JSON response from Flutterwave")
        if r.status_code not in (200, 201) or data.get("status") != "success":
            raise FlutterwaveError(data.get("message", "Payment plan creation failed"))
        return data["data"]

    def get_subscriptions(self, *, email: str | None = None, plan_id=None) -> list:
        """List subscriptions, optionally filtered by customer email and/or plan id."""
        params = {}
        if email:
            params["email"] = email
        if plan_id:
            params["plan"] = plan_id
        r = self._session.get(f"{BASE_URL}/subscriptions", params=params, timeout=15)
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            raise FlutterwaveError("Invalid JSON response from Flutterwave")
        if r.status_code != 200 or data.get("status") != "success":
            raise FlutterwaveError(data.get("message", "Could not list subscriptions"))
        return data.get("data") or []

    def verify_transaction(self, transaction_id) -> dict:
        r = self._session.get(
            f"{BASE_URL}/transactions/{transaction_id}/verify", timeout=15
        )
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            raise FlutterwaveError("Invalid JSON response from Flutterwave")

        if r.status_code != 200 or data.get("status") != "success":
            raise FlutterwaveError(
                data.get("message", "Transaction verification failed")
            )

        return data["data"]

    def cancel_subscription(self, subscription_id) -> dict:
        r = self._session.put(
            f"{BASE_URL}/subscriptions/{subscription_id}/cancel", timeout=15
        )
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            raise FlutterwaveError("Invalid JSON response from Flutterwave")

        if r.status_code not in (200, 204):
            raise FlutterwaveError(
                data.get("message", "Subscription cancellation failed")
            )

        return data


def get_fw_client() -> FlutterwaveClient:
    key = getattr(settings, "FLUTTERWAVE_SECRET_KEY", None)
    if not key:
        raise FlutterwaveError("FLUTTERWAVE_SECRET_KEY is not configured")
    return FlutterwaveClient(key)
