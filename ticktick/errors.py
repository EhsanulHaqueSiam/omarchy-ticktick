"""Exception hierarchy.

`kind` is the literal string the CLI puts in the JSON `error` field, so QML can branch on the
failure class (offer sign-in, show a retry, complain about arguments) without parsing prose.
"""

from __future__ import annotations


class TickTickError(Exception):
    """Base. `kind` is the JSON `error` value the CLI reports."""

    kind = "api"


class AuthError(TickTickError):
    """401/403, or no token stored at all."""

    kind = "auth"


class NetworkError(TickTickError):
    """DNS failure, timeout, connection reset — the request never got an answer."""

    kind = "network"


class ApiError(TickTickError):
    """A 4xx/5xx that is not auth and not a missing id."""

    kind = "api"


class UsageError(TickTickError):
    """Bad CLI arguments."""

    kind = "usage"


class NotFoundError(TickTickError):
    """Unknown task or project id."""

    kind = "notfound"


__all__ = [
    "TickTickError",
    "AuthError",
    "NetworkError",
    "ApiError",
    "UsageError",
    "NotFoundError",
]
