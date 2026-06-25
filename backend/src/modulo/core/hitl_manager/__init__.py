"""HITLManager — atomic claim, approve, reject, and expiry for HITL gates.

Each pipeline run that reaches a HITL gate edge creates one `hitl_claims` row.
The claim lifecycle:

  unclaimed (claimed_by IS NULL)
      ↓  claim()
  claimed  (claimed_by set, claim_token set, expires_at set)
      ↓  approve() or reject()
  decided  (decision set, claim released)

Claim expiry resets a held claim back to unclaimed when `expires_at < NOW()`.

`human_only` enforcement is the responsibility of the ViewModel / API layer.
HITLManager records decisions but does not block them based on the flag.

v1 upgrade: claim_token is now a short-lived JWT (15-min TTL) scoped to
run_id + gate_id + client_id, signed with SECRET_KEY. Opaque tokens from the
alpha are still accepted for backwards compatibility.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import create_claim_token as _create_claim_jwt
from modulo.auth.jwt import decode_claim_token as _decode_claim_jwt
from modulo.db.models.hitl_claim import HitlClaim

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "AlreadyClaimedError",
    "ClaimTokenExpiredError",
    "ClaimTokenInvalidError",
    "GateAlreadyDecidedError",
    "GateNotFoundError",
    "HITLManager",
]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GateNotFoundError(KeyError):
    def __init__(self, run_id: uuid.UUID, gate_id: str) -> None:
        super().__init__(f"run={run_id} gate={gate_id}")
        self.run_id = run_id
        self.gate_id = gate_id


class AlreadyClaimedError(RuntimeError):
    def __init__(self, run_id: uuid.UUID, gate_id: str) -> None:
        super().__init__(f"Gate {gate_id!r} on run {run_id} is already claimed")
        self.run_id = run_id
        self.gate_id = gate_id


class ClaimTokenInvalidError(PermissionError):
    def __init__(self) -> None:
        super().__init__("claim_token is invalid")


class ClaimTokenExpiredError(PermissionError):
    def __init__(self) -> None:
        super().__init__("claim_token has expired")


class GateAlreadyDecidedError(RuntimeError):
    def __init__(self, run_id: uuid.UUID, gate_id: str) -> None:
        super().__init__(f"Gate {gate_id!r} on run {run_id} already has a decision")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

_TOKEN_BYTES = 32
_DEFAULT_EXPIRY_MINUTES = 15


class HITLManager:
    """Service for HITL gate state management. Stateless — pass a session each call.

    If a ``secret_key`` is provided, ``claim()`` generates a short-lived JWT
    (purpose=claim_token) instead of an opaque random string.  Approve/reject
    first try JWT validation and fall back to opaque token comparison for
    backwards compatibility.
    """

    def __init__(self, secret_key: str = "") -> None:
        self._secret_key = secret_key

    # ------------------------------------------------------------------
    # Gate creation
    # ------------------------------------------------------------------

    async def create_gate(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        pipeline_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> HitlClaim:
        """Insert a new unclaimed gate row. Idempotent if called again for same key."""
        # Check for existing row first (unique constraint: run_id + gate_id)
        existing = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
        if existing is not None:
            return existing
        gate = HitlClaim(
            organisation_id=org_id,
            run_id=run_id,
            gate_id=gate_id,
            pipeline_id=pipeline_id,
        )
        session.add(gate)
        await session.flush()
        return gate

    # ------------------------------------------------------------------
    # Claim (atomic)
    # ------------------------------------------------------------------

    async def claim(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claimant_id: uuid.UUID,
        expiry_minutes: int = _DEFAULT_EXPIRY_MINUTES,
    ) -> HitlClaim:
        """Atomically claim the gate.  Raises AlreadyClaimedError if held.

        If ``secret_key`` was provided at construction, the claim token is
        a signed JWT scoped to (run_id, gate_id, claimant_id).  Otherwise
        an opaque random string is used (alpha backwards compat).
        """
        now = datetime.now(UTC)
        # Generate token — JWT if we have a secret_key, else opaque
        if self._secret_key:
            token = _create_claim_jwt(
                str(claimant_id),
                self._secret_key,
                run_id=str(run_id),
                gate_id=gate_id,
                client_id=str(claimant_id),
                expiry_minutes=expiry_minutes,
            )
        else:
            token = secrets.token_urlsafe(_TOKEN_BYTES)

        stmt = (
            update(HitlClaim)
            .where(
                HitlClaim.run_id == run_id,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
                HitlClaim.claimed_by.is_(None),
                HitlClaim.decision.is_(None),
            )
            .values(
                claimed_by=claimant_id,
                claimed_at=now,
                claim_token=token,
                expires_at=now + timedelta(minutes=expiry_minutes),
            )
            .returning(HitlClaim.id)
        )
        result = await session.execute(stmt)
        claimed_id = result.scalar_one_or_none()
        if claimed_id is None:
            gate = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
            if gate is None:
                raise GateNotFoundError(run_id, gate_id)
            raise AlreadyClaimedError(run_id, gate_id)

        gate = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
        if gate is None:
            msg = f"Claim acquired but gate row vanished: run={run_id} gate={gate_id}"
            raise RuntimeError(msg)
        return gate

    # ------------------------------------------------------------------
    # Approve / Reject
    # ------------------------------------------------------------------

    async def approve(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
    ) -> HitlClaim:
        """Record approval. Raises on missing token, expired token, or decided gate."""
        return await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision="approved",
        )

    async def reject(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
    ) -> HitlClaim:
        """Record rejection. Raises on missing token, expired token, or decided gate."""
        return await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision="rejected",
        )

    # ------------------------------------------------------------------
    # Expiry
    # ------------------------------------------------------------------

    async def expire_stale(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Reset claims whose TTL has passed. Returns list of {run_id, gate_id} expired."""
        now = datetime.now(UTC)
        stmt = (
            update(HitlClaim)
            .where(
                HitlClaim.organisation_id == org_id,
                HitlClaim.expires_at < now,
                HitlClaim.claimed_by.is_not(None),
                HitlClaim.decision.is_(None),
            )
            .values(claimed_by=None, claimed_at=None, claim_token=None, expires_at=None)
            .returning(HitlClaim.run_id, HitlClaim.gate_id)
        )
        rows = (await session.execute(stmt)).all()
        return [{"run_id": r[0], "gate_id": r[1]} for r in rows]

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_gate(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
    ) -> HitlClaim | None:
        return await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)

    async def list_pending(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
    ) -> list[HitlClaim]:
        """All unclaimed, undecided gates for the org (run is awaiting_human)."""
        result = await session.execute(
            select(HitlClaim).where(
                HitlClaim.organisation_id == org_id,
                HitlClaim.claimed_by.is_(None),
                HitlClaim.decision.is_(None),
            )
        )
        return list(result.scalars())

    async def list_overdue(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        *,
        threshold_minutes: int = 30,
    ) -> list[dict[str, Any]]:
        """Return gates whose ``claimed_at`` exceeds the overdue threshold.

        Only gates that are claimed but not yet decided are considered.
        """
        now = datetime.now(UTC)
        threshold = timedelta(minutes=threshold_minutes)
        result = await session.execute(
            select(HitlClaim).where(
                HitlClaim.organisation_id == org_id,
                HitlClaim.claimed_by.is_not(None),
                HitlClaim.decision.is_(None),
                HitlClaim.claimed_at.is_not(None),
                HitlClaim.claimed_at < now - threshold,
            )
        )
        gates = list(result.scalars())
        return [
            {
                "run_id": g.run_id,
                "gate_id": g.gate_id,
                "claimed_by": g.claimed_by,
                "claimed_at": g.claimed_at,
                "minutes_overdue": int((now - g.claimed_at).total_seconds() / 60),
            }
            for g in gates
            if g.claimed_at is not None
        ]

    async def count_overdue(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        *,
        threshold_minutes: int = 30,
    ) -> int:
        """Return the number of gates that exceed the overdue threshold."""
        gates = await self.list_overdue(session, org_id, threshold_minutes=threshold_minutes)
        return len(gates)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
    ) -> HitlClaim | None:
        result = await session.execute(
            select(HitlClaim).where(
                HitlClaim.run_id == run_id,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def _decide(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
        decision: str,
    ) -> HitlClaim:
        now = datetime.now(UTC)
        stmt = (
            update(HitlClaim)
            .where(
                HitlClaim.run_id == run_id,
                HitlClaim.gate_id == gate_id,
                HitlClaim.organisation_id == org_id,
                HitlClaim.decision.is_(None),
                HitlClaim.claim_token == claim_token,
                HitlClaim.expires_at.is_not(None),
                HitlClaim.expires_at > now,
            )
            .values(
                decision=decision,
                decision_at=now,
                claimed_by=None,
                claim_token=None,
                expires_at=None,
            )
            .returning(HitlClaim.id)
        )
        result = await session.execute(stmt)
        gate_id_val = result.scalar_one_or_none()
        if gate_id_val is None:
            existing = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
            if existing is None:
                raise GateNotFoundError(run_id, gate_id)
            if existing.decision is not None:
                raise GateAlreadyDecidedError(run_id, gate_id)
            if existing.claim_token != claim_token:
                raise ClaimTokenInvalidError
            raise ClaimTokenExpiredError
        gate = await session.get(HitlClaim, gate_id_val, populate_existing=True)
        if gate is None:
            msg = f"Decision recorded but gate row vanished: run={run_id} gate={gate_id}"
            raise RuntimeError(msg)
        return gate

    @staticmethod
    def _looks_like_jwt(token: str) -> bool:
        """Rough heuristic: a JWT has exactly 2 dots (3 base64 segments)."""
        return token.count(".") == 2

    def _validate_claim_token(
        self,
        gate: HitlClaim,
        claim_token: str,
        run_id: uuid.UUID,
        gate_id: str,
    ) -> None:
        """Validate ``claim_token`` against the gate's stored token.

        When a ``secret_key`` is configured, tries JWT decode first and
        validates scope (run_id, gate_id).  **JWT decode failures (bad
        signature, expired, scope mismatch) are authoritative** — the
        request is rejected without falling through to opaque comparison.

        Opaque-string fallback only applies when the presented token is
        *not* a JWT (alpha backwards compatibility).
        """
        if self._secret_key:
            if self._looks_like_jwt(claim_token):
                try:
                    _decode_claim_jwt(
                        claim_token,
                        self._secret_key,
                        run_id=str(run_id),
                        gate_id=gate_id,
                    )
                    return
                except JWTError:
                    raise ClaimTokenInvalidError() from None
            # Not a JWT — try opaque comparison below

        if gate.claim_token == claim_token:
            return

        raise ClaimTokenInvalidError()
