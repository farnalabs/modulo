"""FAR-1698: a pipeline clone must not carry the source's POLICY-GATE pin pair.

``_clone_snapshots`` / ``_snapshot_to_dict`` carried
``policy_gate_pins_json`` + ``policy_gate_pins_fingerprint`` verbatim
(FAR-967 F10). That rationale holds for the guardrail pin pair — those are
self-contained serialised definitions — but the policy-gate pins are built by
``_build_policy_gate_pins`` from the SOURCE pipeline's ``PolicyGate`` rows
(``policy_gate_id`` / ``eval_id`` / ``action`` / ``node_id`` /
``evaluation_mode``). A clone has
NO gate rows (evals and gates are never cloned; declared gate blocks are
stripped per FAR-1666), so a carried pin set references foreign gates AND
declares a "pinned universe" under which the clone's OWN gates are unpinned —
every clone gate block silently downgraded to warn.

The tests run the REAL clone CRUD functions. Two shapes are pinned:

* the clone's snapshot columns are NULL for the policy pair (guardrail pair
  still carried verbatim), through the ``clone_pipeline`` entrypoint;
* a run built from the clone's snapshot resolves the clone's OWN gate (the
  discriminating runtime proof: against the pre-fix carry, the foreign pin set
  yields ``warn``/``None`` instead of the clone gate's ``block``).

Postgres-only statements are bypassed at the session boundary so the
surrounding Python runs against mocks — same technique as
``test_clone_split_transaction.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.pipeline import (
    _clone_snapshots,
    _snapshot_to_dict,
    clone_pipeline,
)
from modulo.db.models.eval import Eval
from modulo.db.models.policy_gate import PolicyGate

_ASYNCIO = pytest.mark.asyncio(loop_scope="module")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_SOURCE_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_CLONE_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

_SOURCE_NODE = "00000000-0000-0000-0000-0000000000b1"
_CLONE_NODE = "00000000-0000-0000-0000-0000000000b2"

_SOURCE_EVAL_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440111")
_SOURCE_GATE_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440112")
_CLONE_EVAL_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440222")
_CLONE_GATE_ID = uuid.UUID("660e8400-e29b-41d4-a716-446655440223")


class _Row:
    """Attribute bag standing in for an ORM row (mirrors the clone tests)."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _BeginCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


def _scalar_result(row: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return result


def _scalars_result(rows: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = list(rows)
    return result


def _source_pins() -> list[dict[str, Any]]:
    """The source pipeline's policy-gate pin set (foreign to any clone)."""
    return [
        {
            "policy_gate_id": str(_SOURCE_GATE_ID),
            "eval_id": str(_SOURCE_EVAL_ID),
            "action": "block",
            "node_id": _SOURCE_NODE,
        }
    ]


def _guardrail_pins() -> list[dict[str, Any]]:
    return [{"name": "pii-guard", "config": {"action": "warn"}}]


def _source_snapshot_row() -> _Row:
    return _Row(
        id=uuid.uuid4(),
        pipeline_id=_SOURCE_PIPELINE_ID,
        snapshot_version=7,
        account_id=_ACCOUNT_ID,
        environment_profile_id=None,
        graph_json={"nodes": [{"id": _SOURCE_NODE, "node_type": "agent"}], "edges": []},
        connector_bindings_json=[],
        schema_pins_json=[],
        prompt_pins_json=[],
        model_backend_pins_json=[],
        composite_bindings_json=None,
        parameter_bindings_json=None,
        guardrail_pins_json=_guardrail_pins(),
        guardrail_pins_fingerprint="guardrail-fp-source",
        policy_gate_pins_json=_source_pins(),
        policy_gate_pins_fingerprint="policy-fp-source",
        tag=None,
        notes=None,
        default_autonomy_level="manual_approval",
        max_autonomy_level=None,
        config_json={},
        run_context_defaults={},
        stdout_retention_config=None,
    )


def _make_source() -> _Row:
    return _Row(
        id=_SOURCE_PIPELINE_ID,
        name="Original Pipeline",
        description="desc",
        visibility="org",
        owner_team_id=None,
        max_concurrent_runs=5,
        lock_wait_timeout_seconds=300,
        node_timeout_seconds=300,
        run_context_defaults={},
        graph_nodes_json=[{"id": _SOURCE_NODE, "node_type": "agent"}],
        default_autonomy_level="manual_approval",
        stale_run_timeout_minutes=30,
        stdout_retention_config=None,
    )


def _read_session(snapshot_row: _Row) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _scalar_result(_make_source()),
            _scalars_result([]),
            _scalars_result([snapshot_row]),
            _scalars_result([]),
        ]
    )
    session.begin = MagicMock(return_value=_BeginCM())
    session.add = MagicMock()
    session.flush = AsyncMock()
    # ``set_rls_org`` reads ``session.in_transaction()`` synchronously; an
    # AsyncMock would hand back an un-awaited coroutine (and a warning).
    session.in_transaction = MagicMock(return_value=True)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _main_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _clone_eval_and_gate() -> tuple[Eval, PolicyGate]:
    clone_eval = Eval(
        id=_CLONE_EVAL_ID,
        organisation_id=_ORG_ID,
        pipeline_id=_CLONE_PIPELINE_ID,
        node_id=uuid.UUID(_CLONE_NODE),
        account_id=_ACCOUNT_ID,
        eval_type="model_judge",
        config_json={},
        version=1,
    )
    clone_gate = PolicyGate(
        id=_CLONE_GATE_ID,
        organisation_id=_ORG_ID,
        eval_id=_CLONE_EVAL_ID,
        node_id=uuid.UUID(_CLONE_NODE),
        action="block",
        version=3,
        enabled=True,
        enabled_at=datetime.now(UTC),
    )
    return clone_eval, clone_gate


async def _clone(snapshot_row: _Row) -> tuple[AsyncMock, Any]:
    read_session = _read_session(snapshot_row)
    main_session = _main_session()
    cloned = await clone_pipeline(
        main_session,
        org_id=_ORG_ID,
        pipeline_id=_SOURCE_PIPELINE_ID,
        account_id=_ACCOUNT_ID,
        _read_session_factory=lambda: read_session,
    )
    assert cloned is not None
    return main_session, cloned


def _cloned_snapshot(main_session: AsyncMock) -> Any:
    added = [
        call.args[0] for call in main_session.add.call_args_list if type(call.args[0]).__name__ == "PipelineSnapshot"
    ]
    assert added, "expected the cloned PipelineSnapshot to be added"
    return added[0]


# ---------------------------------------------------------------------------
# The clone's snapshot columns
# ---------------------------------------------------------------------------


@_ASYNCIO
class TestCloneStripsPolicyGatePins:
    async def test_clone_nulls_policy_gate_pin_pair(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            main_session, _ = await _clone(_source_snapshot_row())

        cloned_snapshot = _cloned_snapshot(main_session)
        assert cloned_snapshot.policy_gate_pins_json is None
        assert cloned_snapshot.policy_gate_pins_fingerprint is None

    async def test_clone_discloses_the_dropped_pins(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            await _clone(_source_snapshot_row())

        assert "policy_gate pin(s) dropped on clone of pipeline" in caplog.text
        assert "1 pin(s) on snapshot v7" in caplog.text

    async def test_clone_keeps_guardrail_pins_verbatim(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            main_session, _ = await _clone(_source_snapshot_row())

        cloned_snapshot = _cloned_snapshot(main_session)
        assert cloned_snapshot.guardrail_pins_json == _guardrail_pins()
        assert cloned_snapshot.guardrail_pins_fingerprint == "guardrail-fp-source"

    async def test_gate_free_source_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        snapshot_row = _source_snapshot_row()
        snapshot_row.policy_gate_pins_json = None
        snapshot_row.policy_gate_pins_fingerprint = None

        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            main_session, _ = await _clone(snapshot_row)

        cloned_snapshot = _cloned_snapshot(main_session)
        assert cloned_snapshot.policy_gate_pins_json is None
        assert "policy_gate pin(s) dropped on clone" not in caplog.text


# ---------------------------------------------------------------------------
# The runtime discriminator: a run from the clone resolves the clone's OWN gate
# ---------------------------------------------------------------------------


@_ASYNCIO
class TestCloneRunGateResolution:
    async def test_run_from_clone_resolves_clone_gate_not_foreign_pins(self, caplog: pytest.LogCaptureFixture) -> None:
        """With the fix the clone's snapshot carries NO pins, so the clone's own
        live gate governs the run (block). Against the pre-fix carry, the
        source's foreign pin set declares a pinned universe in which the clone
        eval is unpinned — the gate silently degrades to warn and no
        PolicyGateDecision can be written.
        """
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            main_session, _ = await _clone(_source_snapshot_row())
        cloned_snapshot = _cloned_snapshot(main_session)

        clone_eval, clone_gate = _clone_eval_and_gate()

        eval_defs = PipelineExecutor._build_eval_defs_by_node(
            [(clone_eval, clone_gate)],
            _ORG_ID,
            _CLONE_PIPELINE_ID,
            cloned_snapshot.policy_gate_pins_json,
        )

        defs = eval_defs[str(uuid.UUID(_CLONE_NODE))]
        assert len(defs) == 1
        assert defs[0].failure_behaviour == "block"
        assert defs[0].policy_gate_id == _CLONE_GATE_ID


class TestForeignPinCarrySuppressesGate:
    def test_pre_fix_carry_would_suppress_the_clone_gate(self) -> None:
        """Prove-the-discriminator: feeding the SOURCE's pins (the pre-fix
        carry) into the same build downgrades the clone gate to an ungoverned
        warn — the bug this ticket removes.
        """
        clone_eval, clone_gate = _clone_eval_and_gate()

        eval_defs = PipelineExecutor._build_eval_defs_by_node(
            [(clone_eval, clone_gate)],
            _ORG_ID,
            _CLONE_PIPELINE_ID,
            _source_pins(),
        )

        defs = eval_defs[str(uuid.UUID(_CLONE_NODE))]
        assert len(defs) == 1
        assert defs[0].failure_behaviour == "warn"
        assert defs[0].policy_gate_id is None


# ---------------------------------------------------------------------------
# Direct coverage of both carry-through sites
# ---------------------------------------------------------------------------


class TestSnapshotDictStripsPins:
    def test_snapshot_to_dict_nulls_policy_pair(self) -> None:
        result = _snapshot_to_dict(_source_snapshot_row(), [])

        assert result["policy_gate_pins_json"] is None
        assert result["policy_gate_pins_fingerprint"] is None

    def test_snapshot_to_dict_keeps_guardrail_pair(self) -> None:
        result = _snapshot_to_dict(_source_snapshot_row(), [])

        assert result["guardrail_pins_json"] == _guardrail_pins()
        assert result["guardrail_pins_fingerprint"] == "guardrail-fp-source"


def _source_snapshot_dict() -> dict[str, Any]:
    return {
        "snapshot_version": 7,
        "account_id": _ACCOUNT_ID,
        "environment_profile_id": None,
        "graph_json": {"nodes": [{"id": _SOURCE_NODE, "node_type": "agent"}], "edges": []},
        "connector_bindings_json": [],
        "schema_pins_json": [],
        "prompt_pins_json": [],
        "model_backend_pins_json": [],
        "composite_bindings_json": None,
        "parameter_bindings_json": None,
        "guardrail_pins_json": _guardrail_pins(),
        "guardrail_pins_fingerprint": "guardrail-fp-source",
        "policy_gate_pins_json": _source_pins(),
        "policy_gate_pins_fingerprint": "policy-fp-source",
        "tag": None,
        "notes": None,
        "default_autonomy_level": "manual_approval",
        "max_autonomy_level": None,
        "config_json": {},
        "run_context_defaults": {},
        "stdout_retention_config": None,
        "pins": [],
    }


@_ASYNCIO
class TestCloneSnapshotsWriterStripsPins:
    async def test_writer_nulls_policy_pair(self, caplog: pytest.LogCaptureFixture) -> None:
        """``_clone_snapshots`` re-strips defensively: a caller handing it a
        raw dict that still carries pins (bypassing ``_snapshot_to_dict``)
        must not leak them onto the clone.
        """
        session = _main_session()
        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.pipeline"):
            count = await _clone_snapshots(
                session,
                [_source_snapshot_dict()],
                source_id=_SOURCE_PIPELINE_ID,
                cloned_id=_CLONE_PIPELINE_ID,
                org_id=_ORG_ID,
            )

        assert count == 1
        cloned_snapshot = _cloned_snapshot(session)
        assert cloned_snapshot.policy_gate_pins_json is None
        assert cloned_snapshot.policy_gate_pins_fingerprint is None
        assert cloned_snapshot.guardrail_pins_json == _guardrail_pins()
        assert "policy_gate pin(s) dropped on clone of pipeline" in caplog.text
