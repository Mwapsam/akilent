"""One POST to a model API, with every failure turned into a readable ``AIProviderError``.

Shared by the providers so each only adapts its own request and response shape. Messages never
include the API key or customer text.
"""
from __future__ import annotations

import requests

from apps.ai.providers.base import AIProviderError

CONNECT_TIMEOUT = 5


def post_json(url: str, *, headers: dict, body: dict, timeout: float, model: str) -> dict:
    try:
        response = requests.post(url, json=body, headers=headers, timeout=(CONNECT_TIMEOUT, timeout))
    except requests.Timeout as exc:
        raise AIProviderError("The AI service took too long to answer.") from exc
    except requests.RequestException as exc:
        raise AIProviderError(f"Could not reach the AI service ({type(exc).__name__}).") from exc

    if response.status_code in (401, 403):
        raise AIProviderError("The AI service rejected the API key.")
    if response.status_code == 404:
        raise AIProviderError(f"The AI service doesn't have the model {model!r}.")
    if response.status_code == 429:
        raise AIProviderError("The AI service is rate limiting requests.")
    if response.status_code == 529 or response.status_code >= 500:
        raise AIProviderError(f"The AI service is having problems ({response.status_code}).")
    if response.status_code >= 400:
        raise AIProviderError(f"The AI service returned an error ({response.status_code}).")
    try:
        data = response.json()
    except ValueError as exc:
        raise AIProviderError("The AI service sent a response we couldn't read.") from exc
    if not isinstance(data, dict):
        raise AIProviderError("The AI service sent a response we couldn't read.")
    return data
