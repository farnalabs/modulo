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

import asyncio
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jwt import ExpiredSignatureError
from jwt import InvalidTokenError as JWTError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.jwt import create_claim_token as _create_claim_jwt
from modulo.auth.jwt import decode_claim_token as _decode_claim_jwt
from modulo.core.audit_logger import append_audit_event
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.team_membership import TeamMembership

_log = logging.getLogger(__name__)

# Team roles allowed to claim a team-scoped HITL gate. A ``viewer`` membership
# grants read-only visibility — claiming (and therefore approving/rejecting) a
# team gate is a decision action, so it is restricted to members whose team
# role is ``runner`` or ``operator`` (mirroring the org-level ``hitl.claim``
# permission, which is runner-scoped).
_TEAM_CLAIM_ROLES: tuple[str, ...] = ("runner", "operator")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: tuple[str, ...] = (
    "AlreadyClaimedError",
    "ClaimTokenExpiredError",
    "ClaimTokenInvalidError",
    "DecisionPayloadError",
    "GateAlreadyDecidedError",
    "GateNotFoundError",
    "GateVanishedError",
    "HITLError",
    "HITLManager",
    "NotTeamMemberError",
)

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


class DecisionPayloadError(HITLError, ValueError):
    """The decision payload failed shape/size validation at write time."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

_TOKEN_BYTES = 32
_DEFAULT_EXPIRY_MINUTES = 15
_DECISION_APPROVED = "approved"
_DECISION_REJECTED = "rejected"
_DECISION_DELIVER_MANUAL = "deliver_manual"
# Bounded decision payload at write (B1): refuse payloads over this size with a
# clear 422 instead of silently truncating a human's manual output.
_DECISION_PAYLOAD_MAX_BYTES = 256 * 1024


class HITLManager:
    """Service for HITL gate state management. Stateless — pass a session each call.

    If a ``secret_key`` is provided, ``claim()`` generates a short-lived JWT
    (purpose=claim_token) instead of an opaque random string.  Approve/reject
    first try JWT validation and fall back to opaque token comparison for
    backwards compatibility.
    """

    def __init__(self, secret_key: str = "") -> None:  # nosec B107 — empty default disables JWT claim tokens (opaque tokens used instead); real key is injected at runtime
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
            expires_at=datetime.now(UTC) + timedelta(minutes=_DEFAULT_EXPIRY_MINUTES),
        )
        session.add(gate)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            existing = await self._get(session, run_id=run_id, gate_id=gate_id, org_id=org_id)
            if existing is None:
                raise RuntimeError(f"Concurrent gate creation lost race for run={run_id} gate={gate_id}") from None
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
            locked_result = await session.execute(
                select(HitlClaim).where(HitlClaim.id == gate_check.id).with_for_update()
            )
            locked_gate = locked_result.scalar_one_or_none()
            if locked_gate is None:
                raise GateNotFoundError(run_id, gate_id)
            if locked_gate.decision is not None:
                raise GateAlreadyDecidedError(run_id, gate_id)
            if locked_gate.account_id is not None:
                raise AlreadyClaimedError(run_id, gate_id)
            tm_result = await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == gate_check.required_team_id,
                    TeamMembership.account_id == claimant_id,
                    TeamMembership.organisation_id == org_id,
                    TeamMembership.role.in_(_TEAM_CLAIM_ROLES),
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

        # Re-verify team membership — the check above ran before the atomic
        # UPDATE, creating a TOCTOU window where the user could have been
        # removed from the team.  If they're no longer a member, undo the
        # claim and raise.
        if gate_check is not None and gate_check.required_team_id is not None:
            tm_still = await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == gate_check.required_team_id,
                    TeamMembership.account_id == claimant_id,
                    TeamMembership.organisation_id == org_id,
                    TeamMembership.role.in_(_TEAM_CLAIM_ROLES),
                )
            )
            if tm_still.scalar_one_or_none() is None:
                await session.execute(
                    update(HitlClaim)
                    .where(HitlClaim.id == claimed_id)
                    .values(account_id=None, claimed_at=None, claim_token=None, expires_at=now)
                )
                raise NotTeamMemberError(
                    run_id=run_id,
                    gate_id=gate_id,
                    team_id=gate_check.required_team_id,
                    user_id=claimant_id,
                )

        # PRD §8.12 ``hitl_claimed``: the claim acquisition itself was never
        # audited — only the later ``hitl.claim_expired``/decision events were.
        # Failure-isolated: a broken audit append must never fail the claim
        # (the savepoint rollback undoes only the audit write).
        try:
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="hitl_claimed",
                actor_user_id=claimant_id,
                resource_type="hitl_claim",
                resource_id=claimed_id,
                payload_json={
                    "pipeline_run_id": str(run_id),
                    "node_id": gate_id,
                    "team_id": str(gate_check.required_team_id) if gate_check.required_team_id else None,
                    "expiry_minutes": expiry_minutes,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "hitl_manager.claim_audit_failed",
                extra={"run_id": str(run_id), "gate_id": gate_id, "org_id": str(org_id)},
            )

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
        decision_payload: dict[str, Any] | None = None,
    ) -> HitlClaim:
        """Record approval with a modified output payload.

        Logs a ``hitl.output_modified`` audit event documenting the change,
        then logs the standard ``hitl.output_delivered`` event.

        Raises on missing token, expired token, or decided gate, and
        ``DecisionPayloadError`` when the supplied *decision_payload* violates
        the payload contract (FAR-541 iteration 4: ``_decide`` is the single
        stamp authority — foreign-stamped, non-serialisable, and oversized
        payloads are refused).
        """
        if decision_payload is None:
            decision_payload = {"action": "approved", "modified_output": modified_output}
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_APPROVED,
            decision_payload=decision_payload,
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
        decision_payload: dict[str, Any] | None = None,
    ) -> HitlClaim:
        """Record approval and log a ``hitl.output_delivered`` audit event.

        Raises on missing token, expired token, or decided gate, and
        ``DecisionPayloadError`` when the supplied *decision_payload* violates
        the payload contract (FAR-541 iteration 4: ``_decide`` is the single
        stamp authority — foreign-stamped, non-serialisable, and oversized
        payloads are refused).
        """
        if decision_payload is None:
            decision_payload = {"action": "approved"}
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_APPROVED,
            decision_payload=decision_payload,
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
        reason: str | None = None,
        decision_payload: dict[str, Any] | None = None,
    ) -> HitlClaim:
        """Record rejection and log a ``hitl.output_rejected`` audit event.

        Raises on missing token, expired token, or decided gate, and
        ``DecisionPayloadError`` when the supplied *decision_payload* violates
        the payload contract (FAR-541 iteration 4: ``_decide`` is the single
        stamp authority — foreign-stamped, non-serialisable, and oversized
        payloads are refused).
        """
        if decision_payload is None:
            decision_payload = {"action": "rejected"}
            if reason is not None:
                decision_payload["reason"] = reason
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_REJECTED,
            decision_payload=decision_payload,
        )

        extra: dict[str, Any] = {}
        if reason is not None:
            extra["reason"] = reason
        await self._log_audit_and_deliver(
            session,
            gate,
            org_id=org_id,
            actor_id=actor_id,
            events=[("hitl.output_rejected", self._base_audit_payload(gate, **extra))],
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
        decision_payload: dict[str, Any] | None = None,
    ) -> HitlClaim:
        """Record manual delivery and log a ``hitl.manual_delivery`` audit event.

        The reviewer provides *output* directly instead of routing to a
        correction run or back to the agent. The output is validated against
        the expected output schema (if available) and the run resumes past the
        HITL gate with the manually-supplied value.

        Raises on missing token, expired token, or decided gate, and
        ``DecisionPayloadError`` when the supplied *decision_payload* violates
        the payload contract (FAR-541 iteration 4: ``_decide`` is the single
        stamp authority — foreign-stamped, non-serialisable, and oversized
        payloads are refused).
        Sets ``delivered_at`` on the claim after successful audit logging.
        On audit failure the exception propagates and the transaction rolls back.
        """
        if decision_payload is None:
            decision_payload = {"action": "deliver_manual", "output": output}
        gate = await self._decide(
            session,
            run_id=run_id,
            gate_id=gate_id,
            org_id=org_id,
            claim_token=claim_token,
            decision=_DECISION_DELIVER_MANUAL,
            decision_payload=decision_payload,
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
            .values(account_id=None, claimed_at=None, claim_token=None, expires_at=now)
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
        now = datetime.now(UTC)
        threshold = timedelta(minutes=threshold_minutes)
        result = await session.execute(
            select(func.count())
            .where(
                HitlClaim.organisation_id == org_id,
                HitlClaim.account_id.is_not(None),
                HitlClaim.decision.is_(None),
                HitlClaim.claimed_at.is_not(None),
                HitlClaim.claimed_at < now - threshold,
            )
            .select_from(HitlClaim)
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
        decision_payload: dict[str, Any] | None = None,
    ) -> HitlClaim:
        """Record a decision on the claim row — the STAMP authority (FAR-541).

        Decision-payload contract (FAR-541 iteration 3): the persisted payload
        shape is ``{"action": <verdict>, "gate_id": <this row's gate id>}``
        plus any per-action members (``output``/``modified_output``/``reason``/
        ``notes``). ``_decide`` is the single stamp authority: a payload
        WITHOUT a ``gate_id`` is stamped with this row's gate id here; a
        payload already stamped for a DIFFERENT gate raises
        ``DecisionPayloadError`` (a foreign-stamped decision is never
        persisted). Call-site stamps (API routes / MCP) remain — they feed the
        DIRECT ``executor.resume`` injection which bypasses ``_decide``.

        Recognized actions are enforced at RESUME time by each consumer, not
        here:

        * HITL gate nodes: ``approved`` / ``rejected`` / ``deliver_manual``
        * manual nodes: the node's own id as ``gate_id`` + ``output``
        * conformance overrides: ``approved`` / ``deliver_manual`` / ``skip`` /
          ``replay`` (the latter two are stamped by the recover-node route)

        Raises on missing token, expired token, decided gate, or a
        malformed/foreign-stamped payload.
        """
        now = datetime.now(UTC)

        # FAR-541 (iteration 3): stamp authority. A payload without a stamp is
        # stamped with THIS row's gate id; a foreign stamp is refused before
        # any DB write. Copied (not mutated) so the caller's dict — reused for
        # the direct executor.resume injection — is untouched. A non-dict
        # payload skips stamping here and is rejected by the validator below.
        if decision_payload is None:
            decision_payload = {"action": decision, "gate_id": gate_id}
        elif isinstance(decision_payload, dict) and decision_payload.get("gate_id") is None:
            decision_payload = {**decision_payload, "gate_id": gate_id}

        # Validate the resume payload shape at write (B1): it must be a dict
        # and any output/modified_output members must be dicts. Oversized
        # payloads are refused with a clear 422 so the human's verdict is never
        # silently truncated or auto-approved as an empty dict on recovery.
        self._validate_decision_payload(decision_payload, gate_id=gate_id)

        # Validate JWT signature and scope before attempting the SQL UPDATE.
        # Expiry is checked separately via the SQL WHERE clause (expires_at > now)
        # so that the DB remains the authoritative source of truth for TTL.
        if self._secret_key and self._looks_like_jwt(claim_token):
            try:
                _decode_claim_jwt(claim_token, self._secret_key, run_id=str(run_id), gate_id=gate_id)
            except ExpiredSignatureError as err:
                raise ClaimTokenExpiredError from err
            except JWTError as err:
                raise ClaimTokenInvalidError from err

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
                decision_payload=decision_payload,
                account_id=None,
                claim_token=None,
                expires_at=now,
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
                raise ClaimTokenExpiredError
            if existing.claim_token != claim_token:
                raise ClaimTokenInvalidError
            raise ClaimTokenExpiredError
        gate = await session.get(HitlClaim, claim_id, populate_existing=True)
        if gate is None:
            raise GateVanishedError(run_id, gate_id, "decided")
        return gate

    @staticmethod
    def _looks_like_jwt(token: str) -> bool:
        """Rough heuristic: a JWT has exactly 2 dots (3 base64 segments)."""
        return token.count(".") == 2

    @staticmethod
    def _validate_decision_payload(payload: dict[str, Any] | None, *, gate_id: str | None = None) -> None:
        """Validate the resume payload shape at write (B1).

        Rules:
        - ``None`` is allowed (legacy/payload-less decisions).
        - A non-dict payload is rejected (a gate must never be recovered with
          a corrupted decision).
        - FAR-541 (iteration 3): when *gate_id* is given and the payload is
          stamped for a DIFFERENT gate, it is rejected — a foreign-stamped
          decision must never be persisted (``_decide`` stamps missing stamps
          itself; only a MISMATCH reaches this check).
        - ``output`` / ``modified_output`` members must be dicts.
        - The serialised payload must not exceed ``_DECISION_PAYLOAD_MAX_BYTES``.

        Raises ``DecisionPayloadError`` on any violation.
        """
        if payload is None:
            return
        if not isinstance(payload, dict):
            raise DecisionPayloadError("decision_payload must be a JSON object")
        if gate_id is not None:
            stamped = payload.get("gate_id")
            if stamped is not None and str(stamped) != str(gate_id):
                raise DecisionPayloadError(
                    f"decision_payload is stamped for gate {stamped!r}, not the target gate {gate_id!r}"
                )
        for key in ("output", "modified_output"):
            value = payload.get(key)
            if value is not None and not isinstance(value, dict):
                raise DecisionPayloadError(f"decision_payload.{key} must be a JSON object")
        try:
            size = len(json.dumps(payload, default=str).encode("utf-8"))
        except (TypeError, ValueError) as err:
            raise DecisionPayloadError("decision_payload must be JSON-serialisable") from err
        if size > _DECISION_PAYLOAD_MAX_BYTES:
            raise DecisionPayloadError(f"decision_payload exceeds the {_DECISION_PAYLOAD_MAX_BYTES} byte limit")

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
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to log audit event for claim %s", gate.id)
            raise
