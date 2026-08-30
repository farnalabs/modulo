"""JWT encode/decode unit tests."""

import base64
import json
import time
from datetime import UTC, datetime

import jwt as pyjwt
import pytest
from jwt import InvalidTokenError as JWTError

from modulo.auth.jwt import (
    _ALGORITHM,
    create_access_token,
    create_claim_token,
    create_refresh_token,
    create_ws_token,
    decode_claim_token,
    decode_principal,
    decode_refresh_token_claims,
    refresh_access_token,
)

_KEY = "a_sufficiently_long_secret_key_32b"
_ORG = "00000000-0000-0000-0000-000000000001"
_ACCOUNT = "11111111-1111-1111-1111-111111111111"
_RUN = "22222222-2222-2222-2222-222222222222"
_GATE = "review-step"


def _make_access_token(subject: str = "alice") -> str:
    return create_access_token(subject, _KEY, organisation_id=_ORG, account_id=_ACCOUNT, org_role="admin")


def test_create_access_token_and_decode_principal_roundtrip() -> None:
    token = _make_access_token()
    principal = decode_principal(token, _KEY)
    assert principal.username == "alice"


def test_create_access_token_respects_custom_ttl() -> None:
    from datetime import timedelta

    token = create_access_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role="admin",
        ttl_minutes=120,
    )
    claims = pyjwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    now = datetime.now(UTC)
    exp = claims["exp"] if isinstance(claims["exp"], datetime) else datetime.fromtimestamp(claims["exp"], tz=UTC)
    # 120 minutes from now (allow 2 min slack for clock drift)
    assert timedelta(minutes=118) <= (exp - now) <= timedelta(minutes=122)


def test_decode_principal_user_id_proxies_account_id() -> None:
    token = _make_access_token()
    principal = decode_principal(token, _KEY)
    assert principal.user_id == principal.account_id


def test_decode_principal_coerces_non_bool_is_system_admin_to_false() -> None:
    future = int(time.time()) + 3600
    claims = {
        "sub": "alice",
        "org_id": _ORG,
        "account_id": _ACCOUNT,
        "org_role": "admin",
        "is_system_admin": "yes",
        "iat": future - 3600,
        "exp": future,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    principal = decode_principal(token, _KEY)
    assert principal.is_system_admin is False


def test_decode_principal_rejects_malformed_account_uuid() -> None:
    future = int(time.time()) + 3600
    claims = {
        "sub": "alice",
        "org_id": _ORG,
        "account_id": "not-a-uuid",
        "org_role": "admin",
        "iat": future - 3600,
        "exp": future,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="malformed identity UUID"):
        decode_principal(token, _KEY)


def test_decode_principal_rejects_wrong_key() -> None:
    token = _make_access_token()
    with pytest.raises(JWTError):
        decode_principal(token, "wrong_key_but_long_enough_to_pass_validator")


def test_expired_token_raises() -> None:
    past = int(time.time()) - 3600
    claims = {
        "sub": "alice",
        "org_id": _ORG,
        "account_id": _ACCOUNT,
        "org_role": "admin",
        "iat": past - 86400,
        "exp": past,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError):
        decode_principal(token, _KEY)


def test_none_algorithm_rejected() -> None:
    """Tokens with alg:none must be rejected at decode time.

    Manually constructs a JWT with alg: none (bypassing PyJWT encode-time
    validation) and verifies that decode_principal rejects it since HS256 is the
    only allowed algorithm.
    """
    claims = {
        "sub": "alice",
        "org_id": _ORG,
        "account_id": _ACCOUNT,
        "org_role": "admin",
        "exp": int(time.time()) + 3600,
    }
    header_b64 = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    token = f"{header_b64}.{payload_b64}."
    with pytest.raises(JWTError):
        decode_principal(token, _KEY)


def test_missing_sub_raises() -> None:
    claims = {"org_id": _ORG, "account_id": _ACCOUNT, "org_role": "admin", "exp": int(time.time()) + 3600}
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="sub"):
        decode_principal(token, _KEY)


def test_empty_sub_raises() -> None:
    claims = {"sub": "", "org_id": _ORG, "account_id": _ACCOUNT, "org_role": "admin", "exp": int(time.time()) + 3600}
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="sub"):
        decode_principal(token, _KEY)


def test_token_is_string() -> None:
    token = _make_access_token("bob")
    assert isinstance(token, str)
    assert len(token) > 0


def test_token_carries_org_context() -> None:
    token = _make_access_token("alice")
    claims = pyjwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    assert claims["org_id"] == _ORG
    assert claims["org_role"] == "admin"


def test_decode_principal_validates_tenant_identity() -> None:
    org_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    account_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    token = create_access_token(
        "alice",
        _KEY,
        organisation_id=org_id,
        account_id=account_id,
        org_role="operator",
    )
    principal = decode_principal(token, _KEY)
    assert principal.username == "alice"
    assert str(principal.organisation_id) == org_id
    assert str(principal.account_id) == account_id
    assert principal.org_role == "operator"


def test_decode_principal_accepts_malformed_org_id_as_none() -> None:
    token = create_access_token("alice", _KEY, organisation_id="not-a-uuid", account_id=_ACCOUNT, org_role="admin")
    principal = decode_principal(token, _KEY)
    assert principal.organisation_id is None
    assert principal.username == "alice"
    assert principal.org_role == "admin"


def test_decode_principal_rejects_token_without_account_id() -> None:
    claims = {
        "sub": "admin",
        "org_id": _ORG,
        "org_role": "admin",
        "exp": int(time.time()) + 3600,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="account_id"):
        decode_principal(token, _KEY)


# ---------------------------------------------------------------------------
# allowed_purposes
# ---------------------------------------------------------------------------


def test_decode_principal_accepts_ws_token_with_allowed_purpose() -> None:
    token = create_ws_token("alice", _KEY, organisation_id=_ORG, account_id=_ACCOUNT, org_role="admin")
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
        account_id=_ACCOUNT,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    with pytest.raises(JWTError, match="purpose"):
        decode_principal(token, _KEY, allowed_purposes=["ws"])


def test_decode_principal_multiple_allowed_purposes() -> None:
    ws = create_ws_token("alice", _KEY, organisation_id=_ORG, account_id=_ACCOUNT, org_role="admin")
    refresh = create_refresh_token(
        "bob",
        _KEY,
        organisation_id=_ORG,
        account_id=_ACCOUNT,
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
        account_id=_ACCOUNT,
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
        account_id=_ACCOUNT,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    payload = pyjwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    assert payload.get("purpose") == "refresh"
    assert payload.get("token_family") == "f"
    assert payload.get("token_sequence") == 1
    exp_ts: float = payload["exp"]
    iat_ts: float = payload["iat"]
    exp = datetime.fromtimestamp(exp_ts, tz=UTC)
    iat = datetime.fromtimestamp(iat_ts, tz=UTC)
    assert 167 <= (exp - iat).total_seconds() / 3600 <= 168


def test_decode_refresh_token_claims_roundtrip() -> None:
    token = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role="admin",
        token_family="f",
        token_sequence=1,
    )
    payload = decode_refresh_token_claims(token, _KEY)
    assert payload["purpose"] == "refresh"
    assert payload["token_family"] == "f"
    assert payload["token_sequence"] == 1


def test_decode_refresh_token_claims_rejects_access_token() -> None:
    with pytest.raises(JWTError, match="not a refresh token"):
        decode_refresh_token_claims(_make_access_token(), _KEY)


def test_refresh_access_token_returns_valid_access_token() -> None:
    refresh = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=_ORG,
        account_id=_ACCOUNT,
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
    ws = create_ws_token("alice", _KEY, organisation_id=_ORG, account_id=_ACCOUNT, org_role="admin")
    with pytest.raises(JWTError):
        refresh_access_token(ws, _KEY)


def test_refresh_access_token_carries_context() -> None:
    org_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    account_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    refresh = create_refresh_token(
        "alice",
        _KEY,
        organisation_id=org_id,
        account_id=account_id,
        org_role="operator",
        token_family="f",
        token_sequence=1,
    )
    new_token = refresh_access_token(refresh, _KEY)
    principal = decode_principal(new_token, _KEY)
    assert principal.username == "alice"
    assert str(principal.organisation_id) == org_id
    assert str(principal.account_id) == account_id
    assert principal.org_role == "operator"


# ---------------------------------------------------------------------------
# create_claim_token / decode_claim_token
# ---------------------------------------------------------------------------


def test_create_claim_token_roundtrip() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    payload = decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
    assert payload["sub"] == _ACCOUNT
    assert payload["purpose"] == "claim_token"
    assert payload["run_id"] == _RUN
    assert payload["gate_id"] == _GATE
    assert payload["client_id"] == _ACCOUNT


def test_create_claim_token_default_15_min_expiry() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    payload = decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
    exp_ts: float = payload["exp"]  # type: ignore[assignment]
    iat_ts: float = payload["iat"]  # type: ignore[assignment]
    exp = datetime.fromtimestamp(exp_ts, tz=UTC)
    iat = datetime.fromtimestamp(iat_ts, tz=UTC)
    assert 14 <= (exp - iat).seconds // 60 <= 15


def test_create_claim_token_custom_expiry() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
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
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    with pytest.raises(JWTError):
        decode_claim_token(token, "wrong_key_32_bytes_minimum_______", run_id=_RUN, gate_id=_GATE)


def test_decode_claim_token_wrong_run_id_raises() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    with pytest.raises(JWTError, match="run_id"):
        decode_claim_token(token, _KEY, run_id=_RUN + "x", gate_id=_GATE)


def test_decode_claim_token_wrong_gate_id_raises() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    with pytest.raises(JWTError, match="gate_id"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id="wrong-step")


def test_decode_claim_token_client_id_mismatch_raises() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    other = "99999999-9999-9999-9999-999999999999"
    with pytest.raises(JWTError, match="client_id"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE, expected_client_id=other)


def test_decode_claim_token_accepts_matching_client_id() -> None:
    token = create_claim_token(
        str(_ACCOUNT),
        _KEY,
        run_id=_RUN,
        gate_id=_GATE,
        client_id=str(_ACCOUNT),
    )
    payload = decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE, expected_client_id=str(_ACCOUNT))
    assert payload["client_id"] == _ACCOUNT


def test_decode_claim_token_expired_raises() -> None:
    past = int(time.time()) - 60
    claims = {
        "sub": str(_ACCOUNT),
        "purpose": "claim_token",
        "run_id": _RUN,
        "gate_id": _GATE,
        "client_id": str(_ACCOUNT),
        "iat": past - 900,
        "exp": past,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)


def test_decode_claim_token_missing_purpose_raises() -> None:
    future = int(time.time()) + 3600
    claims = {
        "sub": str(_ACCOUNT),
        "run_id": _RUN,
        "gate_id": _GATE,
        "client_id": str(_ACCOUNT),
        "iat": future - 3600,
        "exp": future,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="purpose"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)


def test_decode_claim_token_wrong_purpose_raises() -> None:
    future = int(time.time()) + 3600
    claims = {
        "sub": str(_ACCOUNT),
        "purpose": "access",
        "run_id": _RUN,
        "gate_id": _GATE,
        "client_id": str(_ACCOUNT),
        "iat": future - 3600,
        "exp": future,
    }
    token = pyjwt.encode(claims, _KEY, algorithm=_ALGORITHM)
    with pytest.raises(JWTError, match="purpose"):
        decode_claim_token(token, _KEY, run_id=_RUN, gate_id=_GATE)
