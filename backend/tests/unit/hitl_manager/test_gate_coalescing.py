"""Unit tests for modulo.core.hitl_manager.gate_coalescing (FAR-604 D4).

Mock/fake based — no Postgres. Covers the gate-raise coalescing decision:

* runs without a coalesce key (non-webhook triggers) always raise;
* no open gate for the same (pipeline, gate id, entity key) → raise;
* unchanged SHA → reuse: the duplicate run is skipped (no supersede UPDATE);
* changed SHA → supersede: the old gate is system-closed ``rejected``, the
  parked old run un-parks, and the new run raises fresh;
* a claimer winning the race before the close-out skips the supersede;
* qa F2: Postgres takes a transaction-scoped advisory lock (per work item)
  before the scan; non-Postgres backends skip it;
* qa F3: the candidate scan only considers gates of LIVE runs
  (awaiting_human/hitl_parked) — a terminal run's orphaned gate is never a
  reuse candidate;
* qa F12(a): the candidate scan binds the org + pipeline explicitly (two-org
  isolation: an org-A open gate is invisible to an org-B scan).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.hitl_manager.gate_coalescing import (
    _coalesce_lock_key,
    evaluate_gate_coalescing,
)
from modulo.db.models.hitl_claim import HitlClaim

ORG_ID = uuid.UUID("18348064-eca3-4aa7-be96-8f6c9123efd0")
OTHER_ORG_ID = uuid.UUID("2b6d9f0a-1c3e-4d5b-8a7f-9e0d1c2b3a4c")
PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000006666")
RUN_ID = uuid.UUID("fb4b1368-68ca-4125-8091-ca8d7c25839e")
OLD_RUN_ID = uuid.UUID("0b0e2f60-1f47-4bda-9aeb-6f6fdd807d3c")
GATE = "hitl_gate_review_publish"
KEY = "github:farnalabs/modulo:pr:42"
HASH_A = "a" * 64
HASH_B = "b" * 64
_SUPERSEDE_REASON_MARKER = "superseded_by_newer_payload"


def _payload(*, key: str | None = KEY) -> dict[str, Any]:
    payload: dict[str, Any] = {"input": "value"}
    if key is not None:
        payload["_coalesce_key"] = key
    return payload


def _open_claim() -> MagicMock:
    claim = MagicMock(spec=HitlClaim)
    claim.id = uuid.uuid4()
    claim.run_id = OLD_RUN_ID
    claim.gate_id = GATE
    claim.pipeline_id = PIPELINE_ID
    claim.organisation_id = ORG_ID
    claim.decision = None
    claim.account_id = None
    claim.created_at = datetime.now(UTC)
    return claim


class _CoalesceSession:
    """Session double dispatching on the evaluate_gate_coalescing call order.

    1. run row SELECT            → .first() → run_row
    2. advisory lock (pg ONLY)   → dummy result
    3. candidate gates SELECT    → .all()   → candidates
    4. supersede UPDATE (opt.)   → .rowcount
    5. un-park UPDATE (opt.)     → .rowcount

    ``dialect`` selects the advisory-lock branch (Postgres only, qa F2).
    """

    def __init__(
        self,
        *,
        run_row: tuple[Any, ...] | None,
        candidates: list[tuple[MagicMock, str, dict[str, Any] | None]],
        supersede_rowcount: int = 1,
        dialect: str = "sqlite",
    ) -> None:
        self._run_row = run_row
        self._candidates = candidates
        self._supersede_rowcount = supersede_rowcount
        self._dialect = dialect
        self.statements: list[Any] = []
        self.params_seen: list[dict[str, Any]] = []

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name=self._dialect))

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.statements.append(stmt)
        self.params_seen.append(dict(params or {}))
        index = len(self.statements)
        result = MagicMock()
        if index == 1:
            result.first.return_value = self._run_row
            return result
        candidates_index = 2
        if self._dialect == "postgresql":
            if index == 2:
                return result  # the advisory lock
            candidates_index = 3
        if index == candidates_index:
            result.all.return_value = list(self._candidates)
            return result
        result.rowcount = self._supersede_rowcount
        return result


def _run_row(payload: dict[str, Any] | None, input_hash: str) -> tuple[dict[str, Any] | None, str]:
    return (payload, input_hash)


def _bound_values(stmt: Any) -> set[str]:
    return {str(v) for v in stmt.compile().params.values()}


class TestEvaluateGateCoalescing:
    async def test_missing_run_row_raises(self) -> None:
        session = _CoalesceSession(run_row=None, candidates=[])
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        assert len(session.statements) == 1

    async def test_no_coalesce_key_always_raises(self) -> None:
        session = _CoalesceSession(run_row=_run_row(_payload(key=None), HASH_A), candidates=[])
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        # No candidate scan at all — non-webhook runs are never coalesced.
        assert len(session.statements) == 1

    async def test_no_open_gate_raises(self) -> None:
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[])
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        assert len(session.statements) == 2

    async def test_same_entity_key_required(self) -> None:
        other_key = "github:farnalabs/modulo:pr:43"
        session = _CoalesceSession(
            run_row=_run_row(_payload(), HASH_A),
            candidates=[(_open_claim(), HASH_A, _payload(key=other_key))],
        )
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        assert len(session.statements) == 2

    @patch("modulo.core.hitl_manager.gate_coalescing.append_audit_event", new_callable=AsyncMock)
    async def test_same_sha_reuses_open_gate_no_second_gate(self, audit: AsyncMock) -> None:
        """Same-key same-SHA re-dispatch → no second gate: the existing gate
        decides, the duplicate run is skipped (executor terminalises it)."""
        session = _CoalesceSession(
            run_row=_run_row(_payload(), HASH_A),
            candidates=[(_open_claim(), HASH_A, _payload())],
        )
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "reuse"
        # Pure decision + audit — no gate-closing / un-park writes issued.
        assert len(session.statements) == 2
        audit.assert_awaited_once()

    @patch("modulo.core.hitl_manager.gate_coalescing.append_audit_event", new_callable=AsyncMock)
    async def test_changed_sha_supersedes_old_gate_and_raises_fresh(self, audit: AsyncMock) -> None:
        """Changed SHA → old gate auto-rejected + parked old run un-parked,
        new run raises fresh."""
        session = _CoalesceSession(
            run_row=_run_row(_payload(), HASH_B),
            candidates=[(_open_claim(), HASH_A, _payload())],
        )
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        assert len(session.statements) == 4
        # Statement 3 = the old gate's system-committed rejection (stamped
        # with the gate id + supersede reason, per the FAR-541 shape).
        close_values = _bound_values(session.statements[2])
        assert "rejected" in close_values
        assert any(GATE in v for v in close_values)
        assert any(_SUPERSEDE_REASON_MARKER in v for v in close_values)
        # Statement 4 = the parked old run un-parks (hitl_parked → awaiting_human).
        unpark_values = _bound_values(session.statements[3])
        assert "hitl_parked" in unpark_values
        assert "awaiting_human" in unpark_values
        assert str(OLD_RUN_ID) in unpark_values
        audit.assert_awaited_once()

    @patch("modulo.core.hitl_manager.gate_coalescing.append_audit_event", new_callable=AsyncMock)
    async def test_supersede_race_with_claimer_skips(self, audit: AsyncMock) -> None:
        """A claimer taking the old gate between scan and close-out skips the
        supersede (no decide, no un-park) and the new run still raises."""
        session = _CoalesceSession(
            run_row=_run_row(_payload(), HASH_B),
            candidates=[(_open_claim(), HASH_A, _payload())],
            supersede_rowcount=0,
        )
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        # Only the guarded close-out attempt ran; the un-park never fired.
        assert len(session.statements) == 3
        audit.assert_not_awaited()

    @patch("modulo.core.hitl_manager.gate_coalescing.append_audit_event", new_callable=AsyncMock)
    async def test_candidate_scan_is_open_gate_scoped(self, audit: AsyncMock) -> None:
        """The candidate query filters on pipeline + gate id + open state."""
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[])
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        sql = str(session.statements[1])
        assert "hitl_claims" in sql
        assert "runs" in sql
        assert "decision IS NULL" in sql
        assert "account_id IS NULL" in sql


class TestCandidateScanIsolation:
    """qa F3 + F12(a): the candidate scan only considers LIVE runs' open
    gates and binds the org + pipeline explicitly (two-org isolation)."""

    async def test_candidates_are_live_run_gates_only(self) -> None:
        """qa F3: the scan's WHERE carries the live-status filter — a
        terminal run's orphaned open gate is never a reuse candidate."""
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[])
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        sql = str(session.statements[1])
        assert "status IN" in sql
        # The status literals ride as a bound (POSTCOMPILE) list — assert on
        # the compiled params, not the rendered SQL text.
        compiled_params: set[str] = set()
        for value in session.statements[1].compile().params.values():
            if isinstance(value, (list, tuple, set, frozenset)):
                compiled_params.update(str(item) for item in value)
            else:
                compiled_params.add(str(value))
        assert "awaiting_human" in compiled_params
        assert "hitl_parked" in compiled_params
        assert "failed" not in compiled_params

    async def test_candidate_scan_binds_org_and_pipeline(self) -> None:
        """qa F12(a): the compiled candidate statement binds the caller's
        org + pipeline — the scan can never widen past them (RLS is defence
        in depth, not the only guard)."""
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[])
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        params = session.statements[1].compile().params
        param_values = {str(v) for v in params.values()}
        assert str(ORG_ID) in param_values
        assert str(PIPELINE_ID) in param_values
        assert str(RUN_ID) in param_values  # the != run_id exclusion

    @patch("modulo.core.hitl_manager.gate_coalescing.append_audit_event", new_callable=AsyncMock)
    async def test_two_org_scans_never_cross(self, audit: AsyncMock) -> None:
        """qa F12(a): org A's open gate is INVISIBLE to an org-B scan — the
        org-B call binds org-B's id, finds no candidates, and the outcome is
        ``raise`` (the new run raises its own gate)."""
        session_a = _CoalesceSession(
            run_row=_run_row(_payload(), HASH_A),
            candidates=[(_open_claim(), HASH_A, _payload())],
        )
        outcome_a = await evaluate_gate_coalescing(
            session_a, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome_a == "reuse"

        session_b = _CoalesceSession(run_row=_run_row(_payload(), HASH_B), candidates=[])
        outcome_b = await evaluate_gate_coalescing(
            session_b, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=OTHER_ORG_ID
        )
        assert outcome_b == "raise"
        params_b = session_b.statements[1].compile().params
        param_values_b = {str(v) for v in params_b.values()}
        assert str(OTHER_ORG_ID) in param_values_b
        assert str(ORG_ID) not in param_values_b


class TestAdvisoryLockSerialisation:
    """qa F2: concurrent duplicate deliveries serialise on a transaction-
    scoped advisory lock BEFORE any of them scans."""

    async def test_postgres_takes_lock_before_scan(self) -> None:
        session = _CoalesceSession(
            run_row=_run_row(_payload(), HASH_A),
            candidates=[],
            dialect="postgresql",
        )
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        lock_stmt = str(session.statements[1])
        assert "pg_advisory_xact_lock" in lock_stmt
        assert "hashtext" in lock_stmt
        # The lock key names the work item (org:pipeline:gate:entity key).
        assert session.params_seen[1] == {"key": _coalesce_lock_key(ORG_ID, PIPELINE_ID, GATE, KEY)}
        # The lock is issued BEFORE the candidate scan.
        assert "hitl_claims" not in lock_stmt

    async def test_non_postgres_skips_the_lock(self) -> None:
        """Advisory locks are a Postgres feature — other backends skip the
        lock statement entirely (SQLite is single-writer anyway)."""
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[])
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        assert all("pg_advisory_xact_lock" not in str(s) for s in session.statements)

    async def test_no_coalesce_key_never_takes_the_lock(self) -> None:
        """The lock is keyed on the entity key — a key-less run returns
        before any locking statement is issued."""
        session = _CoalesceSession(run_row=_run_row(_payload(key=None), HASH_A), candidates=[], dialect="postgresql")
        outcome = await evaluate_gate_coalescing(
            session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID
        )
        assert outcome == "raise"
        assert all("pg_advisory_xact_lock" not in str(s) for s in session.statements)


class TestServerSideKeyFilter:
    """qa F9: the work-item key match is pushed server-side on Postgres —
    the bounded candidate scan stops hauling candidates' full input_payload
    JSONB just to compare one field."""

    async def test_postgres_filters_key_server_side_and_skips_payload_haul(self) -> None:
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[], dialect="postgresql")
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        sql = str(session.statements[2])
        assert "jsonb_extract_path_text" in sql
        # input_payload appears ONLY in the server-side filter — the SELECT
        # hauls (claim, input_hash) pairs, never payloads.
        assert sql.count("input_payload") == 1

    async def test_non_postgres_hauls_payloads_for_client_side_match(self) -> None:
        """Non-Postgres backends have no JSON path filter: payloads stay in
        the SELECT and the key match happens client-side."""
        session = _CoalesceSession(run_row=_run_row(_payload(), HASH_A), candidates=[])
        await evaluate_gate_coalescing(session, run_id=RUN_ID, gate_id=GATE, pipeline_id=PIPELINE_ID, org_id=ORG_ID)
        sql = str(session.statements[1])
        assert "jsonb_extract_path_text" not in sql
        assert "input_payload" in sql
