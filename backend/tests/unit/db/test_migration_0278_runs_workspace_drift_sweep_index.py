"""Unit tests for migration 0278_runs_workspace_drift_sweep_index.

Structural — load the migration module and assert its contract without a
database, and pin the four-way agreement behind FAR-1438:

1. the predicate the reconcile tick's compensating sweep ACTUALLY executes
   (``core/cron_helpers.py::_sweep_workspace_input_drift_flags``) — captured
   by running the real function against a recording session and compiling its
   SELECT for PostgreSQL, never by reading the sweep's source;
2. the migration's ``postgresql_where`` / ``sqlite_where`` partial predicate;
3. the ``Run`` model's declared ``ix_runs_workspace_drift_sweep`` index;
4. the ``TERMINAL_STATUSES`` vocabulary the sweep filters on.

A future change to any one of them (a new terminal status, a changed flag
condition, a re-keyed index) fails here instead of silently orphaning the
index and dropping the sweep back to a full-table scan of ``runs`` every
60 seconds. Also pins the chain
(``0277_run_daily_facts_trigger_dispatch_phase`` ->
``0278_runs_workspace_drift_sweep_index`` ->
``0279_table_autovacuum_tuning`` ->
``0280_runs_node_deadline_watchdog_fired_count`` ->
``0281_org_api_keys_grants`` ->
``0282_env_profiles_kubernetes`` ->
``0283_runs_drop_unused_indexes`` ->
``0284_add_rejected_run_status`` ->
``0285_system_audit_events`` ->
``0286_pipeline_run_state`` ->
``0287_team_rls_lifecycle_evals`` ->
``0288_runs_execution_origin`` ->
``0289_pipelines_environment_profile`` as the single linear head) and the
``ORDER BY id`` / ``LIMIT 200`` access shape
the ``(id)`` key is chosen to serve.

They run without a database.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Self
from unittest.mock import patch

from alembic.script import ScriptDirectory
from sqlalchemy import Index
from sqlalchemy.dialects import postgresql

from modulo.core.cron_helpers import _sweep_workspace_input_drift_flags
from modulo.db.models.run import TERMINAL_STATUSES, Run

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0278_runs_workspace_drift_sweep_index"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0277_run_daily_facts_trigger_dispatch_phase"
_CHAIN_HEAD_MIGRATION = "0289_pipelines_environment_profile"
_INDEX_NAME = "ix_runs_workspace_drift_sweep"
_KEY_COLUMNS = ("id",)

#: The predicate AS 0278 CREATED THE INDEX (historical; FAR-1487's migration
#: 0284 re-created the index with ``'rejected'`` added to the IN list).
_PREDICATE_0278 = (
    "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed') "
    "AND workspace_inputs_drift_detected IS NULL"
)

#: The sweep's WHERE clause as it must read in the index TODAY (after 0284).
#: The IN list is the ``TERMINAL_STATUSES`` vocabulary (asserted below);
#: parity comparisons canonicalise order so frozenset iteration can never
#: flake them.
_PREDICATE = (
    "status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed', 'rejected') "
    "AND workspace_inputs_drift_detected IS NULL"
)
_REJECTED_MIGRATION_PATH = _VERSIONS / "0284_add_rejected_run_status.py"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_calls(entry_point: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Every index DDL call the given entry point makes, as (args, kwargs)."""
    module = _load_migration()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    with patch.object(module, "op") as mock_op:
        mock_op.create_index.side_effect = _record
        mock_op.drop_index.side_effect = _record
        getattr(module, entry_point)()
    return calls


class _EmptyResult:
    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    """Async session double: records every statement and returns no rows, so
    the sweep executes exactly its bounded SELECT and nothing else."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> _RecordingSession:
        return self

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _EmptyResult:
        self.statements.append(stmt)
        return _EmptyResult()


async def _sweep_select() -> Any:
    """Run the real sweep and return the SELECT statement it executes."""
    session = _RecordingSession()

    def factory() -> Any:
        return session

    result = await _sweep_workspace_input_drift_flags(factory)
    assert result == {"scanned": 0, "corrected": 0}, f"sweep swallowed an error: {result}"
    assert len(session.statements) == 1, (
        f"expected exactly the bounded SELECT (0 rows match), got {len(session.statements)} statements"
    )
    return session.statements[0]


def _compile(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _extract_where(compiled_sql: str) -> str:
    match = re.search(r"\bWHERE\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)", compiled_sql, re.DOTALL | re.IGNORECASE)
    assert match is not None, f"no WHERE clause in compiled sweep SELECT:\n{compiled_sql}"
    return match.group(1)


def _canonical(predicate: str) -> str:
    """Whitespace-normalised, table-qualifier-stripped, IN-list-order-free
    canonical form of a predicate, for order-insensitive equality."""
    norm = re.sub(r"\s+", " ", predicate).strip()
    norm = re.sub(r"\bruns\.", "", norm)
    norm = norm.lower()
    in_match = re.search(r"in \(([^()]*)\)", norm)
    if in_match is not None:
        items = sorted(item.strip().strip("'\"") for item in in_match.group(1).split(","))
        rendered = "in (" + ", ".join(f"'{item}'" for item in items) + ")"
        norm = norm[: in_match.start()] + rendered + norm[in_match.end() :]
    return norm


def _statuses(predicate: str) -> frozenset[str]:
    match = re.search(r"in \(([^()]*)\)", predicate.lower())
    assert match is not None, f"no IN list in predicate: {predicate}"
    return frozenset(item.strip().strip("'\"") for item in match.group(1).split(","))


def _model_index() -> Index:
    declared = {idx.name: idx for idx in Run.__table__.indexes if idx.name is not None}
    assert _INDEX_NAME in declared, f"model/migration drift: {_INDEX_NAME} missing from the Run model"
    return declared[_INDEX_NAME]


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------


class TestChain:
    def test_single_head_is_0289_pipelines_environment_profile(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_CHAIN_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0277_run_daily_facts_trigger_dispatch_phase(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


class TestUpgrade:
    def test_creates_exactly_one_index_on_runs_id(self) -> None:
        calls = _migration_calls("upgrade")
        assert len(calls) == 1, f"expected exactly one index DDL call, got {calls}"
        args, _kwargs = calls[0]
        assert args[0] == _INDEX_NAME
        assert args[1] == "runs"
        # Key (id): the only access requirement left once both filter conjuncts
        # live in the partial predicate is ORDER BY id, which (id) serves as a
        # plain ordered scan stopping at LIMIT 200.
        assert tuple(args[2]) == _KEY_COLUMNS

    def test_postgresql_where_matches_the_sweep_predicate(self) -> None:
        calls = _migration_calls("upgrade")
        kwargs = calls[0][1]
        pg_where = kwargs.get("postgresql_where")
        assert pg_where is not None, "upgrade must declare postgresql_where"
        assert _canonical(str(pg_where)) == _canonical(_PREDICATE_0278), (
            f"migration postgresql_where drifted from the sweep predicate:\n{pg_where}"
        )

    def test_sqlite_where_matches_the_postgresql_where(self) -> None:
        calls = _migration_calls("upgrade")
        kwargs = calls[0][1]
        sqlite_where = kwargs.get("sqlite_where")
        assert sqlite_where is not None, "upgrade must declare sqlite_where (create_all'd test schemas)"
        assert _canonical(str(sqlite_where)) == _canonical(_PREDICATE_0278), (
            f"migration sqlite_where drifted from the sweep predicate:\n{sqlite_where}"
        )

    def test_predicate_statuses_are_the_terminal_vocabulary(self) -> None:
        """A new TERMINAL_STATUSES member must be added to the index
        predicate in the same change — otherwise that status falls outside
        the partial index and the sweep seq-scans for it. 0278 created the
        index for the vocabulary of its day; ``rejected`` (FAR-1487) was added
        by the 0284 re-creation, so 0278's list + ``rejected`` == today's."""
        assert _statuses(_PREDICATE_0278) | {"rejected"} == frozenset(TERMINAL_STATUSES)
        assert _statuses(_PREDICATE) == frozenset(TERMINAL_STATUSES)

    def test_predicate_carries_both_conjuncts(self) -> None:
        norm = _canonical(_PREDICATE)
        assert "status in (" in norm
        assert "workspace_inputs_drift_detected is null" in norm


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


class TestDowngrade:
    def test_downgrade_drops_the_index_by_name(self) -> None:
        calls = _migration_calls("downgrade")
        assert len(calls) == 1, f"expected exactly one drop_index call, got {calls}"
        args, kwargs = calls[0]
        assert args[0] == _INDEX_NAME
        assert kwargs.get("table_name") == "runs"


# ---------------------------------------------------------------------------
# Model parity
# ---------------------------------------------------------------------------


class TestModelParity:
    def test_model_declares_the_index_with_the_key_columns(self) -> None:
        index = _model_index()
        assert tuple(col.name for col in index.columns) == _KEY_COLUMNS

    def test_model_postgresql_where_matches_the_migration(self) -> None:
        pg_where = _model_index().dialect_options["postgresql"].get("where")
        assert pg_where is not None, f"{_INDEX_NAME} model missing postgresql_where"
        assert _canonical(str(pg_where)) == _canonical(_PREDICATE), (
            f"model/migration drift: postgresql_where {pg_where} != {_PREDICATE}"
        )

    def test_model_sqlite_where_matches_the_migration(self) -> None:
        sqlite_where = _model_index().dialect_options["sqlite"].get("where")
        assert sqlite_where is not None, f"{_INDEX_NAME} model missing sqlite_where"
        assert _canonical(str(sqlite_where)) == _canonical(_PREDICATE), (
            f"model/migration drift: sqlite_where {sqlite_where} != {_PREDICATE}"
        )


# ---------------------------------------------------------------------------
# THE guard: the sweep's real predicate vs the index's postgresql_where
# ---------------------------------------------------------------------------


class TestSweepPredicateParity:
    async def test_sweep_where_matches_the_index_postgresql_where(self) -> None:
        """The predicate the sweep actually executes == the partial index's
        ``postgresql_where``. Captured from a real sweep run, so editing the
        sweep's WHERE without widening the index (or vice versa) fails here."""
        sweep_where = _extract_where(_compile(await _sweep_select()))

        # The index as it stands TODAY is the one migration 0284 (re)created.
        spec = importlib.util.spec_from_file_location("migration_0284_rejected", _REJECTED_MIGRATION_PATH)
        assert spec is not None
        assert spec.loader is not None
        rejected_migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rejected_migration)
        index_where = rejected_migration._DRIFT_PREDICATE_NEW

        assert _canonical(sweep_where) == _canonical(index_where), (
            "sweep predicate and index postgresql_where disagree — the index would be orphaned.\n"
            f"sweep: {sweep_where.strip()}\nindex: {index_where}"
        )

    async def test_sweep_statuses_are_the_terminal_vocabulary(self) -> None:
        sweep_where = _extract_where(_compile(await _sweep_select()))
        assert _statuses(sweep_where) == frozenset(TERMINAL_STATUSES)

    async def test_sweep_access_shape_is_order_by_id_limit_200(self) -> None:
        """The index key is (id); pin the access shape it exists to serve so
        a future re-ordering of the query invalidates this migration's design
        (and the parity test above) in the same pass."""
        compiled = _compile(await _sweep_select())
        assert "ORDER BY runs.id" in compiled, f"sweep no longer orders by runs.id:\n{compiled}"
        assert "LIMIT 200" in compiled, f"sweep budget is no longer LIMIT 200:\n{compiled}"
