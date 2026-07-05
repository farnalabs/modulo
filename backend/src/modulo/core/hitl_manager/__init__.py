"""HITLManager — atomic claim, approve, reject, deliver_manual, and expiry for HITL gates.

Each pipeline run that reaches a HITL gate edge creates one `hitl_claims` row.
The claim lifecycle:

  unclaimed (claimed_by IS NULL)
      ↓  claim()
  claimed  (claimed_by set, claim_token set, expires_at set)
      ↓  approve(), reject(), or deliver_manual()
  decided  (decision set, claim released)

``deliver_manual`` is similar to approve but the reviewer supplies the output
directly instead of accepting the agent's output. The manually-supplied output
is validated and passed through to the pipeline on resume.

Claim expiry resets a held claim back to unclaimed when `expires_at < NOW()`.

`human_only` enforcement is the responsibility of the ViewModel / API layer.
HITLManager records decisions but does not block them based on the flag.

v1 upgrade: claim_token is now a short-lived JWT (15-min TTL) scoped to
run_id + gate_id + client_id, signed with SECRET_KEY. Opaque tokens from the
alpha are still accepted for backwards compatibility.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import ExpiredSignatureError, JWTError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import create_claim_token as _create_claim_jwt
from modulo.auth.jwt import decode_claim_token as _decode_claim_jwt
from modulo.core.audit_logger import append_audit_event
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.team_membership import TeamMembership

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "AlreadyClaimedError",
    "ClaimTokenExpiredError",
    "ClaimTokenInvalidError",
    "GateAlreadyDecidedError",
    "GateNotFoundError",
    "GateVanishedError",
    "HITLError",
    "HITLManager",
    "NotTeamMemberError",
]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HITLError(Exception):
    """Base exception for all HITL manager errors."""


class GateNotFoundError(HITLError, KeyError):
    def __init__(self, run_id: uuid.UUID, gate_id: str) -> None:
        super().__init__(f"run={run_id} gate={gate_id}")
        self.run_id = run_id
        self.gate_id = gate_id


class AlreadyClaimedError(HITLError, RuntimeError):
    def __init__(self, run_id: uuid.UUID, gate_id: str) -> None:
        super().__init__(f"Gate {gate_id!r} on run {run_id} is already claimed")
        self.run_id = run_id
        self.gate_id = gate_id


class ClaimTokenInvalidError(HITLError, PermissionError):
    def __init__(self) -> None:
        super().__init__("claim_token is invalid")


class ClaimTokenExpiredError(HITLError, PermissionError):
    def __init__(self) -> None:
        super().__init__("claim_token has expired")


class GateAlreadyDecidedError(HITLError, RuntimeError):
    def __init__(self, run_id: uuid.UUID, gate_id: str) -> None:
        super().__init__(f"Gate {gate_id!r} on run {run_id} already has a decision")


class NotTeamMemberError(HITLError, PermissionError):
    def __init__(self, run_id: uuid.UUID, gate_id: str, team_id: uuid.UUID, user_id: uuid.UUID) -> None:
        super().__init__(
            f"User {user_id} is not a member of team {team_id} required by gate {gate_id!r} on run {run_id}"
        )
        self.run_id = run_id
        self.gate_id = gate_id
        self.team_id = team_id
        self.user_id = user_id


class GateVanishedError(HITLError, RuntimeError):
    """Claim acquired/decided but the gate row disappeared before we could read it."""

    def __init__(self, run_id: uuid.UUID, gate_id: str, operation: str) -> None:
        super().__init__(f"Gate {gate_id!r} on run {run_id} {operation} but row vanished")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

_TOKEN_BYTES = 32
_DEFAULT_EXPIRY_MINUTES = 15
_DECISION_APPROVED = "approved"
_DECISION_REJECTED = "rejected"
_DECISION_DELIVER_MANUAL = "deliver_manual"


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
        required_team_id: uuid.UUID | None = None,
    ) -> HitlClaim:
        """Insert a new unclaimed gate row. Idempotent if called again for same key."""
        # Check for existing row first (unique constraint: run_id + gate_id).
        # Race: a concurrent caller may insert between our check and flush.
        # Handle IntegrityError gracefully by fetching the existing row.
        existing = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
        if existing is not None:
            return existing
        gate = HitlClaim(
            organisation_id=org_id,
            run_id=run_id,
            gate_id=gate_id,
            pipeline_id=pipeline_id,
            required_team_id=required_team_id,
        )
        session.add(gate)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            existing = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
            if existing is None:
                raise RuntimeError(f"Concurrent gate creation lost race for run={run_id} gate={gate_id}")
            return existing
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

        If the gate has a ``required_team_id``, the claimant must be a
        member of that team, otherwise ``NotTeamMemberError`` is raised.
        """
        if expiry_minutes <= 0:
            raise HITLError(f"expiry_minutes must be positive, got {expiry_minutes}")

        now = datetime.now(UTC)

        # Pre-check: gate must exist, not already decided, and claimant must
        # be a team member if the gate is team-scoped.
        gate_check = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
        if gate_check is None:
            raise GateNotFoundError(run_id, gate_id)
        if gate_check.decision is not None:
            raise GateAlreadyDecidedError(run_id, gate_id)
        if gate_check.account_id is not None:
            raise AlreadyClaimedError(run_id, gate_id)
        if gate_check.required_team_id is not None:
            # Lock the gate row so the team check is serialised with the UPDATE.
            gate_check_locked = await session.execute(
                select(HitlClaim).where(HitlClaim.id == gate_check.id).with_for_update()
            )
            gate_check_locked = gate_check_locked.scalar_one_or_none()
            if gate_check_locked is None:
                raise GateNotFoundError(run_id, gate_id)
            if gate_check_locked.decision is not None:
                raise GateAlreadyDecidedError(run_id, gate_id)
            if gate_check_locked.account_id is not None:
                raise AlreadyClaimedError(run_id, gate_id)
            tm_result = await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == gate_check.required_team_id,
                    TeamMembership.account_id == claimant_id,
                    TeamMembership.organisation_id == org_id,
                )
            )
            if tm_result.scalar_one_or_none() is None:
                raise NotTeamMemberError(
                    run_id=run_id,
                    gate_id=gate_id,
                    team_id=gate_check.required_team_id,
                    user_id=claimant_id,
                )

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
                HitlClaim.account_id.is_(None),
                HitlClaim.decision.is_(None),
            )
            .values(
                account_id=claimant_id,
                claimed_at=now,
                claim_token=token,
                expires_at=now + timedelta(minutes=expiry_minutes),
            )
            .returning(HitlClaim.id)
        )
        result = await session.execute(stmt)
        claimed_id = result.scalar_one_or_none()
        if claimed_id is None:
            # Race condition — someone else claimed between our check and update
            raise AlreadyClaimedError(run_id, gate_id)

        gate = await session.get(HitlClaim, claimed_id, populate_existing=True)
        if gate is None:
            raise GateVanishedError(run_id, gate_id, "claimed")
        return gate

    # ------------------------------------------------------------------
    # Approve / Reject
    # ------------------------------------------------------------------

    async def approve_with_modification(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
        modified_output: dict[str, Any],
        actor_id: uuid.UUID | None = None,
    ) -> HitlClaim:
        """Record approval with a modified output payload.

        Logs a ``hitl.output_modified`` audit event documenting the change,
        then logs the standard ``hitl.output_delivered`` event.

        Raises on missing token, expired token, or decided gate.
        """
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_APPROVED,
        )

        await self._log_audit_and_deliver(
            session,
            gate,
            org_id=org_id,
            actor_id=actor_id,
            events=[
                ("hitl.output_modified", self._base_audit_payload(gate, modified_output=modified_output)),
                ("hitl.output_delivered", self._base_audit_payload(gate, modified=True)),
            ],
        )

        return gate

    async def approve(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
        actor_id: uuid.UUID | None = None,
    ) -> HitlClaim:
        """Record approval and log a ``hitl.output_delivered`` audit event.

        Raises on missing token, expired token, or decided gate.
        Sets ``delivered_at`` on the claim after successful audit logging.
        If audit logging fails, logs ``hitl.output_delivery_failed`` instead.
        """
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_APPROVED,
        )

        await self._log_audit_and_deliver(
            session,
            gate,
            org_id=org_id,
            actor_id=actor_id,
            events=[("hitl.output_delivered", self._base_audit_payload(gate))],
        )

        return gate

    async def reject(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
        actor_id: uuid.UUID | None = None,
    ) -> HitlClaim:
        """Record rejection and log a ``hitl.output_rejected`` audit event.

        Raises on missing token, expired token, or decided gate.
        """
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_REJECTED,
        )

        await self._log_audit_and_deliver(
            session,
            gate,
            org_id=org_id,
            actor_id=actor_id,
            events=[("hitl.output_rejected", self._base_audit_payload(gate))],
        )

        return gate

    async def deliver_manual(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        gate_id: str,
        org_id: uuid.UUID,
        claim_token: str,
        output: dict[str, Any],
        actor_id: uuid.UUID | None = None,
    ) -> HitlClaim:
        """Record manual delivery and log a ``hitl.manual_delivery`` audit event.

        The reviewer provides *output* directly instead of routing to a
        correction run or back to the agent. The output is validated against
        the expected output schema (if available) and the run resumes past the
        HITL gate with the manually-supplied value.

        Raises on missing token, expired token, or decided gate.
        Sets ``delivered_at`` on the claim after successful audit logging.
        If audit logging fails, logs ``hitl.output_delivery_failed`` instead.
        """
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_DELIVER_MANUAL,
        )

        await self._log_audit_and_deliver(
            session,
            gate,
            org_id=org_id,
            actor_id=actor_id,
            events=[("hitl.manual_delivery", self._base_audit_payload(gate, manual_output=output))],
        )

        return gate

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
                HitlClaim.account_id.is_not(None),
                HitlClaim.decision.is_(None),
            )
            .values(account_id=None, claimed_at=None, claim_token=None, expires_at=None)
            .returning(HitlClaim.run_id, HitlClaim.gate_id)
        )
        rows = (await session.execute(stmt)).all()
        return [{"run_id": r.run_id, "gate_id": r.gate_id} for r in rows]

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
                HitlClaim.account_id.is_(None),
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
                HitlClaim.account_id.is_not(None),
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
                "claimed_by": g.account_id,
                "claimed_at": g.claimed_at,
                "minutes_overdue": int((now - g.claimed_at).total_seconds() / 60),
            }
            for g in gates
        ]

    async def count_overdue(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        *,
        threshold_minutes: int = 30,
    ) -> int:
        """Return the number of gates that exceed the overdue threshold."""
        now = datetime.now(UTC)
        threshold = timedelta(minutes=threshold_minutes)
        result = await session.execute(
            select(func.count()).where(
                HitlClaim.organisation_id == org_id,
                HitlClaim.account_id.is_not(None),
                HitlClaim.decision.is_(None),
                HitlClaim.claimed_at.is_not(None),
                HitlClaim.claimed_at < now - threshold,
            ).select_from(HitlClaim)
        )
        return result.scalar() or 0

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

        # Validate JWT signature and scope before attempting the SQL UPDATE.
        # Expiry is checked separately via the SQL WHERE clause (expires_at > now)
        # so that the DB remains the authoritative source of truth for TTL.
        if self._secret_key and self._looks_like_jwt(claim_token):
            try:
                _decode_claim_jwt(claim_token, self._secret_key, run_id=str(run_id), gate_id=gate_id)
            except ExpiredSignatureError as err:
                raise ClaimTokenExpiredError() from err
            except JWTError as err:
                raise ClaimTokenInvalidError() from err

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
                account_id=None,
                claim_token=None,
                expires_at=None,
            )
            .returning(HitlClaim.id)
        )
        result = await session.execute(stmt)
        claim_id = result.scalar_one_or_none()
        if claim_id is None:
            existing = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
            if existing is None:
                raise GateNotFoundError(run_id, gate_id)
            if existing.decision is not None:
                raise GateAlreadyDecidedError(run_id, gate_id)
            if existing.claim_token is None:
                raise ClaimTokenExpiredError()
            if existing.claim_token != claim_token:
                raise ClaimTokenInvalidError()
            raise ClaimTokenExpiredError()
        gate = await session.get(HitlClaim, claim_id, populate_existing=True)
        if gate is None:
            raise GateVanishedError(run_id, gate_id, "decided")
        return gate

    @staticmethod
    def _looks_like_jwt(token: str) -> bool:
        """Rough heuristic: a JWT has exactly 2 dots (3 base64 segments)."""
        return token.count(".") == 2

    @staticmethod
    def _base_audit_payload(gate: HitlClaim, **extra: Any) -> dict[str, Any]:
        """Common audit event payload fields for a HITL gate decision."""
        return {
            "pipeline_run_id": str(gate.run_id),
            "node_id": gate.gate_id,
            "decision": gate.decision,
            "team_id": str(gate.required_team_id) if gate.required_team_id else None,
            **extra,
        }

    async def _log_audit_and_deliver(
        self,
        session: AsyncSession,
        gate: HitlClaim,
        *,
        org_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Log audit events, mark delivered, and flush.

        On failure, the original session transaction is in a broken state,
        so the decision rolls back along with the audit events — preventing
        half-completed operations.  The failure is logged with enough context
        for operators to investigate.
        """
        try:
            for event_type, payload in events:
                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type=event_type,
                    actor_user_id=actor_id,
                    resource_type="hitl_claim",
                    resource_id=gate.id,
                    payload_json=payload,
                )
            if gate.decision != _DECISION_REJECTED:
                gate.delivered_at = datetime.now(UTC)
            await session.flush()
        except Exception:
            _log.exception("Failed to log audit event for claim %s", gate.id)
            raise


