"""Unit tests for migration 0334_runs_webhook_decay_sweep_index (FAR-2116).

Structural — load the migration module and assert its contract without a
database, and pin the four-way agreement behind the pipeline-decay sweep:

1. the predicate the sweep's SELECT ACTUALLY executes
   (``core/pipeline_decay_alerts.build_health_query``) — captured by running
   the real sweep against a recording session and compiling the statement for
   PostgreSQL, never by reading the sweep's source;
2. the migration's ``postgresql_where`` partial predicate;
3. the ``Run`` model's declared ``ix_runs_webhook_decay_sweep`` index;
4. the ``TERMINAL_STATUSES`` vocabulary the sweep filters on.

A future change to any one of them (a new terminal status, a changed trigger
filter, a re-keyed index) fails here instead of silently orphaning the index
and dropping a 60-second cron back to a full scan of ``runs``.

They run without a database.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Self
from unittest.mock import patch

from sqlalchemy import Index
from sqlalchemy.dialects import postgresql

from modulo.core import pipeline_decay_alerts as decay
from modulo.db.models.run import TERMINAL_STATUSES, Run
from tests.unit.db._migration_graph import assert_single_linear_head, script_directory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0334_runs_webhook_decay_sweep_index"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0333_trigger_event_findings"
_INDEX_NAME = "ix_runs_webhook_decay_sweep"
_KEY_COLUMNS = ("completed_at",)
_INCLUDE_COLUMNS = ("organisation_id", "pipeline_id", "status", "node_deadline_watchdog_fired_count")


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


def _upgrade_calls() -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    module = _load_migration()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    with patch.object(module, "op") as mock_op:
        mock_op.get_bind.return_value.dialect.name = "postgresql"
        mock_op.create_index.side_effect = _record
        module.upgrade()
    return calls


class _EmptyResult:
    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    """Records every statement; returns no rows so the sweep only reads."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _EmptyResult:
        self.statements.append(stmt)
        return _EmptyResult()


async def _sweep_select() -> Any:
    session = _RecordingSession()

    def factory() -> Any:
        return session

    await decay.evaluate_pipeline_decay_alerts(
        factory=factory,
        redis_client=_NullRedis(),
        notifier=_NullNotifier(),
        now=datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
    )
    assert len(session.statements) == 1, (
        f"expected exactly the bounded aggregate SELECT, got {len(session.statements)} statements"
    )
    return session.statements[0]


class _NullRedis:
    async def scan_iter(self, match: str = "*") -> Any:
        for key in ():
            yield key

    async def get(self, key: str) -> None:
        return None


class _NullNotifier:
    async def dispatch_event(self, org_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> list[Any]:
        return []


def _compile(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _extract_where(compiled_sql: str) -> str:
    """The TOP-LEVEL WHERE clause.

    Searched from ``FROM runs`` onwards: the projection also contains a
    ``FILTER (WHERE status = 'complete')``, which is not the query predicate.
    """
    from_match = re.search(r"\bFROM\s+runs\b", compiled_sql, re.IGNORECASE)
    assert from_match is not None, f"no FROM runs in compiled sweep SELECT:\n{compiled_sql}"
    tail = compiled_sql[from_match.start() :]
    match = re.search(r"\bWHERE\b(.*?)(?:\bGROUP BY\b|\bORDER BY\b|\bLIMIT\b|$)", tail, re.DOTALL | re.IGNORECASE)
    assert match is not None, f"no WHERE clause in compiled sweep SELECT:\n{compiled_sql}"
    return match.group(1)


def _canonical(predicate: str) -> str:
    """Whitespace-normalised, qualifier-stripped, IN-order-free predicate."""
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


def _migration_predicate() -> str:
    return str(_load_migration()._DECAY_WHERE)


def _model_index() -> Index:
    declared = {idx.name: idx for idx in Run.__table__.indexes if idx.name is not None}
    assert _INDEX_NAME in declared, f"model/migration drift: {_INDEX_NAME} missing from the Run model"
    return declared[_INDEX_NAME]


def _model_predicate(index: Index) -> str:
    return str(index.dialect_options["postgresql"]["where"])


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------


class TestChain:
    def test_single_linear_head(self) -> None:
        assert_single_linear_head(script_directory())

    def test_down_revision_is_0333_trigger_event_findings(self) -> None:
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
    def test_creates_exactly_one_index(self) -> None:
        calls = _upgrade_calls()
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == _INDEX_NAME
        assert args[1] == "runs"
        assert tuple(args[2]) == _KEY_COLUMNS
        assert tuple(kwargs["postgresql_include"]) == _INCLUDE_COLUMNS
        assert kwargs["if_not_exists"] is True

    def test_is_postgres_only(self) -> None:
        module = _load_migration()
        with patch.object(module, "op") as mock_op:
            mock_op.get_bind.return_value.dialect.name = "sqlite"
            module.upgrade()
            mock_op.create_index.assert_not_called()


# ---------------------------------------------------------------------------
# Predicate parity (the whole point of this test)
# ---------------------------------------------------------------------------


class TestPredicateParity:
    async def test_sweep_select_static_filter_matches_the_index_predicate(self) -> None:
        where = _canonical(_extract_where(_compile(await _sweep_select())))
        predicate = _canonical(_migration_predicate())
        assert where.startswith(predicate), (
            "the sweep's static filter no longer implies the partial index predicate, so the "
            f"planner cannot use {_INDEX_NAME}:\n  sweep:     {where}\n  predicate: {predicate}"
        )

    async def test_sweep_select_filters_the_two_hour_window_on_the_index_key(self) -> None:
        where = _canonical(_extract_where(_compile(await _sweep_select())))
        assert "completed_at >= " in where

    def test_migration_and_model_predicates_are_identical(self) -> None:
        assert _canonical(_model_predicate(_model_index())) == _canonical(_migration_predicate())

    def test_predicate_status_list_is_the_terminal_vocabulary(self) -> None:
        assert _statuses(_migration_predicate()) == frozenset(TERMINAL_STATUSES)
        assert _statuses(_model_predicate(_model_index())) == frozenset(TERMINAL_STATUSES)

    def test_sweep_status_list_is_the_terminal_vocabulary(self) -> None:
        assert frozenset(decay.DECAY_STATUSES) == frozenset(TERMINAL_STATUSES)

    def test_predicate_pins_the_webhook_trigger_type(self) -> None:
        assert "trigger_type = 'webhook'" in _canonical(_migration_predicate())
        assert decay.DECAY_TRIGGER_TYPE == "webhook"

    def test_model_index_key_and_include_columns(self) -> None:
        index = _model_index()
        assert tuple(column.name for column in index.columns) == _KEY_COLUMNS
        assert tuple(index.dialect_options["postgresql"]["include"]) == _INCLUDE_COLUMNS
