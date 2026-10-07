"""Helpers for contract-correct SQLAlchemy async session test doubles."""

from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock

from sqlalchemy.sql import Select

# require_permission's per-request kill-switch read (ADR 047 DECISION 3). The
# strict mock raises on un-stubbed queries, so this SELECT on the organisations
# authz_enforce column is stubbed by default to the enforce=True default.
_AUTHZ_ENFORCE_SNIPPET = "authz_enforce"

# FAR-223 PR A: the graph-save route loads the pipeline's guardrail eval rows
# (select(Eval).where(pipeline_id=..., organisation_id=...,
# eval_type="guardrail")) to enforce the per-node guardrail cap at authoring
# time. FAR-1101 chunk 3b redirected the load to the ``evals`` table. The
# strict mock raises on un-stubbed queries, so this SELECT on the evals table is
# stubbed by default to no rows — no guardrail rows means no cap violation
# (no 422).
_GUARDRAIL_ROWS_SNIPPET = "FROM evals"

# FAR-526 Part A: the context-bound decrypt helper (decode_stored_secret_scoped)
# (re-)applies the RLS org via set_rls_org, which issues a
# ``SELECT set_config('app.organisation_id', ...)`` inside the caller's active
# transaction. Routing secrets through the scoped helper is the new normal, so
# the strict mock treats RLS set_config as a benign no-op (an empty result) —
# the session is already scoped by the test, and the config write is a no-return
# SET-LOCAL equivalent.
_RLS_SET_CONFIG_SNIPPET = "set_config"

# FAR-583: the run-blob reads go through the run_node_outputs repo reader,
# which issues TWO queries per read — the new-table rows page and the legacy
# fallback SELECT of the three ``runs`` blob columns. The strict mock serves
# an EMPTY new table and a legacy row with no dicts ("no blobs stored")
# unless the test stubs the reader itself. The census snippet is matched
# FIRST: its column list also contains the legacy column names.
_RUN_NODE_OUTPUTS_SNIPPET = "FROM run_node_outputs"
_RUNS_LEGACY_BLOBS_SNIPPET = "runs.outputs_json, runs.node_telemetry_json, runs.raw_output_markers"

# Issue #1801: the pipeline mutation endpoints re-verify the team gate INSIDE
# the mutation transaction (``_reapply_team_gate_inside_mutation_txn``), which
# issues a ``SELECT ... FROM pipelines ... FOR UPDATE`` on the strict mock.
# Serve an org-visible row by default (owner_team_id None) so the re-check
# short-circuits; tests exercising the gate itself stub the result explicitly.
_PIPELINE_ROW_SNIPPET = "FROM pipelines"

# FAR-1514: the request-time team gate on the ``{lifecycle_map_id}`` routes
# (``team_scope.resolve_lifecycle_map_team_scope``) issues
# ``SELECT lifecycle_maps.owner_team_id, lifecycle_maps.visibility FROM
# lifecycle_maps WHERE lifecycle_maps.id = ...`` before the handler runs. The
# strict mock raises on un-stubbed queries, so serve an ORG-visible row by
# default (owner_team_id None) — the gate then short-circuits to "allowed",
# exactly like the pipeline row stub above. Tests exercising the team gate
# itself stub the result explicitly.
_LIFECYCLE_MAP_ROW_SNIPPET = "FROM lifecycle_maps"


def _is_lifecycle_map_row_query(stmt: Any) -> bool:
    """Matches the lifecycle-map team-gate SELECT (and any ORM row-read FROM lifecycle_maps)."""
    if not isinstance(stmt, Select):
        return False
    return _LIFECYCLE_MAP_ROW_SNIPPET.lower() in str(stmt).lower()


def _is_run_node_outputs_query(stmt: Any) -> bool:
    if not isinstance(stmt, Select):
        return False
    return _RUN_NODE_OUTPUTS_SNIPPET in str(stmt)


def _is_runs_legacy_blobs_query(stmt: Any) -> bool:
    if not isinstance(stmt, Select):
        return False
    return _RUNS_LEGACY_BLOBS_SNIPPET in str(stmt)


def _is_pipeline_row_query(stmt: Any) -> bool:
    """Matches the #1801 in-txn gate SELECT (and any ORM row-read FROM pipelines)."""
    if not isinstance(stmt, Select):
        return False
    return _PIPELINE_ROW_SNIPPET.lower() in str(stmt).lower()


def _is_authz_enforce_query(stmt: Any) -> bool:
    if not isinstance(stmt, Select):
        return False
    return _AUTHZ_ENFORCE_SNIPPET in str(stmt)


def _is_guardrail_rows_query(stmt: Any) -> bool:
    if not isinstance(stmt, Select):
        return False
    return _GUARDRAIL_ROWS_SNIPPET in str(stmt)


def _is_rls_set_config_query(stmt: Any) -> bool:
    # set_config is issued via sqlalchemy text(), not select() — match the SQL text.
    return _RLS_SET_CONFIG_SNIPPET in str(stmt)


def configure_rls_preamble(session: AsyncMock) -> AsyncMock:
    """Make the RLS preamble's sync reads sync ``MagicMock`` methods.

    ``set_rls_org`` (``backend/src/modulo/db/rls.py``) calls ``in_transaction()`` and
    ``get_bind()`` synchronously. On a bare ``AsyncMock`` those attributes are
    async mocks whose calls return never-awaited coroutines, leaking
    ``PytestUnraisableException`` noise (FAR-739). The sqlite dialect routes
    ``set_rls_org`` to the generic ``session.info`` branch, so the production
    ``set_config``-per-statement path stays unexercised.
    """
    session.in_transaction = MagicMock(return_value=True)
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    return session


def make_empty_execute_result(*, scalar_value: int | None = None) -> MagicMock:
    """Build a plain-``MagicMock`` DB result whose readers all report "no rows".

    A plain ``MagicMock`` is required — never a child of the ``AsyncMock``
    session: attribute children of an ``AsyncMock`` are themselves AsyncMocks
    whose calls return never-awaited coroutines, the source of
    ``PytestUnraisableException`` noise (FAR-739). Shared by the strict-mock
    opt-in below and the pipeline-engine capacity mocks so the empty-result
    shape cannot drift between the two call sites.

    ``scalar``/``scalar_one`` return ``scalar_value`` (default ``None`` for a
    missing row; the strict mock passes ``0`` for its documented no-cap
    contract).
    """
    result = MagicMock()
    result.scalar.return_value = scalar_value
    result.scalar_one.return_value = scalar_value
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.fetchone.return_value = None
    result.all.return_value = []
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result.scalars.return_value = scalars_mock
    return result


def configure_mock_session(session: AsyncMock, *, allow_empty_execute: bool = False) -> AsyncMock:
    """Configure AsyncSession contracts, requiring explicit query results by default."""
    configure_rls_preamble(session)
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.expunge = MagicMock()
    session.info = {}
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    if allow_empty_execute:
        session.execute = AsyncMock(return_value=make_empty_execute_result(scalar_value=0))
    else:
        execute = AsyncMock()

        def require_explicit_result(*args: Any, **kwargs: Any) -> Any:
            if execute._mock_return_value is not DEFAULT:
                return execute._mock_return_value
            if _is_authz_enforce_query(args[0] if args else None):
                authz_result = MagicMock()
                authz_result.scalar_one_or_none.return_value = None
                return authz_result
            if _is_guardrail_rows_query(args[0] if args else None):
                guardrail_result = MagicMock()
                guardrail_result.scalars.return_value.all.return_value = []
                return guardrail_result
            if _is_rls_set_config_query(args[0] if args else None):
                rls_result = MagicMock()
                rls_result.scalar.return_value = None
                return rls_result
            if _is_run_node_outputs_query(args[0] if args else None):
                rows_result = MagicMock()
                rows_result.all.return_value = []
                return rows_result
            if _is_runs_legacy_blobs_query(args[0] if args else None):
                legacy_result = MagicMock()
                legacy_result.first.return_value = None
                return legacy_result
            if _is_pipeline_row_query(args[0] if args else None):
                pipeline_row = MagicMock()
                pipeline_row.visibility = "org"
                pipeline_row.owner_team_id = None
                pipeline_row.deleted_at = None
                pipeline_result = MagicMock()
                pipeline_result.scalar_one_or_none.return_value = pipeline_row
                return pipeline_result
            if _is_lifecycle_map_row_query(args[0] if args else None):
                # team_scope_resolver reads a (owner_team_id, visibility) row
                # via ``result.first()`` — an org-visible row keeps the gate
                # open for route-shape tests.
                lifecycle_map_result = MagicMock()
                lifecycle_map_result.first.return_value = (None, "org")
                return lifecycle_map_result
            raise AssertionError(
                "Unexpected session.execute(); stub the expected result or opt in with allow_empty_execute=True"
            )

        execute.side_effect = require_explicit_result
        session.execute = execute
    return session
