"""Website origins a chatbot may run on.

A browser's ``Origin`` header is ``scheme://host[:port]``, with no path and no trailing slash. Owners
type all sorts ("acme.com", "https://www.acme.com/shop/"), so every stored domain and every
incoming Origin goes through ``normalise_origin`` before they're compared.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise_origin(value: str) -> str | None:
    """``scheme://host[:port]`` in lower case, or None when ``value`` isn't a website address.

    A bare host ("acme.com") is taken as https. A default port is dropped, and any path, query or
    fragment is ignored.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or " " in host:
        return None
    if port and port != _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def clean_domains(values: list[str]) -> list[str]:
    """The normalised, de-duplicated domain list, or ValidationError naming the bad entries."""
    cleaned: list[str] = []
    bad: list[str] = []
    for value in values:
        if not (value or "").strip():
            continue
        origin = normalise_origin(value)
        if origin is None:
            bad.append(value.strip())
        elif origin not in cleaned:
            cleaned.append(origin)
    if bad:
        raise ValidationError(
            "These aren't website addresses: "
            + ", ".join(bad)
            + ". Use the form https://www.example.com."
        )
    return cleaned
