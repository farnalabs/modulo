"""FAR-2008: db-level ordering of the rollback post-lock hook vs the graph write.

The route-level rollback tests replace ``rollback_to_snapshot`` with a fake that
calls ``post_lock_gate`` itself, so they pin the ROUTE's ordering but would keep
passing if production moved the hook call AFTER it rewrites
``pipeline.graph_nodes_json``. That ordering is the whole point of the hook —
the FAR-488a Agent-row sync (and the connector-team gate) must run BEFORE the
restored graph is persisted, in the same transaction.

This module drives the REAL ``rollback_to_snapshot`` with a fake session and a
recording pipeline row, and asserts the hook is invoked before the
``graph_nodes_json`` assignment. Moving the hook call after the write makes the
recorded order ``["graph_write", "hook"]`` and fails here.

Follows the fake-session style of
``tests/unit/db/crud/test_far1666_policy_gate_graph_writers.py`` without needing
SQLite: every collaborator is a pure/DB seam mocked at the versioning module.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modulo.db.crud.pipeline_snapshot_versioning import rollback_to_snapshot

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ab")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ac")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ad")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ae")

_NODE = {
    "id": "00000000-0000-0000-0000-0000000000af",
    "agent_id": str(_AGENT_ID),
    "agent_commands": ["opencode run -- restored"],
    "position": {"x": 0, "y": 0},
}


class _Result:
    """Stand-in for an ``AsyncResult``: the pipeline for the lock, no edges."""

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    def scalar_one_or_none(self) -> Any:
        return self._pipeline

    def scalars(self) -> list[Any]:
        return []


class _RecordingPipeline:
    """A pipeline whose ``graph_nodes_json`` assignment is order-recorded."""

    def __init__(self) -> None:
        object.__setattr__(self, "order", [])
        object.__setattr__(self, "organisation_id", _ORG_ID)
        object.__setattr__(self, "owner_team_id", None)
        object.__setattr__(self, "graph_nodes_json", [])

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "graph_nodes_json":
            order: list[str] = object.__getattribute__(self, "order")
            order.append("graph_write")
        object.__setattr__(self, name, value)


class _FakeSession:
    """Minimal async session: ``execute`` returns the pipeline, adds/flushes."""

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline
        self.added: list[Any] = []
        self.flush_count = 0

    async def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result(self._pipeline)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


@pytest.fixture
def patch_rollback_collaborators() -> Any:
    """Patch the versioning module's DB/audit seams so the real body executes."""
    with (
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.resolve_effective_privilege",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.enforce_guardrail_binding_strip",
            new=AsyncMock(),
        ),
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.apply_gated_edge_diff",
            new=Mock(
                return_value=SimpleNamespace(
                    weakened_edges=(),
                    weakened_nodes=(),
                    denied=False,
                    has_weakening=False,
                    reason_code=None,
                )
            ),
        ),
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.reconcile_graph_policy_gates",
            new=AsyncMock(),
        ),
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.append_audit_event",
            new=AsyncMock(),
        ),
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.create_snapshot_from_live_graph",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield


def _target_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        pipeline_id=_PIPELINE_ID,
        snapshot_version=3,
        graph_json={"nodes": [dict(_NODE)], "edges": []},
    )


async def test_post_lock_gate_runs_before_the_graph_nodes_write(patch_rollback_collaborators: Any) -> None:
    """The REAL ``rollback_to_snapshot`` invokes ``post_lock_gate`` BEFORE it
    assigns ``pipeline.graph_nodes_json``.

    Fails if the hook call is moved after the write: the recorded order would
    be ``["graph_write", "hook"]`` instead of ``["hook", "graph_write"]``.
    """
    pipeline = _RecordingPipeline()
    session = _FakeSession(pipeline)
    target = _target_snapshot()
    hook_calls: list[tuple[Any, Any]] = []

    async def _hook(gate_target: Any, locked_pipeline: Any) -> None:
        pipeline.order.append("hook")
        hook_calls.append((gate_target, locked_pipeline))

    with patch(
        "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot",
        new=AsyncMock(return_value=target),
    ):
        result = await rollback_to_snapshot(
            session,
            _PIPELINE_ID,
            _SNAPSHOT_ID,
            account_id=_ACCOUNT_ID,
            is_privileged=True,
            caller_type="rest",
            post_lock_gate=_hook,
        )

    assert result is None
    assert pipeline.order == ["hook", "graph_write"], pipeline.order
    assert hook_calls == [(target, pipeline)]
    assert pipeline.graph_nodes_json == [dict(_NODE)]


async def test_no_hook_leaves_only_the_graph_write(patch_rollback_collaborators: Any) -> None:
    """Control: with no hook supplied the only recorded step is the write,
    proving the recording is not picking up an unrelated assignment."""
    pipeline = _RecordingPipeline()
    session = _FakeSession(pipeline)
    target = _target_snapshot()

    with patch(
        "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot",
        new=AsyncMock(return_value=target),
    ):
        await rollback_to_snapshot(
            session,
            _PIPELINE_ID,
            _SNAPSHOT_ID,
            account_id=_ACCOUNT_ID,
            is_privileged=True,
            caller_type="rest",
        )

    assert pipeline.order == ["graph_write"], pipeline.order
