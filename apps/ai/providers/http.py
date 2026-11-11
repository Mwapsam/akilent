"""One POST to a model API, with every failure turned into a readable ``AIProviderError``.

Shared by the providers so each only adapts its own request and response shape. Messages never
include the API key or customer text.

``post_json`` waits for one JSON answer. ``post_ndjson_stream`` reads a streamed answer (one JSON
object per line, as Ollama sends with ``stream: true``): bytes keep flowing while the model writes,
so a proxy that cuts idle connections (Cloudflare, ~100 s) never sees the line go quiet.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import requests

from apps.ai.providers.base import AIProviderError

CONNECT_TIMEOUT = 5


def _send(url, *, headers, body, timeout, auth, connect_timeout, stream=False):
    try:
        return requests.post(
            url,
            json=body,
            headers=headers,
            timeout=(connect_timeout, timeout),
            **({"auth": auth} if auth else {}),
            **({"stream": True} if stream else {}),
        )
    except requests.Timeout as exc:
        raise AIProviderError("The AI service took too long to answer.") from exc
    except requests.RequestException as exc:
        raise AIProviderError(
            f"Could not reach the AI service ({type(exc).__name__})."
        ) from exc


def raise_for_status(response, model: str) -> None:
    status = response.status_code
    if status in (401, 403):
        raise AIProviderError("The AI service rejected the API key.")
    if status == 402:
        raise AIProviderError(
            "The AI service account needs payment (402): out of credit or plan limit reached."
        )
    if status == 524:
        raise AIProviderError(
            "The AI server took longer than Cloudflare allows (524, about 100 s): "
            "the model was probably still loading or the server is too slow."
        )
    if status == 404:
        raise AIProviderError(f"The AI service doesn't have the model {model!r}.")
    if status == 429:
        raise AIProviderError("The AI service is rate limiting requests.")
    if status == 529 or status >= 500:
        raise AIProviderError(f"The AI service is having problems ({status}).")
    if status >= 400:
        raise AIProviderError(f"The AI service returned an error ({status}).")


def post_json(
    url: str,
    *,
    headers: dict,
    body: dict,
    timeout: float,
    model: str,
    auth: tuple[str, str] | None = None,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> dict:
    response = _send(
        url,
        headers=headers,
        body=body,
        timeout=timeout,
        auth=auth,
        connect_timeout=connect_timeout,
    )
    raise_for_status(response, model)
    try:
        data = response.json()
    except ValueError as exc:
        raise AIProviderError(
            "The AI service sent a response we couldn't read."
        ) from exc
    if not isinstance(data, dict):
        raise AIProviderError("The AI service sent a response we couldn't read.")
    return data


def post_ndjson_stream(
    url: str,
    *,
    headers: dict,
    body: dict,
    deadline: float,
    idle_timeout: float,
    model: str,
    auth: tuple[str, str] | None = None,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> Iterator[dict]:
    """Yield each JSON line of a streamed answer.

    ``idle_timeout`` is the longest silence allowed between chunks (it also covers the wait for
    the first token while the prompt is read). ``deadline`` caps the whole answer: past it the
    connection is closed, which makes Ollama stop generating.
    """
    started = time.monotonic()
    response = _send(
        url,
        headers=headers,
        body=body,
        timeout=idle_timeout,
        auth=auth,
        connect_timeout=connect_timeout,
        stream=True,
    )
    try:
        raise_for_status(response, model)
        try:
            for line in response.iter_lines():
                if time.monotonic() - started > deadline:
                    raise AIProviderError("The AI service took too long to answer.")
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError as exc:
                    raise AIProviderError(
                        "The AI service sent a response we couldn't read."
                    ) from exc
                if not isinstance(chunk, dict):
                    raise AIProviderError(
                        "The AI service sent a response we couldn't read."
                    )
                if chunk.get("error"):
                    raise AIProviderError("The AI service stopped with an error.")
                yield chunk
        except requests.Timeout as exc:
            raise AIProviderError("The AI service took too long to answer.") from exc
        except requests.RequestException as exc:
            raise AIProviderError(
                f"The AI service dropped the connection ({type(exc).__name__})."
            ) from exc
    finally:
        response.close()
