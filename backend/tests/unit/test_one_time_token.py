"""Unit tests for modulo.util.one_time_token (FAR-461 / MCP setup handoff)."""

import hashlib
import string

from modulo.util.one_time_token import generate_token, hash_token


def test_generate_token_is_urlsafe_high_entropy() -> None:
    token = generate_token()
    assert isinstance(token, str)
    assert len(token) >= 40  # 32 bytes urlsafe base64 ≈ 43 chars
    allowed = set(string.ascii_letters + string.digits + "-_")
    assert all(c in allowed for c in token)


def test_generate_token_is_unique() -> None:
    assert len({generate_token() for _ in range(100)}) == 100


def test_hash_token_is_sha256_hexdigest() -> None:
    token = "example-one-time-token"
    digest = hash_token(token)
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert len(digest) == 64


def test_hash_token_deterministic() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)
