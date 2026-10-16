"""Shared test helpers for the ``graph_validator`` unit package.

Facts defined once here so the test suite cannot drift apart:

- ``codes_of`` — the issue-code extraction (was near-identical in nine modules).
- ``UUID_A`` / ``UUID_B`` / ``VALID_DESCRIPTION`` — the reserved-id stand-ins
  and the valid HITL gate description (were re-declared in five HITL modules).
- ``session_returning`` — the no-DB mock session (was re-declared in five
  modules). Returns *rows* via ``execute(...).scalars().all()`` on every call.
- ``gated_edge_graph`` / ``hitl_node_graph`` — the two HITL gate graph shapes
  (edge-level ``hitl_review_config`` / FAR-402 node-level ``hitl_config``);
  callers construct their own ``config`` dict, only the graph skeleton is
  shared.
- ``schema_version_row`` / ``schema_version_session`` — the SchemaVersion row
  stand-in and a schema-version lookup session that resolves rows by the
  query's OWN ``(schema_id, version)`` filter (structural), never by call
  order; statements not filtering ``schema_versions`` get an empty result.
- ``snapshot`` — the PipelineSnapshot stand-in ``validate_for_run`` reads
  (was re-declared in the schema-enforcement and pre-existing-marker modules).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList
from sqlalchemy.sql.operators import eq

from modulo.core.graph_validator import ValidationResult

UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
VALID_DESCRIPTION = "Approve the deploy only after a human has reviewed the plan."


def codes_of(result: ValidationResult) -> set[str]:
    """Return the set of issue codes present in *result*."""
    return {i.code for i in result.issues}


def session_returning(rows: list[Any]) -> AsyncMock:
    """Mock session whose execute() returns the given rows via .scalars().all()."""
    session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = rows
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def snapshot(
    graph_json: dict[str, Any],
    *,
    created_at: datetime,
    schema_pins_json: list[Any] | None = None,
    connector_bindings_json: list[Any] | None = None,
    model_backend_pins_json: list[Any] | None = None,
    pipeline_id: Any = None,
    snapshot_id: Any = None,
    environment_profile_id: Any = None,
) -> MagicMock:
    """A PipelineSnapshot stand-in carrying the fields ``validate_for_run`` reads.

    ``created_at`` drives the Phase 1 pre-existing cutover decision, so callers
    pass it explicitly (a pre-cutover snapshot exercises the pre-existing path).
    ``id`` is exposed as ``snapshot_id`` to avoid shadowing the builtin.
    """
    snap = MagicMock()
    snap.graph_json = graph_json
    snap.schema_pins_json = [] if schema_pins_json is None else schema_pins_json
    snap.connector_bindings_json = [] if connector_bindings_json is None else connector_bindings_json
    snap.model_backend_pins_json = [] if model_backend_pins_json is None else model_backend_pins_json
    snap.created_at = created_at
    snap.pipeline_id = uuid.uuid4() if pipeline_id is None else pipeline_id
    snap.id = uuid.uuid4() if snapshot_id is None else snapshot_id
    snap.environment_profile_id = environment_profile_id
    return snap


def gated_edge_graph(config: dict[str, Any]) -> dict[str, Any]:
    """Edge-level HITL gate: *config* attached as ``hitl_review_config``."""
    return {
        "nodes": [{"id": UUID_A}, {"id": UUID_B}],
        "edges": [{"source": UUID_A, "target": UUID_B, "type": "normal", "hitl_review_config": config}],
    }


def hitl_node_graph(config: dict[str, Any]) -> dict[str, Any]:
    """FAR-402 node-level HITL gate: *config* attached as node ``hitl_config``."""
    return {
        "nodes": [
            {"id": UUID_A, "node_type": "hitl", "hitl_config": config},
            {"id": UUID_B},
        ],
        "edges": [{"source": UUID_A, "target": UUID_B, "type": "normal"}],
    }


def schema_version_row(
    schema_id: Any,
    definition_json: dict[str, Any] | None,
    *,
    version: str = "1.0",
) -> MagicMock:
    """A SchemaVersion row stand-in for the pinned-version definition lookup."""
    sv = MagicMock()
    sv.schema_id = schema_id
    sv.version = version
    sv.definition_json = definition_json
    return sv


def _pin_values(stmt: Any) -> tuple[str, str] | None:
    """Extract the ``(schema_id, version)`` equality literals from a SELECT.

    Returns ``None`` when the statement does not equality-filter both columns,
    which lets non-SchemaVersion statements fall through to an empty result
    instead of leaking a row to an unrelated DB-backed check.
    """
    clause = stmt.whereclause
    if clause is None:
        return None
    if isinstance(clause, BooleanClauseList):
        clauses = list(clause.clauses)
    elif isinstance(clause, BinaryExpression):
        clauses = [clause]
    else:
        return None
    schema_id: str | None = None
    version: str | None = None
    for expr in clauses:
        if not isinstance(expr, BinaryExpression) or expr.operator is not eq:
            continue
        column_name = getattr(expr.left, "key", None)
        if column_name == "schema_id":
            schema_id = str(expr.right.value)
        elif column_name == "version":
            version = str(expr.right.value)
    if schema_id is None or version is None:
        return None
    return schema_id, version


def schema_version_session(rows: list[MagicMock]) -> AsyncMock:
    """Mock session resolving ``select(SchemaVersion)`` by the query's own filter.

    Rows are indexed by ``(schema_id, version)``; each execute returns the row
    whose pin the statement filters on (or an empty result when no row matches,
    or when the statement does not equality-filter both columns). Vacuous by
    design for calls the suite does not pin, so evaluation order of the
    validator's per-pin lookups cannot break a test.
    """
    by_pin = {(str(row.schema_id), str(row.version)): row for row in rows}

    def _execute_side(stmt: Any, *_a: Any, **_kw: Any) -> MagicMock:
        result = MagicMock()
        row = by_pin.get(_pin_values(stmt))
        result.scalar_one_or_none.return_value = row
        result.scalar_one.return_value = row
        scalars_result = MagicMock()
        scalars_result.all.return_value = [row] if row is not None else []
        result.scalars.return_value = scalars_result
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute_side)
    return session
