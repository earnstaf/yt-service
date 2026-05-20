"""URL safety helpers for outbound HTTP (webhook callbacks).

A caller-supplied ``callback_url`` is fetched server-side by the webhook
worker, so without validation it becomes an SSRF foothold: an attacker can
target loopback, private RFC1918 ranges, the cloud metadata endpoint
(169.254.169.254), or otherwise reach internal services the API can talk to
but the attacker cannot.

This module centralizes the safety check. Call :func:`validate_callback_url`
on any URL accepted from the public API surface (batch ``callback_url``,
future job-submission callbacks, monitor sinks). It returns the normalized
URL on success or raises :class:`InvalidRequestError` with a human-readable
reason on rejection.

Rules:

- Scheme: ``https`` is always allowed; ``http`` is allowed only when
  ``settings.is_production`` is False (dev/test convenience). Anything else
  rejected.
- Host: must resolve. Every resolved IP is checked against the
  ``ipaddress`` flags ``is_private``, ``is_loopback``, ``is_link_local``,
  ``is_multicast``, ``is_reserved``, ``is_unspecified``. Any single match
  rejects the URL — this catches AWS/GCP metadata (169.254.x.x) and
  loopback even when the URL uses a hostname that resolves there.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings
from app.exceptions import InvalidRequestError

# Schemes we are willing to fetch over. ``http`` is conditional on dev mode.
_ALWAYS_ALLOWED_SCHEMES: frozenset[str] = frozenset({"https"})
_DEV_ALLOWED_SCHEMES: frozenset[str] = frozenset({"https", "http"})


def _resolve_ips(host: str) -> list[str]:
    """Resolve ``host`` to all IP strings via ``socket.getaddrinfo``.

    Returns an empty list when resolution fails so callers can surface a
    clean rejection reason.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    out: list[str] = []
    for info in infos:
        # info = (family, type, proto, canonname, sockaddr)
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if isinstance(ip, str) and ip:
            out.append(ip)
    return out


def _is_dangerous_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` is a non-public range we must NOT fetch."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Cannot parse — treat as dangerous (refuse fail-open).
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_callback_url(url: str) -> str:
    """Validate a caller-supplied callback URL.

    Returns the canonical URL string on success. Raises
    :class:`InvalidRequestError` with a descriptive ``reason`` in details on
    failure.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidRequestError(
            "callback_url must be a non-empty string",
            details={"reason": "empty"},
        )

    parsed = urlparse(url.strip())

    allowed = _DEV_ALLOWED_SCHEMES if not settings.is_production else _ALWAYS_ALLOWED_SCHEMES
    if parsed.scheme.lower() not in allowed:
        raise InvalidRequestError(
            f"callback_url scheme {parsed.scheme!r} not allowed",
            details={
                "reason": "scheme_not_allowed",
                "scheme": parsed.scheme,
                "allowed": sorted(allowed),
            },
        )

    host = parsed.hostname
    if not host:
        raise InvalidRequestError(
            "callback_url is missing a hostname",
            details={"reason": "missing_host"},
        )

    # Resolve and check every address record. A hostname that maps to a mix
    # of public and private IPs is rejected — we cannot guarantee which one
    # the worker's resolver will pick at request time.
    ips = _resolve_ips(host)
    if not ips:
        raise InvalidRequestError(
            f"callback_url host {host!r} did not resolve",
            details={"reason": "resolution_failed", "host": host},
        )
    for ip in ips:
        if _is_dangerous_ip(ip):
            raise InvalidRequestError(
                f"callback_url resolves to disallowed IP {ip}",
                details={
                    "reason": "private_ip",
                    "host": host,
                    "resolved_ip": ip,
                },
            )

    return parsed.geturl()


__all__ = ["validate_callback_url"]
