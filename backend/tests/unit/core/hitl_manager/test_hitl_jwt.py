"""Integration tests: HITLManager JWT claim tokens."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from jose import jwt as jose_jwt

from modulo.auth.jwt import _ALGORITHM, create_claim_token
from modulo.core.hitl_manager import (
    ClaimTokenExpiredError,
    ClaimTokenInvalidError,
    GateAlreadyDecidedError,
    GateNotFoundError,
    HITLManager,
)
from modulo.db.models.hitl_claim import HitlClaim

_KEY = "test_secret_key_32_bytes_long!!!!!!"
_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_USER = uuid.uuid4()
_GATE = "review-step"


def _gate(
    *,
    account_id: uuid.UUID | None = None,
    claim_token: str | None = None,
    expires_at: datetime | None = None,
    decision: str | None = None,
    claimed_at: datetime | None = None,
) -> HitlClaim:
    g = MagicMock(spec=HitlClaim)
    g.id = uuid.uuid4()
    g.run_id = _RUN
    g.gate_id = _GATE
    g.pipeline_id = _PIPELINE
    g.organisation_id = _ORG
    g.account_id = account_id
    g.claimed_at = claimed_at or (datetime.now(UTC) if account_id else None)
    g.claim_token = claim_token
    g.expires_at = expires_at
    g.decision = decision
    g.decision_at = None
    g.required_team_id = None
    return g


def _begin_nested(session: AsyncMock) -> None:
    """Add begin_nested async context manager to a session mock."""
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)


def _session_get(return_value: Any = None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = return_value
    scalars_result = MagicMock()
    scalars_result.__iter__ = lambda self: iter([return_value] if return_value else [])
    result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    _begin_nested(session)
    return session


def _session_claim(
    *,
    pre_check_gate: HitlClaim | None = None,
    claimed_gate: HitlClaim | None = None,
) -> AsyncMock:
    """Session mock for claim(): pre-check SELECT, UPDATE RETURNING, post-check SELECT."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    _begin_nested(session)

    pre_result = MagicMock()
    pre_result.scalar_one_or_none.return_value = pre_check_gate

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = uuid.uuid4() if pre_check_gate is not None else None

    post_result = MagicMock()
    post_result.scalar_one_or_none.return_value = claimed_gate

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return pre_result
        if call_count == 2:
            return update_result
        return post_result

    session.execute = _execute
    session.get = AsyncMock(return_value=claimed_gate)
    return session


def _session_decide(
    *,
    update_returns_id: uuid.UUID | None = None,
    diagnosis_gate: HitlClaim | None = None,
    session_get_gate: HitlClaim | None = None,
) -> AsyncMock:
    """Session mock for _decide(): UPDATE RETURNING, optional diagnosis SELECT, session.get."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    _begin_nested(session)

    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = update_returns_id

    diag_result = MagicMock()
    diag_result.scalar_one_or_none.return_value = diagnosis_gate

    call_count = 0

    async def _execute(stmt: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return update_result
        return diag_result

    session.execute = _execute
    session.get = AsyncMock(return_value=session_get_gate)
    return session


def _make_jwt_kwargs(**overrides: Any) -> dict[str, Any]:
    """Default kwargs for create_claim_token, overridable."""
    return {
        "subject": str(_USER),
        "secret_key": _KEY,
        "run_id": str(_RUN),
        "gate_id": _GATE,
        "client_id": str(_USER),
        **overrides,
    }


# ---------------------------------------------------------------------------
# claim() generates JWT
# ---------------------------------------------------------------------------


async def test_claim_succeeds_with_jwt_secret_key() -> None:
    """claim() with a secret_key still succeeds and returns the gate."""
    unclaimed_gate = _gate()  # no decision, no account_id
    claimed_gate = _gate(
        account_id=_USER,
        claim_token="jwt-will-be-set",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    session = _session_claim(pre_check_gate=unclaimed_gate, claimed_gate=claimed_gate)
    mgr = HITLManager(secret_key=_KEY)
    result = await mgr.claim(session, run_id=_RUN, gate_id=_GATE, org_id=_ORG, claimant_id=_USER)
    assert result is claimed_gate


def test_create_claim_token_roundtrip_unittest() -> None:
    """create_claim_token generates a valid JWT with correct scope."""
    token = create_claim_token(**_make_jwt_kwargs())
    payload = jose_jwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    assert payload["purpose"] == "claim_token"
    assert payload["run_id"] == str(_RUN)
    assert payload["gate_id"] == _GATE
    assert payload["client_id"] == str(_USER)
    assert payload["sub"] == str(_USER)


def test_create_claim_token_default_expiry_15_minutes() -> None:
    """create_claim_token defaults to 15-minute TTL."""
    token = create_claim_token(**_make_jwt_kwargs())
    payload = jose_jwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    exp_ts: float = payload["exp"]
    iat_ts: float = payload["iat"]
    assert 14 <= (exp_ts - iat_ts) / 60 <= 15


def test_create_claim_token_custom_expiry() -> None:
    """create_claim_token respects custom expiry_minutes."""
    token = create_claim_token(**_make_jwt_kwargs(expiry_minutes=60))
    payload = jose_jwt.decode(token, _KEY, algorithms=[_ALGORITHM])
    exp_ts: float = payload["exp"]
    iat_ts: float = payload["iat"]
    assert 59 <= (exp_ts - iat_ts) / 60 <= 60


# ---------------------------------------------------------------------------
# approve() validates JWT scope
# ---------------------------------------------------------------------------


async def test_approve_validates_jwt_scope() -> None:
    """approve() validates a well-formed JWT with matching scope."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(_RUN),
        gate_id=_GATE,
        client_id=str(_USER),
    )
    gate = _gate(account_id=_USER, claim_token=token, expires_at=future)
    gate_decided = _gate(decision="approved", claim_token=None, account_id=None)
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager(secret_key=_KEY)
    result = await mgr.approve(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token=token,
    )
    assert result.decision == "approved"
    assert result.claim_token is None


async def test_approve_rejects_expired_jwt() -> None:
    """approve() rejects a JWT whose exp is in the past."""
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(_RUN),
        gate_id=_GATE,
        client_id=str(_USER),
        expiry_minutes=-1,
    )
    session = AsyncMock()
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token=token,
        )


async def test_approve_rejects_client_id_mismatch() -> None:
    """approve() rejects a JWT scoped to a different run_id."""
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(uuid.uuid4()),  # wrong run_id
        gate_id=_GATE,
        client_id=str(_USER),
    )
    session = AsyncMock()
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token=token,
        )


async def test_approve_rejects_wrong_gate_id() -> None:
    """approve() rejects a JWT with non-matching gate_id."""
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(_RUN),
        gate_id="wrong-gate",
        client_id=str(_USER),
    )
    session = AsyncMock()
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token=token,
        )


async def test_approve_rejects_tampered_token() -> None:
    """approve() rejects a JWT that has been tampered with."""
    stored_token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(_RUN),
        gate_id=_GATE,
        client_id=str(_USER),
    )
    tampered = stored_token[:-5] + "XXXXX"
    session = AsyncMock()
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token=tampered,
        )


# ---------------------------------------------------------------------------
# reject() validates JWT
# ---------------------------------------------------------------------------


async def test_reject_validates_jwt() -> None:
    """reject() validates a well-formed JWT claim token."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(_RUN),
        gate_id=_GATE,
        client_id=str(_USER),
    )
    gate = _gate(account_id=_USER, claim_token=token, expires_at=future)
    gate_decided = _gate(decision="rejected", claim_token=None, account_id=None)
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager(secret_key=_KEY)
    result = await mgr.reject(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token=token,
    )
    assert result.decision == "rejected"
    assert result.claim_token is None


async def test_reject_rejects_expired_jwt() -> None:
    """reject() rejects an expired JWT."""
    token = create_claim_token(
        str(_USER),
        _KEY,
        run_id=str(_RUN),
        gate_id=_GATE,
        client_id=str(_USER),
        expiry_minutes=-1,
    )
    session = AsyncMock()
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(ClaimTokenExpiredError):
        await mgr.reject(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token=token,
        )


# ---------------------------------------------------------------------------
# Opaque token backwards compatibility
# ---------------------------------------------------------------------------


async def test_approve_opaque_token_backwards_compat() -> None:
    """approve() still works with opaque (non-JWT) tokens when secret_key is set."""
    gate_decided = _gate(decision="approved", claim_token=None, account_id=None)
    gate = _gate(account_id=_USER, claim_token="opaque-token-123")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager(secret_key=_KEY)
    result = await mgr.approve(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="opaque-token-123",
    )
    assert result.decision == "approved"


async def test_reject_opaque_token_backwards_compat() -> None:
    """reject() still works with opaque (non-JWT) tokens when secret_key is set."""
    gate_decided = _gate(decision="rejected", claim_token=None, account_id=None)
    gate = _gate(account_id=_USER, claim_token="opaque-token-456")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager(secret_key=_KEY)
    result = await mgr.reject(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="opaque-token-456",
    )
    assert result.decision == "rejected"


async def test_approve_no_secret_key_still_uses_opaque() -> None:
    """Without a secret_key, HITLManager uses opaque tokens (no change)."""
    gate_decided = _gate(decision="approved", claim_token=None, account_id=None)
    gate = _gate(account_id=_USER, claim_token="plain-token")
    session = _session_decide(update_returns_id=gate.id, session_get_gate=gate_decided)
    mgr = HITLManager()  # no secret_key
    result = await mgr.approve(
        session,
        run_id=_RUN,
        gate_id=_GATE,
        org_id=_ORG,
        claim_token="plain-token",
    )
    assert result.decision == "approved"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_gate_not_found_with_jwt_manager() -> None:
    """GateNotFoundError still raised when secret_key is configured."""
    session = _session_get(return_value=None)
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(GateNotFoundError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="anything",
        )


async def test_already_decided_with_jwt_manager() -> None:
    """GateAlreadyDecidedError still raised when secret_key is configured."""
    gate_decided = _gate(decision="approved")
    # Use opaque token so JWT validation is skipped; error comes from DB diagnosis
    session = _session_decide(update_returns_id=None, diagnosis_gate=gate_decided)
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(GateAlreadyDecidedError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token="opaque-already-decided",
        )


async def test_approve_rejects_jwt_with_wrong_key() -> None:
    """A JWT signed with a different key is rejected as invalid."""
    different_key = "a_completely_different_key_1234567890123"
    token = create_claim_token(
        str(_USER),
        different_key,
        run_id=str(_RUN),
        gate_id=_GATE,
        client_id=str(_USER),
    )
    session = AsyncMock()
    mgr = HITLManager(secret_key=_KEY)
    with pytest.raises(ClaimTokenInvalidError):
        await mgr.approve(
            session,
            run_id=_RUN,
            gate_id=_GATE,
            org_id=_ORG,
            claim_token=token,
        )
