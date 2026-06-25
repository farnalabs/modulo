"""Password hashing, verification, and entropy enforcement.

Uses `bcrypt` directly (not passlib) because passlib's bcrypt backend detection
is incompatible with bcrypt 4.x which rejects passwords > 72 bytes in its
internal wrap-bug probe.

Password entropy: minimum Shannon entropy threshold enforced on creation.
"""

import math
import re

import bcrypt as _bcrypt_lib

from modulo.db.models.user import User

_MIN_ENTROPY_BITS = 30


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return _bcrypt_lib.hashpw(password.encode(), _bcrypt_lib.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return _bcrypt_lib.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False


def authenticate_db_user(password: str, user: User | None) -> bool:
    """Authenticate a user against a DB User record.

    Returns False if the user is None, inactive, or password is wrong.
    """
    if user is None:
        return False
    if not user.active:
        return False
    if not user.password_hash:
        return False
    return verify_password(password, user.password_hash)


def password_entropy_bits(password: str) -> float:
    """Calculate Shannon entropy of a password based on character pool size.

    Returns minimum entropy in bits.
    """
    pool_size = 0
    if re.search(r"[a-z]", password):
        pool_size += 26
    if re.search(r"[A-Z]", password):
        pool_size += 26
    if re.search(r"[0-9]", password):
        pool_size += 10
    if re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\/~`]', password):
        pool_size += 32

    if pool_size == 0:
        return 0.0

    return len(password) * math.log2(pool_size)


def validate_password_strength(password: str) -> None:
    """Validate password meets minimum entropy requirements.

    Raises ValueError if the password is too weak.
    """
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")

    entropy = password_entropy_bits(password)
    if entropy < _MIN_ENTROPY_BITS:
        raise ValueError(
            f"Password too weak: {entropy:.1f} entropy bits "
            f"(minimum {_MIN_ENTROPY_BITS}). Use a longer password "
            f"with a mix of character types."
        )
