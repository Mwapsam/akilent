"""Turn a channel error from a send into the public API's error response."""

from rest_framework import status
from rest_framework.response import Response

from apps.core.channels import ChannelStateUnknownError, channel_error_status


def channel_error_response(exc):
    if isinstance(exc, ChannelStateUnknownError):
        return Response(
            {"error": {"code": "channel_unavailable", "message": str(exc)}},
            status=channel_error_status(exc),
            headers={"Retry-After": "60"},
        )
    return Response(
        {"error": {"code": "channel_disabled", "message": str(exc)}},
        status=status.HTTP_403_FORBIDDEN,
    )
