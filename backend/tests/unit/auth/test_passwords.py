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


def test_authenticate_db_user_none() -> None:
    assert authenticate_db_user("any", None) is False


def test_authenticate_db_user_inactive() -> None:
    class FakeUser:
        active = False
        password_hash = _h("correct")

    assert authenticate_db_user("correct", FakeUser()) is False


def test_authenticate_db_user_no_hash() -> None:
    class FakeUser:
        active = True
        password_hash = None

    assert authenticate_db_user("any", FakeUser()) is False


def test_authenticate_db_user_correct() -> None:
    class FakeUser:
        active = True
        password_hash = _h("CorrectHorseBattery99!")

    assert authenticate_db_user("CorrectHorseBattery99!", FakeUser()) is True


def test_authenticate_db_user_wrong() -> None:
    class FakeUser:
        active = True
        password_hash = _h("CorrectHorseBattery99!")

    assert authenticate_db_user("wrong", FakeUser()) is False


# ---------------------------------------------------------------------------
# password_entropy_bits
# ---------------------------------------------------------------------------


def test_entropy_empty() -> None:
    assert password_entropy_bits("") == 0.0


def test_entropy_lowercase_only() -> None:
    bits = password_entropy_bits("abcdefgh")
    assert 35 < bits < 40  # 8 * log2(26) ~= 37.6


def test_entropy_mixed_case() -> None:
    bits = password_entropy_bits("AbCdEfGh")
    # 8 * log2(52) ~= 45.6
    assert 44 < bits < 47


def test_entropy_all_pools() -> None:
    bits = password_entropy_bits("Ab1!DefGh")
    # 9 * log2(94) ~= 59.0
    assert 58 < bits < 61


def test_entropy_single_repeated_lowercase() -> None:
    bits = password_entropy_bits("aaaaaaaa")
    assert 35 < bits < 40


def test_entropy_unicode_no_effect_on_pool() -> None:
    bits = password_entropy_bits("♥")
    assert bits == 0.0


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
