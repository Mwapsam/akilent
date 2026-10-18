"""Graph API error parsing shared by the publishing providers.

A ``MetaPublishError`` is a definitive answer: Meta rejected the request, or a local
pre-flight check did. Anything else raised during a publish call (timeouts, resets,
unreadable responses) is not one of these and is treated as an unknown outcome.
"""

from __future__ import annotations

# Graph API error codes, grouped by what the caller should do about them.
# https://developers.facebook.com/docs/graph-api/guides/error-handling
_RATE_LIMIT_CODES = {4, 9, 17, 32, 341, 613}
_PERMISSION_CODES = {3, 10}  # plus the 200-299 permission range
_VALIDATION_CODES = {100, 352, 9004}  # plus the 36000-36999 IG media range
_OUTAGE_CODES = {1, 2}


def parse_insights(data: dict) -> dict:
    """Turn a Graph ``/insights`` body into {metric: int}.

    Reads ``values[-1].value`` (or ``total_value.value``). A breakdown dict, such as
    reactions by type, is summed. Metrics absent from the body are left out.
    """
    result: dict[str, int] = {}
    for item in data.get("data") or []:
        name = item.get("name")
        if not name:
            continue
        value = None
        values = item.get("values") or []
        if values:
            value = values[-1].get("value")
        elif isinstance(item.get("total_value"), dict):
            value = item["total_value"].get("value")
        if isinstance(value, dict):
            value = sum(v for v in value.values() if isinstance(v, int | float))
        number = _as_int(value)
        if number is not None:
            result[name] = number
    return result


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MetaPublishError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        http_status: int = 0,
        error_type: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        self._error_type = error_type

    @property
    def token_invalid(self) -> bool:
        """The access token is expired, revoked or otherwise unusable (reconnect needed)."""
        return self.code == 190 or self.http_status == 401

    @property
    def error_type(self) -> str:
        """The ContentPostVariant.ErrorType value for this error."""
        from ..models import ContentPostVariant

        types = ContentPostVariant.ErrorType
        code = self.code
        if self._error_type:
            return self._error_type
        if self.token_invalid or code in _PERMISSION_CODES:
            return types.PERMISSION
        if code is not None and 200 <= code <= 299:
            return types.PERMISSION
        if code in _RATE_LIMIT_CODES or self.http_status == 429:
            return types.RATE_LIMIT
        if code is not None and 80000 <= code <= 80099:  # business-use-case limits
            return types.RATE_LIMIT
        if code in _VALIDATION_CODES or (code is not None and 36000 <= code <= 36999):
            return types.VALIDATION
        if code in _OUTAGE_CODES or self.http_status >= 500:
            return types.PROVIDER_OUTAGE
        return types.UNKNOWN

    @classmethod
    def from_response(cls, response, data: dict, message: str | None = None):
        """Build the error from a Graph API ``{"error": {...}}`` body."""
        err = data.get("error") or {}
        if not isinstance(err, dict):
            err = {"message": str(err)}
        status = getattr(response, "status_code", 0)
        return cls(
            message or err.get("message") or str(err),
            code=_as_int(err.get("code")),
            subcode=_as_int(err.get("error_subcode")),
            http_status=status if isinstance(status, int) else 0,
        )
