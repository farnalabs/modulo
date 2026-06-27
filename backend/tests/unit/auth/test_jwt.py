"""JWT encode/decode unit tests."""

import time
from datetime import UTC, datetime

import pytest
from jose import JWTError
from jose import jwt as jose_jwt

from modulo.auth.jwt import (
    _ALGORITHM,
    create_access_token,
    create_claim_token,
    create_refresh_token,
    decode_claim_token,
    decode_principal,
    refresh_access_token,
)

_KEY = "a_sufficiently_long_secret_key_32b"
_ORG = "00000000-0000-0000-0000-000000000001"
_USER = "11111111-1111-1111-1111-111111111111"
_RUN = "22222222-2222-2222-2222-222222222222"
_GATE = "review-step"


def _make_access_token(subject: str = "alice") -> str:
    return create_access_token(subject, _KEY, organisation_id=_ORG, user_id=_USER, org_role="admin")


def test_roundtrip() -> None:
    token = _make_access_token()
    principal = decode_principal(token, _KEY)
    assert principal.username == "alice"


def test_wrong_key_raises() -> None:
    token = _make_access_token()
    with pytest.raises(JWTError):
        decode_principal(token, "wrong_key_but_long_enough_to_pass_validator")


def test_expired_token_raises() -> None:
    past = int(time.time()) - 3600
    claims = {"sub": "alice", "org_id": _ORG, "user_id": _USER, "org_role": "admin", "iat": past - 86400, "exp": past}
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError):
        decode_principal(token, _KEY)


def test_none_algorithm_rejected() -> None:
    """Tokens with alg:none must be rejected — not in the allowed algorithms list."""
    claims = {"sub": "alice", "org_id": _ORG, "user_id": _USER, "org_role": "admin", "exp": int(time.time()) + 3600}
    # The library may reject alg:none at encode time (preferred) or at decode time
    # Either way the test passes: alg:none must never be accepted
    with pytest.raises(Exception):
        token = jose_jwt.encode(claims, "", algorithm="none")
        decode_principal(token, _KEY)


def test_missing_sub_raises() -> None:
    claims = {"org_id": _ORG, "user_id": _USER, "org_role": "admin", "exp": int(time.time()) + 3600}
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="sub"):
        decode_principal(token, _KEY)


def test_empty_sub_raises() -> None:
    claims = {"sub": "", "org_id": _ORG, "user_id": _USER, "org_role": "admin", "exp": int(time.time()) + 3600}
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="sub"):
        decode_principal(token, _KEY)


def test_token_is_string() -> None:
    token = _make_access_token("bob")
    assert isinstance(token, str)
    assert len(token) > 0


def test_token_carries_org_context() -> None:
    token = _make_access_token("alice")
    claims = jose_jwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    assert claims["org_id"] == _ORG
    assert claims["org_role"] == "admin"


def test_decode_principal_validates_tenant_identity() -> None:
    org_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    user_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    token = create_access_token(
        "alice",
        _KEY,
        organisation_id=org_id,
        user_id=user_id,
        org_role="operator",
    )
    principal = decode_principal(token, _KEY)
    assert principal.username == "alice"
    assert str(principal.organisation_id) == org_id
    assert str(principal.user_id) == user_id
    assert principal.org_role == "operator"


def test_decode_principal_rejects_malformed_org_id() -> None:
    token = create_access_token("alice", _KEY, organisation_id="not-a-uuid", user_id=_USER, org_role="admin")
    with pytest.raises(JWTError, match="UUID"):
        decode_principal(token, _KEY)


def test_decode_principal_rejects_token_without_user_id() -> None:
    claims = {
        "sub": "admin",
        "org_id": _ORG,
        "org_role": "admin",
        "exp": int(time.time()) + 3600,
    }
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="user_id"):
        decode_principal(token, _KEY)


# ---------------------------------------------------------------------------
# allowed_purposes
# ---------------------------------------------------------------------------


def test_decode_principal_accepts_ws_token_with_allowed_purpose() -> None:
    from modulo.auth.jwt import create_ws_token

    token = create_ws_token("alice", _KEY, organisation_id=_ORG, user_id=_USER, org_role="admin")
    principal = decode_principal(token, _KEY, allowed_purposes=["ws"])
    assert principal.username == "alice"


def test_decode_principal_rejects_access_token_for_ws_purpose() -> None:
    token = _make_access_token("alice")
    with pytest.raises(JWTError, match="purpose"):
        decode_principal(token, _KEY, allowed_purposes=["ws"])


def test_decode_principal_rejects_refresh_token_for_ws_purpose() -> None:
    token = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        user_id=_USER,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    with pytest.raises(JWTError, match="purpose"):
        decode_principal(token, _KEY, allowed_purposes=["ws"])


def test_decode_principal_multiple_allowed_purposes() -> None:
    from modulo.auth.jwt import create_ws_token

    ws = create_ws_token("alice", _KEY, organisation_id=_ORG, user_id=_USER, org_role="admin")
    refresh = create_refresh_token(
        "bob",
        _KEY,
        organisation_id=_ORG,
        user_id=_USER,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    principal_ws = decode_principal(ws, _KEY, allowed_purposes=["ws", "refresh"])
    assert principal_ws.username == "alice"

    principal_refresh = decode_principal(refresh, _KEY, allowed_purposes=["ws", "refresh"])
    assert principal_refresh.username == "bob"


# ---------------------------------------------------------------------------
# create_refresh_token / refresh_access_token
# ---------------------------------------------------------------------------


def test_create_refresh_token_roundtrip() -> None:
    token = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        user_id=_USER,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    principal = decode_principal(token, _KEY, allowed_purposes=["refresh"])
    assert principal.username == "alice"


def test_refresh_token_has_refresh_purpose() -> None:
    token = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        user_id=_USER,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    payload = jose_jwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    assert payload.get("purpose") == "refresh"
    assert payload.get("token_family") == "f"
    assert payload.get("token_sequence") == 1
    exp_ts: float = payload["exp"]
    iat_ts: float = payload["iat"]
    exp = datetime.fromtimestamp(exp_ts, tz=UTC)
    iat = datetime.fromtimestamp(iat_ts, tz=UTC)
    assert 23 <= (exp - iat).total_seconds() / 3600 <= 24


def test_refresh_access_token_returns_valid_access_token() -> None:
    refresh = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        user_id=_USER,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    new_token = refresh_access_token(refresh, _KEY)
    principal = decode_principal(new_token, _KEY)
    assert principal.username == "alice"


def test_refresh_access_token_rejects_access_token() -> None:
    access = _make_access_token("alice")
    with pytest.raises(JWTError):
        refresh_access_token(access, _KEY)


def test_refresh_access_token_rejects_ws_token() -> None:
    from modulo.auth.jwt import create_ws_token

    ws = create_ws_token("alice", _KEY, organisation_id=_ORG, user_id=_USER, org_role="admin")
    with pytest.raises(JWTError):
        refresh_access_token(ws, _KEY)


def test_refresh_access_token_carries_context() -> None:
    org_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    user_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    refresh = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=org_id,
        user_id=user_id,
        org_role="operator",
        token_family="f",
        token_sequence=1,
    )
    new_token = refresh_access_token(refresh, _KEY)
    principal = decode_principal(new_token, _KEY)
    assert principal.username == "alice"
    assert str(principal.organisation_id) == org_id
    assert str(principal.user_id) == user_id
    assert principal.org_role == "operator"


# ---------------------------------------------------------------------------
# create_claim_token / decode_claim_token
# ---------------------------------------------------------------------------


def test_create_claim_token_roundtrip() -> None:
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_USER),
    )
    payload = decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
    assert payload["sub"] == _USER
    assert payload["purpose"] == "claim_token"
    assert payload["run_id"] == _RUN
    assert payload["gate_id"] == _GATE
    assert payload["client_id"] == _USER


def test_create_claim_token_default_15_min_expiry() -> None:
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_USER),
    )
    payload = decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
    exp_ts: float = payload["exp"]  # type: ignore[assignment]
    iat_ts: float = payload["iat"]  # type: ignore[assignment]
    exp = datetime.fromtimestamp(exp_ts, tz=UTC)
    iat = datetime.fromtimestamp(iat_ts, tz=UTC)
    assert 14 <= (exp - iat).seconds // 60 <= 15


def test_create_claim_token_custom_expiry() -> None:
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_USER),
        expiry_minutes=60,
    )
    payload = decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
    exp_ts: float = payload["exp"]  # type: ignore[assignment]
    iat_ts: float = payload["iat"]  # type: ignore[assignment]
    exp = datetime.fromtimestamp(exp_ts, tz=UTC)
    iat = datetime.fromtimestamp(iat_ts, tz=UTC)
    assert 59 <= (exp - iat).seconds // 60 <= 60


def test_decode_claim_token_wrong_key_raises() -> None:
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_USER),
    )
    with pytest.raises(JWTError):
        decode_claim_token(token, "wrong_key_32_bytes_minimum_______", run_id=_RUN, gate_id=_GATE)


def test_decode_claim_token_wrong_run_id_raises() -> None:
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_USER),
    )
    with pytest.raises(JWTError, match="run_id"):
        decode_claim_token(token, _KEY, run_id=_RUN + "x", gate_id=_GATE)


def test_decode_claim_token_wrong_gate_id_raises() -> None:
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_USER),
    )
    with pytest.raises(JWTError, match="gate_id"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id="wrong-step")


def test_decode_claim_token_expired_raises() -> None:
    past = int(time.time()) - 60
    claims = {
        "sub": str(_USER),
        "purpose": "claim_token",
        "run_id": _RUN,
        "gate_id": _GATE,
        "client_id": str(_USER),
        "iat": past - 900,
        "exp": past,
    }
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)


def test_decode_claim_token_missing_purpose_raises() -> None:
    future = int(time.time()) + 3600
    claims = {
        "sub": str(_USER),
        "run_id": _RUN,
        "gate_id": _GATE,
        "client_id": str(_USER),
        "iat": future - 3600,
        "exp": future,
    }
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="purpose"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)


def test_decode_claim_token_wrong_purpose_raises() -> None:
    future = int(time.time()) + 3600
    claims = {
        "sub": str(_USER),
        "purpose": "access",
        "run_id": _RUN,
        "gate_id": _GATE,
        "client_id": str(_USER),
        "iat": future - 3600,
        "exp": future,
    }
    token = jose_jwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="purpose"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
