"""Password hashing, verification, and entropy tests for v1 user management."""

import bcrypt as _bcrypt_lib
import pytest

from modulo.auth.passwords import (
    authenticate_db_user,
    hash_password,
    password_entropy_bits,
    validate_password_strength,
    verify_password,
)

_VALID_32 = "a" * 32


def _h(password: str) -> str:
    return _bcrypt_lib.hashpw(password.encode(), _bcrypt_lib.gensalt()).decode()


# ---------------------------------------------------------------------------
# hash_password / verify_password
# ---------------------------------------------------------------------------


def test_hash_and_verify_roundtrip() -> None:
    pw = "SuperSecret123!"
    h = hash_password(pw)
    assert h != pw
    assert h.startswith("$2b$") or h.startswith("$2a$")
    assert verify_password(pw, h) is True
    assert verify_password("wrong", h) is False


def test_verify_password_bad_hash_returns_false() -> None:
    assert verify_password("any", "not-a-valid-hash") is False


# ---------------------------------------------------------------------------
# authenticate_db_user
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("password", "user_active", "user_hash", "expected"),
    [
        ("any", False, _h("correct"), False),
        ("any", True, None, False),
        ("CorrectHorseBattery99!", True, _h("CorrectHorseBattery99!"), True),
        ("wrong", True, _h("CorrectHorseBattery99!"), False),
    ],
)
def test_authenticate_db_user(password: str, user_active: bool, user_hash: str | None, expected: bool) -> None:
    class FakeUser:
        active = user_active
        password_hash = user_hash

    result = authenticate_db_user(password, FakeUser())
    assert result is expected


def test_authenticate_db_user_none() -> None:
    assert authenticate_db_user("any", None) is False


# ---------------------------------------------------------------------------
# password_entropy_bits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pw", "lo", "hi"),
    [
        ("", 0.0, 0.0),
        ("abcdefgh", 35, 40),
        ("AbCdEfGh", 44, 47),
        ("Ab1!DefGh", 58, 61),
        ("aaaaaaaa", 35, 40),
        ("♥", 0.0, 0.0),
    ],
)
def test_entropy(pw: str, lo: float, hi: float) -> None:
    bits = password_entropy_bits(pw)
    if lo == 0.0 and hi == 0.0:
        assert bits == 0.0
    else:
        assert lo < bits < hi


# ---------------------------------------------------------------------------
# validate_password_strength
# ---------------------------------------------------------------------------


def test_validate_strong_password() -> None:
    validate_password_strength("CorrectHorseBattery99!")


def test_validate_short_password() -> None:
    with pytest.raises(ValueError, match="at least 8 characters"):
        validate_password_strength("Ab1!")


def test_validate_low_entropy_long_password() -> None:
    # digits-only: 8 * log2(10) ≈ 26.6 bits < 30 minimum
    with pytest.raises(ValueError, match="too weak"):
        validate_password_strength("12345678")


def test_validate_all_lowercase_12_chars() -> None:
    # 12 * log2(26) = 56.4, which exceeds 30 bits
    validate_password_strength("abcdefghijkl")
