"""Unit tests for ``app.auth``.

These tests are stdlib-only (plus argon2-cffi via the import chain). They do
NOT touch the database — DB-bound coverage lives in
``tests/integration/test_admin_tokens.py`` per the test-layering decision (P-14).
"""

from __future__ import annotations

import pytest

from app.auth import (
    BEARER_RE,
    hash_token,
    make_token,
    make_token_id,
    make_webhook_secret,
    verify_token_hash,
)


# ---- ID / plaintext minting -----------------------------------------------


def test_make_token_has_yt_prefix_and_length() -> None:
    tok = make_token()
    assert isinstance(tok, str)
    assert tok.startswith("yt_")
    # token_urlsafe(32) emits ~43 chars; plus the "yt_" prefix that's > 30.
    assert len(tok) > 30


def test_make_token_yields_unique_values() -> None:
    assert make_token() != make_token()


def test_make_token_id_has_tok_prefix() -> None:
    tid = make_token_id()
    assert isinstance(tid, str)
    assert tid.startswith("tok_")
    # 12 url-safe bytes => 16 base64 chars; "tok_" prefix => total > 16.
    assert len(tid) > len("tok_")


def test_make_token_id_yields_unique_values() -> None:
    assert make_token_id() != make_token_id()


def test_make_webhook_secret_is_64_hex_chars() -> None:
    secret = make_webhook_secret()
    assert isinstance(secret, str)
    assert len(secret) == 64
    # All chars are valid lowercase hex.
    assert all(c in "0123456789abcdef" for c in secret)


# ---- Hash / verify round-trip ---------------------------------------------


def test_hash_and_verify_round_trip() -> None:
    plain = make_token()
    stored = hash_token(plain)
    assert stored != plain  # paranoia: never store plaintext
    assert verify_token_hash(stored, plain) is True


def test_verify_returns_false_for_wrong_plain() -> None:
    plain = make_token()
    stored = hash_token(plain)
    # Wrong plaintext must return False, NOT raise.
    assert verify_token_hash(stored, "yt_definitely_not_the_same_value") is False


def test_verify_returns_false_for_corrupted_hash() -> None:
    # Garbage hash string should be reported as a miss, not crash the request.
    assert verify_token_hash("not-a-valid-argon2-encoded-string", "anything") is False


def test_hash_outputs_argon2id_encoded_form() -> None:
    """Sanity check: the hash uses argon2id and embeds parameters."""
    stored = hash_token("test-plaintext")
    assert stored.startswith("$argon2id$")


def test_hash_is_salted_so_repeats_differ() -> None:
    """Hashing the same plaintext twice yields different encoded strings (salt is random)."""
    plain = "stable-test-value"
    assert hash_token(plain) != hash_token(plain)


# ---- Bearer header regex --------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer xyz", "xyz"),
        ("bearer xyz", "xyz"),
        ("BEARER xyz", "xyz"),
        ("Bearer   xyz", "xyz"),  # multiple spaces
        ("Bearer\txyz", "xyz"),  # tab as whitespace
        ("Bearer yt_abc_def_ghi", "yt_abc_def_ghi"),
    ],
)
def test_bearer_re_matches_valid_headers(header: str, expected: str) -> None:
    match = BEARER_RE.match(header)
    assert match is not None
    assert match.group(1).strip() == expected


@pytest.mark.parametrize(
    "header",
    [
        "",
        "xyz",
        "Bearer",  # no credential
        "Basic xyz",  # wrong scheme
        " Bearer xyz",  # leading whitespace breaks the anchored ^
    ],
)
def test_bearer_re_rejects_invalid_headers(header: str) -> None:
    assert BEARER_RE.match(header) is None
