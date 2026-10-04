"""FAR-1408 — HITL approve validation must run inside a transaction (real Postgres).

Production defect (reproduced, do not re-investigate): ``POST
/api/v1/runs/{run_id}/hitl/{review_id}/approve`` returned **503 on every
request** with

``sqlalchemy.exc.InvalidRequestError: Autobegin is disabled on this Session;
please call session.begin() to start a new transaction``

because ``approve_review`` called ``_validate_choice_answer(...)`` at route
level — BEFORE ``_run_hitl_manager`` opened ``async with session.begin()`` —
and the REST DI session is built ``autobegin=False``
(``dependencies.get_or_create_session_factory``). ``handle_db_errors`` then
misreported that local programming error as ``503 "Database temporarily
unavailable."``

Why this is an INTEGRATION test: the existing route tests use an ``AsyncMock``
session, which happily executes outside a transaction — exactly why CI stayed
green while the endpoint 503'd in production. The session under test here is
built the way the app builds it:

    ``async_sessionmaker(bind=engine, expire_on_commit=False, autobegin=False)``

and the engine is ``app_engine`` (the ``modulo_integration_app``
NOBYPASSRLS role), so FORCE-RLS genuinely filters. That makes the SECOND half
of the fix observable too: ``set_rls_org`` must run in the SAME transaction
before the validation read, otherwise the RLS predicate filters the claim/run
rows away and validation silently FAILS OPEN (no 422) — a worse bug than the
crash.

Three tests:

1. ``test_negative_control_...`` — the pre-fix reproduction, pinned. Calling
   the route-level validation helper on a real ``autobegin=False`` session
   with NO transaction open raises ``InvalidRequestError``. Passes before AND
   after the fix by design: it proves the harness reproduces the production
   crash mechanism (the fix moves the call, it does not change the helper).
2. ``test_choice_gate_missing_answer_...`` — FAILS before the fix (the 503/500
   misclassification beats the expected 422), passes after. Because the 422
   only happens when the RLS-scoped claim row is visible, it also proves
   ``set_rls_org`` ran before the validation read.
3. ``test_approve_runs_validation_inside_the_transaction_...`` — FAILS before
   the fix (``InvalidRequestError`` → HTTPException instead of success),
   passes after: the decision commits, the validated answer rides in both the
   persisted ``decision_payload`` and the direct ``executor.resume`` payload.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.api.routes.hitl import ApproveRequest, _validate_choice_answer, approve_review
from modulo.auth.jwt import TenantPrincipal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

_REVIEW_ID = "hitl_review_a_b"
#: Fire-time gate config stamped on the claim row (the resolver's O(1) fast
#: path) — a ``kind: choice`` contract so ``require_answer=True`` can fire.
_GATE_CONFIG: dict[str, Any] = {
    "human_only": True,
    "response_contract": {
        "kind": "choice",
        "options": [{"id": "ship", "label": "Ship"}, {"id": "fix", "label": "Fix"}],
    },
}
_ANSWER = {"kind": "choice", "option_id": "ship"}


@dataclass(frozen=True)
class _SeededGate:
    """The minimal org/run/claim state the approve path resolves."""

    org_id: uuid.UUID
    account_id: uuid.UUID
    run_id: uuid.UUID
    review_id: str
    claim_token: str


def _di_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """The REST DI session shape, verbatim (``dependencies.py``)."""
    return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)


def _principal(gate: _SeededGate) -> TenantPrincipal:
    """A browser JWT principal (``human_only`` short-circuits for browsers)."""
    return TenantPrincipal(
        username="far1408-approver@test.local",
        organisation_id=gate.org_id,
        account_id=gate.account_id,
        org_role="admin",
    )


async def _seed_claimed_choice_gate(engine: AsyncEngine) -> _SeededGate:
    """Seed org + account + pipeline + snapshot + awaiting run + claimed gate.

    Mirrors the seeding in ``test_hitl_resume_roundtrip.py``; the claim row is
    seeded directly (claimed, undecided, unexpired) so the test exercises the
    APPROVE path rather than re-running the claim flow.
    """
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    claim_token = f"far1408-{uuid.uuid4().hex}"
    run_number = int(run_id.int % 10**9) + 1
    graph = {
        "nodes": [
            {"id": "a", "agent_id": str(uuid.uuid4()), "role": "agent", "prompt_template": "A"},
            {"id": "b", "agent_id": str(uuid.uuid4()), "role": "agent", "prompt_template": "B"},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "a",
                "target": "b",
                "type": "normal",
                "hitl_review_config": _GATE_CONFIG,
            }
        ],
    }
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": "Far1408", "slug": f"far1408-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": f"far1408-{org_id.hex[:8]}@test.local", "name": "FAR-1408 Approver"},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, '[]'::json, 'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": "PipeFar1408", "uid": str(account_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, CAST(:graph AS json), '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {
                "id": str(snapshot_id),
                "pid": str(pipeline_id),
                "oid": str(org_id),
                "graph": json.dumps(graph),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                "run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, :rn, 'awaiting_human')"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": uuid.uuid4().hex,
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO hitl_claims (id, organisation_id, run_id, pipeline_id, review_id, "
                "account_id, claimed_at, claim_token, expires_at, gate_config_json) "
                "VALUES (:id, :oid, :rid, :pid, :review, :aid, now(), :tok, :exp, CAST(:cfg AS json))"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "rid": str(run_id),
                "pid": str(pipeline_id),
                "review": _REVIEW_ID,
                "aid": str(account_id),
                "tok": claim_token,
                "exp": datetime.now(UTC) + timedelta(minutes=15),
                "cfg": json.dumps(_GATE_CONFIG),
            },
        )
    return _SeededGate(
        org_id=org_id,
        account_id=account_id,
        run_id=run_id,
        review_id=_REVIEW_ID,
        claim_token=claim_token,
    )


async def test_negative_control_validation_outside_a_transaction_raises_invalid_request(
    app_engine: AsyncEngine,
) -> None:
    """NEGATIVE CONTROL — the pre-fix reproduction, pinned.

    This is the exact call ``approve_review`` used to make at ROUTE level, on
    the exact session shape the REST DI builds (``autobegin=False``), with no
    transaction open: it raises ``InvalidRequestError: Autobegin is disabled``
    before any SQL reaches the database. Asserted so the harness is known to
    reproduce the production crash mechanism — the fix moves the call into the
    transaction, it does not make the helper transaction-agnostic.

    Passes both before and after the fix, by design. The fail-before /
    pass-after controls are the two tests below it.
    """
    factory = _di_session_factory(app_engine)
    async with factory() as session:
        with pytest.raises(InvalidRequestError, match="Autobegin is disabled"):
            await _validate_choice_answer(
                session,
                uuid.uuid4(),
                _REVIEW_ID,
                uuid.uuid4(),
                {"kind": "choice", "option_id": "ship"},
                require_answer=True,
            )


async def test_choice_gate_missing_answer_is_422_under_the_transaction_rls_context(
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
) -> None:
    """A choice gate with no answer must 422 — proving the RLS ordering.

    The 422 can only be produced when the RLS-scoped claim row is VISIBLE to
    the validation read: with ``set_rls_org`` missing from the transaction the
    FORCE-RLS predicate (``app_engine`` runs as the NOBYPASSRLS
    ``modulo_integration_app`` role) filters the claim and the run away, the
    config resolves to ``None``, and validation FAILS OPEN — the approval
    would proceed with no answer at all.

    Fails before the fix: ``_validate_choice_answer`` ran outside any
    transaction, so ``handle_db_errors`` answered 503 (later 500) instead of
    422.
    """
    gate = await _seed_claimed_choice_gate(db_engine)
    principal = _principal(gate)
    factory = _di_session_factory(app_engine)

    async with factory() as session:
        with pytest.raises(HTTPException) as excinfo:
            await approve_review(
                gate.run_id,
                gate.review_id,
                ApproveRequest(claim_token=gate.claim_token),
                session=session,
                engine=db_engine,
                principal=principal,
            )

    assert excinfo.value.status_code == 422, excinfo.value.detail
    assert "choice gate requires an answer" in excinfo.value.detail

    # The 422 rolled the decision transaction back — the gate is untouched.
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT decision FROM hitl_claims WHERE run_id = :rid AND review_id = :review"),
                {"rid": str(gate.run_id), "review": gate.review_id},
            )
        ).fetchone()
    assert row is not None
    assert row[0] is None


async def test_approve_runs_validation_inside_the_transaction(
    app_engine: AsyncEngine,
    db_engine: AsyncEngine,
) -> None:
    """The fixed approve path: no ``InvalidRequestError``, decision commits.

    Fails before the fix (the route-level validation raised
    ``InvalidRequestError``, which ``handle_db_errors`` translated to an HTTP
    503/500 instead of the success path), passes after: the validated answer
    reaches BOTH the persisted ``decision_payload`` and the direct
    ``executor.resume`` payload the route hands to the graph.
    """
    gate = await _seed_claimed_choice_gate(db_engine)
    principal = _principal(gate)
    factory = _di_session_factory(app_engine)

    executor = MagicMock()
    executor.resume = AsyncMock()

    with patch("modulo.api.routes.hitl._build_resume_executor", return_value=executor):
        async with factory() as session:
            result = await approve_review(
                gate.run_id,
                gate.review_id,
                ApproveRequest(claim_token=gate.claim_token, answer=dict(_ANSWER)),
                session=session,
                engine=db_engine,
                principal=principal,
            )

    assert result == {"status": "approved", "run_id": str(gate.run_id)}

    resume_kwargs = executor.resume.call_args.kwargs
    assert resume_kwargs["run_id"] == gate.run_id
    assert resume_kwargs["org_id"] == gate.org_id
    assert resume_kwargs["resume_data"]["action"] == "approved"
    assert resume_kwargs["resume_data"]["answer"] == _ANSWER

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT decision, decided_by, decision_payload FROM hitl_claims "
                    "WHERE run_id = :rid AND review_id = :review"
                ),
                {"rid": str(gate.run_id), "review": gate.review_id},
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "approved", "the decision must be committed by the in-transaction path"
    assert row[1] == gate.account_id
    payload = row[2]
    assert isinstance(payload, dict), f"decision_payload must deserialize to a dict, got {type(payload)}"
    assert payload["action"] == "approved"
    assert payload["answer"] == _ANSWER
