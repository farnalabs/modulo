"""Unit tests for FAR-967 chunk 10: policy-gate pin fingerprint + operator control.

Covers the acceptance criteria that do not need a live Postgres:

* §3.3 fingerprint determinism (C2, C7, C7a)
* §3.4 run-start verification three-case table (C4 shape, C5, C6, C15)
* §5 operator-control disabled-gate filtering (C8, C9, C10)
* §3.1/3.2 pin construction + snapshot persistence wiring (C1, C3, C14)
* schema-contract assertions (C11, C12, model-DDL level)

Migration round-trip + live-DB CHECK behaviour (C13) live in
``backend/tests/integration/test_policy_gate_pin_migration.py``.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from modulo.core.eval_engine.policy_gate import fingerprint_policy_gate_pins
from modulo.db.crud.pipeline_snapshot import (
    _build_policy_gate_pins,
    _load_policy_gate_rows_for_pipeline,
)
from modulo.db.crud.run import (
    _filter_disabled_policy_gates,
    _stamp_guardrail_blocked_run,
    _verify_policy_gate_pin_fingerprint,
)
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.policy_gate import PolicyGate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _row_result(value: object) -> MagicMock:
    """A MagicMock whose ``one_or_none()`` and ``all()`` yield ``value``."""
    result = MagicMock()
    result.one_or_none.return_value = value
    result.all.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    scalars = MagicMock()
    scalars.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _gate_pin(action: str = "block") -> dict[str, str]:
    return {
        "policy_gate_id": str(uuid.uuid4()),
        "eval_id": str(uuid.uuid4()),
        "action": action,
        "node_id": str(uuid.uuid4()),
    }


def _pins(count: int = 2) -> list[dict[str, str]]:
    return [_gate_pin() for _ in range(count)]


def _interception_request(
    *,
    is_replay: bool = True,
    snapshot_id: uuid.UUID | None = None,
) -> Any:
    from modulo.db.crud.run import _InterceptionRequest

    return _InterceptionRequest(
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        payload={"input": "x"},
        is_replay=is_replay,
        snapshot_id=snapshot_id if snapshot_id is not None else uuid.uuid4(),
        guardrails_kill_switch=False,
    )


def _policy_gate_row(
    gate_id: uuid.UUID,
    *,
    node_id: uuid.UUID | None = None,
    action: str = "block",
) -> MagicMock:
    row = MagicMock()
    row.id = gate_id
    row.eval_id = uuid.uuid4()
    row.node_id = node_id if node_id is not None else uuid.uuid4()
    row.action = action
    return row


def _stubbed_guardrail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the guardrail pass so the policy-gate branch is the only
    live logic under test in ``_intercept_guardrails`` runs."""
    import modulo.db.crud.guardrail_config as guardrail_config

    monkeypatch.setattr(
        guardrail_config,
        "load_pipeline_guardrail_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "modulo.db.crud.run._resolve_pinned_guardrail_state",
        AsyncMock(
            return_value=MagicMock(
                pinned_defs=[],
                skipped_guardrails=[],
                blocked=False,
                block_message="",
            )
        ),
    )


# ---------------------------------------------------------------------------
# §3.3 — fingerprint_policy_gate_pins determinism (C2, C7, C7a)
# ---------------------------------------------------------------------------


def test_fingerprint_none_pins_returns_none() -> None:
    """``None`` (absent pins — legacy snapshot) fingerprints to ``None``."""
    assert fingerprint_policy_gate_pins(None) is None


def test_fingerprint_empty_pins_produce_deterministic_digest() -> None:
    """An EMPTY pin list is a deliberate state: it must produce a stable
    64-hex digest — never ``None``, never a per-call arbitrary value."""
    digest = fingerprint_policy_gate_pins([])
    assert digest is not None
    assert fingerprint_policy_gate_pins([]) == digest
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_fingerprint_empty_differs_from_absent() -> None:
    """C7: empty ≠ absent. The ``[]`` digest must never collide with the
    ``None`` sentinel of a legacy absent pin set."""
    assert fingerprint_policy_gate_pins([]) != fingerprint_policy_gate_pins(None)


def test_fingerprint_is_order_and_key_order_insensitive() -> None:
    """Re-ordering entries or reserialising dict keys must NOT change the
    digest — the canonical form is sorted, so iteration or persistence order
    is irrelevant."""
    pin_a = _gate_pin()
    pin_b = _gate_pin()
    base = fingerprint_policy_gate_pins([pin_a, pin_b])
    assert fingerprint_policy_gate_pins([pin_b, pin_a]) == base
    reordered_keys = [dict(reversed(list(pin.items()))) for pin in (pin_b, pin_a)]
    assert fingerprint_policy_gate_pins(reordered_keys) == base


def test_fingerprint_changes_when_pin_content_changes() -> None:
    """Any content change (an added, removed, or edited gate) must flip the
    digest — otherwise the run-start verification detects nothing."""
    pins = _pins(2)
    base = fingerprint_policy_gate_pins(pins)
    assert fingerprint_policy_gate_pins(pins[:1]) != base
    assert fingerprint_policy_gate_pins([*pins, _gate_pin()]) != base
    drifted = [dict(pins[0], action="warn"), pins[1]]
    assert fingerprint_policy_gate_pins(drifted) != base


# ---------------------------------------------------------------------------
# §3.4 — _verify_policy_gate_pin_fingerprint three-case table
# (C4 shape, C5, C6, C7a, C15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_case_i_no_pins_no_fingerprint_falls_back_to_live() -> None:
    """C6: a legacy snapshot with NO pins + NO fingerprint does not block —
    the replay proceeds against the pipeline's CURRENT live gates."""
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=None,
        saved_fingerprint=None,
    )
    assert not blocked
    assert message == ""


@pytest.mark.asyncio
async def test_verify_case_ii_pins_without_fingerprint_proceed_unverified() -> None:
    """C7a: pins present but NO fingerprint (the middle legacy state) — the
    run must proceed WITHOUT verification. Verifying anyway would spuriously
    block every snapshot created before the fingerprint migration."""
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=_pins(),
        saved_fingerprint=None,
    )
    assert not blocked
    assert message == ""


@pytest.mark.asyncio
async def test_verify_matching_fingerprint_trusts_the_pins() -> None:
    """C5: pins + matching fingerprint → verified and trusted, no block."""
    pins = _pins(3)
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=pins,
        saved_fingerprint=fingerprint_policy_gate_pins(pins),
    )
    assert not blocked
    assert message == ""


def _assert_mismatch_message(
    block_message: str,
    snapshot_id: uuid.UUID,
    stored: str,
    recomputed: str,
) -> None:
    assert "fingerprint mismatch" in block_message
    assert f"snapshot {snapshot_id}" in block_message, block_message
    assert stored[:12] in block_message
    assert recomputed[:12] in block_message
    assert stored not in block_message, "user-facing message must carry TRUNCATED digests only"
    assert recomputed not in block_message, "user-facing message must carry TRUNCATED digests only"


@pytest.mark.asyncio
async def test_verify_added_gate_fails_closed_with_truncated_actionable_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C4: a gate added to the pins after creation digests differently →
    fail CLOSED. The message names the snapshot, the truncated stored +
    recomputed digests, and the remediation; the LOG extras carry the FULL
    digests."""
    import logging

    pins = _pins(2)
    tampered = [*pins, _gate_pin()]
    stored = fingerprint_policy_gate_pins(pins)
    recomputed = fingerprint_policy_gate_pins(tampered)
    snapshot_id = uuid.uuid4()

    with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
        blocked, message = await _verify_policy_gate_pin_fingerprint(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            snapshot_id=snapshot_id,
            snap_pins=tampered,
            saved_fingerprint=stored,
        )

    assert blocked
    _assert_mismatch_message(message, snapshot_id, stored, recomputed)
    assert "create a new run" in message
    mismatch_records = [r for r in caplog.records if r.getMessage() == "policy_gates.pin_fingerprint_mismatch"]
    assert mismatch_records
    record = mismatch_records[-1]
    assert record.__dict__["stored_fingerprint"] == stored
    assert record.__dict__["recomputed_fingerprint"] == recomputed
    assert record.__dict__["snapshot_id"] == str(snapshot_id)


@pytest.mark.asyncio
async def test_verify_removed_gate_fails_closed() -> None:
    """A REMOVED pin is also a digest breaker — the set is immutable after
    creation, so the mismatch must fail closed in that direction too."""
    pins = _pins(3)
    snapshot_id = uuid.uuid4()
    stored = fingerprint_policy_gate_pins(pins)
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=snapshot_id,
        snap_pins=pins[:2],
        saved_fingerprint=stored,
    )
    assert blocked
    assert "fingerprint mismatch" in message
    assert f"snapshot {snapshot_id}" in message


@pytest.mark.asyncio
async def test_verify_mismatch_message_directs_at_a_new_run_only() -> None:
    """C15: the mismatch is NON-RETRYABLE — the remediation is creating a
    NEW run; retrying the same snapshot cannot reconcile pins and digest."""
    pins = _pins(2)
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=[*pins, _gate_pin()],
        saved_fingerprint=fingerprint_policy_gate_pins(pins),
    )
    assert blocked
    assert message.endswith("create a new run)")


def test_stamp_guardrail_blocked_run_marks_terminal_eval_failed() -> None:
    """C15 run-side: an interception-blocked run — including the policy-gate
    fingerprint block — is stamped TERMINAL (eval_failed), never dispatched
    to the executor, never auto-retried."""
    run = MagicMock()
    message = (
        "policy gate mechanism error: snapshot policy-gate pin fingerprint mismatch (remediation: create a new run)"
    )
    _stamp_guardrail_blocked_run(run, message)
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert run.error_detail == message
    assert run.completed_at is not None


# ---------------------------------------------------------------------------
# §5 — operator control: disabled-gate filtering (C8/C9/C10 function level)
# ---------------------------------------------------------------------------


def test_filter_excludes_disabled_live_gates() -> None:
    """C9: a DISABLED live row overrides the pin — the gate is excluded from
    the evaluated set even though the pin still names it. The pin set itself
    is never mutated (immutable membership)."""
    disabled_pin = _gate_pin()
    enabled_pin = _gate_pin()
    pins = [disabled_pin, enabled_pin]
    enabled_map = {enabled_pin["policy_gate_id"]: True, disabled_pin["policy_gate_id"]: False}
    filtered = _filter_disabled_policy_gates(pins, enabled_map)
    assert filtered == [enabled_pin]
    assert pins == [disabled_pin, enabled_pin]


def test_filter_treats_soft_deleted_gate_as_disabled() -> None:
    """A gate soft-deleted since snapshot creation is absent from the live
    map → treated as DISABLED (excluded), never silently evaluated."""
    gone_pin = _gate_pin()
    live_pin = _gate_pin()
    filtered = _filter_disabled_policy_gates(
        [gone_pin, live_pin],
        {live_pin["policy_gate_id"]: True},
    )
    assert filtered == [live_pin]


def test_filter_never_adds_gates() -> None:
    """C8 symmetry: extra ENABLED rows in the live map must not smuggle new
    gates INTO the pinned evaluation set — the filter can only REMOVE."""
    pins = _pins(1)
    filtered = _filter_disabled_policy_gates(
        pins,
        {pins[0]["policy_gate_id"]: True, "ghost-gate": True, "ghost-2": True},
    )
    assert filtered == pins


def test_filter_all_disabled_and_empty_inputs_yield_empty() -> None:
    pins = _pins(2)
    all_disabled = {p["policy_gate_id"]: False for p in pins}
    assert not _filter_disabled_policy_gates(pins, all_disabled)
    assert not _filter_disabled_policy_gates([], {})
    assert not _filter_disabled_policy_gates(None, {})


def test_filter_treats_unresolvable_pin_as_disabled() -> None:
    """A pin whose live row cannot be resolved is treated as DISABLED — the
    operator layer fails closed on missing data."""
    pin = _gate_pin()
    assert not _filter_disabled_policy_gates([pin], {})


# ---------------------------------------------------------------------------
# C8 / C4 call-site wiring — _intercept_guardrails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intercept_runs_operator_control_on_pinned_replay(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C8 load-bearing: on a pinned replay the run-start path loads the live
    enabled map and applies the disabled filter. Remove the filter (or its
    call site) and the ``operator_control_filtered`` audit event vanishes —
    this test fails."""
    import logging

    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request()
    p1, p2 = _pins(2)
    disabled = _gate_pin()
    pins = [p1, p2, disabled]
    second_gate = _gate_pin()
    enabled_map = [
        (p1["policy_gate_id"], True),
        (p2["policy_gate_id"], True),
        (disabled["policy_gate_id"], False),
        (second_gate["policy_gate_id"], True),
    ]

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((pins, None)), _row_result(enabled_map)])

    with caplog.at_level(logging.INFO, logger="modulo.db.crud.run"):
        interception = await _intercept_guardrails(session, request)

    assert not interception.blocked
    assert session.execute.await_count == 2
    filtered_records = [r for r in caplog.records if r.getMessage() == "policy_gates.operator_control_filtered"]
    assert filtered_records
    extras = filtered_records[-1].__dict__
    assert extras["before"] == 3
    assert extras["after"] == 2
    assert extras["run_id"] == str(request.run_id)
    assert extras["org_id"] == str(request.org_id)


@pytest.mark.asyncio
async def test_intercept_non_replay_never_touches_the_policy_gate_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NON-replay run carries no snapshot → the policy-gate seam must not
    read or emit anything: zero session queries, no block."""
    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request(is_replay=False, snapshot_id=None)
    session = AsyncMock(spec=AsyncSession)

    interception = await _intercept_guardrails(session, request)

    assert not interception.blocked
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_intercept_fingerprint_block_short_circuits_operator_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4 wiring: a fingerprint mismatch blocks IMMEDIATELY at run start —
    the operator control (the live enabled-map query) must not even run."""
    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request()
    pins = _pins(2)
    wrong_fp = fingerprint_policy_gate_pins(_pins(5))

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((pins, wrong_fp))])

    interception = await _intercept_guardrails(session, request)

    assert interception.blocked
    assert "fingerprint mismatch" in interception.block_message
    assert session.execute.await_count == 1, "enabled-map query must not run past a fingerprint block"


# ---------------------------------------------------------------------------
# §3.1/3.2 — (C1, C3, C14) pin construction + snapshot persistence wiring
# ---------------------------------------------------------------------------


def test_build_policy_gate_pins_serialises_entry_fields() -> None:
    """C1: each pin entry carries policy_gate_id, eval_id, action, node_id —
    enough identity to reconstruct the gate's evaluation context and detect
    corruption of the action field."""
    gate_id = uuid.uuid4()
    node_id = uuid.uuid4()
    row = _policy_gate_row(gate_id, node_id=node_id, action="block")

    assert _build_policy_gate_pins([row]) == [
        {
            "policy_gate_id": str(gate_id),
            "eval_id": str(row.eval_id),
            "action": "block",
            "node_id": str(node_id),
        }
    ]


def test_build_policy_gate_pins_none_when_no_rows() -> None:
    """Zero live gates → pins are ABSENT (``None``), preserving the legacy
    fallback semantics — this is what keeps the C6 fallback possible."""
    assert _build_policy_gate_pins([]) is None


@pytest.mark.asyncio
async def test_creation_loader_compiled_sql_excludes_disabled_and_deleted() -> None:
    """C14 load-bearing: the creation-time loader is the ONLY place gates
    are selected for pinning — its SQL must exclude disabled gates
    (``enabled IS true``) and soft-deleted gates (``deleted_at IS NULL``).
    Remove either predicate from the source and this fails."""
    from sqlalchemy.dialects import postgresql

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_scalars_result([]))

    await _load_policy_gate_rows_for_pipeline(
        session,
        pipeline_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
    )
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "policy_gates.enabled IS true" in compiled
    assert "policy_gates.deleted_at IS NULL" in compiled
    assert "evals.pipeline_id" in compiled
    assert "policy_gates.organisation_id" in compiled


def _creation_session(
    pipeline: MagicMock,
    edge: MagicMock,
    gate_rows: list[MagicMock],
) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    session.execute.side_effect = [
        lock_result,
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalar_result(1),
        _scalars_result([]),  # guardrail rows
        _scalars_result(gate_rows),  # policy gate rows
        MagicMock(),  # unlock
    ]
    return session


def _simple_pipeline_and_edge() -> tuple[MagicMock, MagicMock]:
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.organisation_id = uuid.uuid4()
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}
    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None
    return pipeline, edge


@pytest.mark.asyncio
async def test_creation_writes_policy_gate_pins_and_fingerprint_to_snapshot() -> None:
    """C1 + C3 end-to-end through ``create_snapshot_from_live_graph``: gate
    rows are serialised into ``policy_gate_pins_json`` and the snapshot's
    ``policy_gate_pins_fingerprint`` matches ``fingerprint_policy_gate_pins``
    of the STORED pins."""
    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

    pipeline, edge = _simple_pipeline_and_edge()
    gate_rows = [_policy_gate_row(uuid.uuid4()), _policy_gate_row(uuid.uuid4())]
    expected_pins = [
        {
            "policy_gate_id": str(row.id),
            "eval_id": str(row.eval_id),
            "action": row.action,
            "node_id": str(row.node_id),
        }
        for row in gate_rows
    ]

    snapshot = await create_snapshot_from_live_graph(
        _creation_session(pipeline, edge, gate_rows),
        pipeline_id=pipeline.id,
    )

    assert snapshot is not None
    assert snapshot.policy_gate_pins_json == expected_pins
    assert snapshot.policy_gate_pins_fingerprint == fingerprint_policy_gate_pins(expected_pins)
    assert len(snapshot.policy_gate_pins_fingerprint) == 64


@pytest.mark.asyncio
async def test_creation_zero_gates_leave_pins_absent_not_empty() -> None:
    """C7 creation-side: a pipeline with no live gates snapshots with pins
    ``None`` AND fingerprint ``None`` — absent (legacy fallback), NOT the
    empty-set state (empty ≠ absent, §3.3)."""
    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

    pipeline, edge = _simple_pipeline_and_edge()
    snapshot = await create_snapshot_from_live_graph(
        _creation_session(pipeline, edge, []),
        pipeline_id=pipeline.id,
    )

    assert snapshot is not None
    assert snapshot.policy_gate_pins_json is None
    assert snapshot.policy_gate_pins_fingerprint is None


# ---------------------------------------------------------------------------
# (C11, C12) schema contract — via sqlalchemy.inspect
# ---------------------------------------------------------------------------


def _sqlite_engine_for(tables: list[type]) -> Any:
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    for model in tables:
        model.__table__.create(engine, checkfirst=True)
    return engine


def test_schema_policy_gate_enabled_columns_contract() -> None:
    """C11: ``enabled`` is BOOLEAN NOT NULL DEFAULT true; the audit pair
    ``enabled_at``/``disabled_at`` exists and is nullable."""
    engine = _sqlite_engine_for([PolicyGate])
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("policy_gates")}
        enabled = columns["enabled"]
        assert not enabled["nullable"]
        assert "BOOLEAN" in str(enabled["type"]).upper()
        assert enabled["default"] is not None
        assert "true" in str(enabled["default"]).lower()
        for nullable_audit_column in ("enabled_at", "disabled_at"):
            assert nullable_audit_column in columns
            assert columns[nullable_audit_column]["nullable"]
    finally:
        engine.dispose()


def test_schema_policy_gate_enabled_timestamps_check_on_model() -> None:
    """C11 parity: the symmetric CHECK ships on the MODEL, not only in
    migration 0270 — ``metadata.create_all()`` / SQLite-mirror CTAS must
    match the shipped post-0270 contract (FAR-967 F1)."""
    from sqlalchemy import CheckConstraint

    checks = [
        c
        for c in PolicyGate.__table_args__
        if isinstance(c, CheckConstraint) and c.name == "ck_policy_gates_enabled_timestamps"
    ]
    assert len(checks) == 1, "PolicyGate model must declare ck_policy_gates_enabled_timestamps"
    predicate = str(checks[0].sqltext)
    assert "enabled AND enabled_at IS NOT NULL AND disabled_at IS NULL" in predicate
    assert "NOT enabled AND disabled_at IS NOT NULL AND enabled_at IS NULL" in predicate
    ddl = str(CreateTable(PolicyGate.__table__))
    assert "ck_policy_gates_enabled_timestamps" in ddl


def test_sqlite_create_all_enforces_enabled_timestamps_check() -> None:
    """The model-level CHECK is LIVE on a create_all-built SQLite table:
    an enabled row without enabled_at is rejected (mirror of migration
    0270), while the creation state (enabled + enabled_at set, disabled_at
    NULL) is accepted."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    engine = _sqlite_engine_for([PolicyGate])
    try:
        invalid = {
            "i": str(uuid.uuid4()),
            "o": str(uuid.uuid4()),
            "e": str(uuid.uuid4()),
            "n": str(uuid.uuid4()),
        }
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                    "action, version, enabled, enabled_at, disabled_at) "
                    "VALUES (:i, :o, :e, :n, 'warn', 1, 1, NULL, NULL)"
                ),
                invalid,
            )
        valid = {
            "i": str(uuid.uuid4()),
            "o": str(uuid.uuid4()),
            "e": str(uuid.uuid4()),
            "n": str(uuid.uuid4()),
        }
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                    "action, version, enabled, enabled_at, disabled_at) "
                    "VALUES (:i, :o, :e, :n, 'warn', 1, 1, '2026-01-01 00:00:00', NULL)"
                ),
                valid,
            )
    finally:
        engine.dispose()


def test_schema_snapshot_policy_gate_pin_columns_contract() -> None:
    """C12: ``policy_gate_pins_fingerprint`` is a nullable VARCHAR(64) and
    ``policy_gate_pins_json`` is nullable JSON — legacy snapshots (no pins,
    no fingerprint) load cleanly."""
    engine = _sqlite_engine_for([PipelineSnapshot])
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("pipeline_snapshots")}
        fingerprint = columns["policy_gate_pins_fingerprint"]
        assert fingerprint["nullable"]
        assert "VARCHAR" in str(fingerprint["type"]).upper()
        assert fingerprint["type"].length == 64
        assert columns["policy_gate_pins_json"]["nullable"]
    finally:
        engine.dispose()


def test_create_table_ddl_for_policy_gate_pins_fingerprint_is_char_64() -> None:
    """C12 belt-and-braces: the rendered DDL carries VARCHAR(64) for the
    fingerprint column — the length is never silently dropped."""
    ddl = str(CreateTable(PipelineSnapshot.__table__))
    assert "policy_gate_pins_fingerprint VARCHAR(64)" in ddl
