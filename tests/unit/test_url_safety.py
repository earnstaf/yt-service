"""Unit tests for :mod:`app.url_safety`.

Covers the SSRF guard described in code-review finding H11:

- HTTPS to a public IP passes.
- HTTPS to 127.0.0.1 / loopback rejects.
- HTTP in production rejects; HTTP in dev passes.
- Hostnames that resolve to the cloud metadata range (169.254.x) reject.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.exceptions import InvalidRequestError
from app.url_safety import validate_callback_url


def _patch_resolver(monkeypatch: Any, mapping: dict[str, list[str]]) -> None:
    """Replace :func:`app.url_safety._resolve_ips` with a deterministic map."""
    from app import url_safety

    def fake(host: str) -> list[str]:
        return mapping.get(host, [])

    monkeypatch.setattr(url_safety, "_resolve_ips", fake)


def _patch_env(monkeypatch: Any, *, production: bool) -> None:
    """Force ``settings.is_production`` to the requested value for this test."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "yt_env", "production" if production else "dev")


def test_https_to_public_ip_is_allowed(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    out = validate_callback_url("https://example.com/hook")
    assert out.startswith("https://example.com")


def test_https_to_loopback_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {"localhost": ["127.0.0.1"]})
    with pytest.raises(InvalidRequestError) as ei:
        validate_callback_url("https://localhost/hook")
    assert ei.value.details is not None
    assert ei.value.details["reason"] == "private_ip"


def test_https_to_metadata_endpoint_rejected(monkeypatch: Any) -> None:
    """169.254.x.x is link-local — cloud metadata exfiltration risk."""
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {"evil.example.com": ["169.254.169.254"]})
    with pytest.raises(InvalidRequestError) as ei:
        validate_callback_url("https://evil.example.com/")
    assert ei.value.details["reason"] == "private_ip"


def test_https_to_private_rfc1918_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {"internal.corp": ["10.0.0.5"]})
    with pytest.raises(InvalidRequestError):
        validate_callback_url("https://internal.corp/")


def test_http_in_production_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    with pytest.raises(InvalidRequestError) as ei:
        validate_callback_url("http://example.com/hook")
    assert ei.value.details["reason"] == "scheme_not_allowed"


def test_http_in_dev_allowed(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=False)
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    out = validate_callback_url("http://example.com/hook")
    assert out.startswith("http://example.com")


def test_unresolvable_host_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {})
    with pytest.raises(InvalidRequestError) as ei:
        validate_callback_url("https://no-such-host.invalid/")
    assert ei.value.details["reason"] == "resolution_failed"


def test_unsupported_scheme_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    with pytest.raises(InvalidRequestError):
        validate_callback_url("file:///etc/passwd")


def test_missing_host_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    with pytest.raises(InvalidRequestError):
        validate_callback_url("https:///no-host")


def test_empty_url_rejected(monkeypatch: Any) -> None:
    _patch_env(monkeypatch, production=True)
    with pytest.raises(InvalidRequestError):
        validate_callback_url("   ")
