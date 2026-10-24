"""FAR-2040: the 0330 backfill reports progress, so a long migration is not a hang.

A 10M-row ``eval_results`` table is thousands of batches; emitting only the
final total makes the release command look hung. The cadence is pinned here:
one start line, one progress line per N batches (never per batch), one final
line. The loop itself is exercised with a fake connection — no Postgres needed.
"""

from __future__ import annotations

import importlib
import logging
from types import SimpleNamespace
from typing import Any

import pytest

_MIGRATION = importlib.import_module("modulo.db.migrations.versions.0330_eval_verdict_contract")
_LOGGER_NAME = "alembic.runtime.migration"


class _FakeResult:
    def __init__(self, rows: list[tuple[int]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[int]]:
        return self._rows


class _FakeConnection:
    def __init__(self, batches: list[list[tuple[int]]]) -> None:
        self._batches = list(batches)
        self.params: list[dict[str, Any]] = []

    def execute(self, _statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        self.params.append(params or {})
        if not self._batches:
            return _FakeResult([])
        return _FakeResult(self._batches.pop(0))


class _FakeConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeConnection:
        return self._connection

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def fake_backfill(monkeypatch: pytest.MonkeyPatch) -> _FakeConnection:
    # 25 one-row batches, then the empty batch that terminates the loop.
    batches = [[(index,)] for index in range(1, 26)] + [[]]
    connection = _FakeConnection(batches)
    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", lambda: _FakeConnectionContext(connection))
    monkeypatch.setattr(_MIGRATION, "_relation_visible", lambda _conn: True)
    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_eval_results", lambda: False)
    monkeypatch.setattr(_MIGRATION, "_PROGRESS_EVERY_BATCHES", 10)
    monkeypatch.setattr(_MIGRATION, "_PROGRESS_EVERY_SECONDS", 10_000.0)
    return connection


def test_the_backfill_logs_start_progress_and_final_lines(
    fake_backfill: _FakeConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        total = _MIGRATION._backfill_outcome()

    messages = [record.getMessage() for record in caplog.records]
    assert total == 25

    start = [message for message in messages if "starting online outcome backfill" in message]
    assert len(start) == 1
    assert f"batch_size={_MIGRATION._BATCH_SIZE}" in start[0]
    assert "resumable" in start[0]

    progress = [message for message in messages if "backfill progress" in message]
    # 25 batches with N=10 -> after batch 10 and batch 20; never one line each.
    assert len(progress) == 2
    assert "rows=10 batches=10 cursor=10" in progress[0]
    assert "rows=20 batches=20 cursor=20" in progress[1]

    final = [message for message in messages if "backfilled outcome on" in message]
    assert len(final) == 1
    assert "25 row(s)" in final[0]
    assert "25 batch(es)" in final[0]


def test_the_loop_still_keysets_from_the_last_id(fake_backfill: _FakeConnection) -> None:
    """No behavioural change: each batch advances the cursor by the max id."""
    _MIGRATION._backfill_outcome()

    cursors = [params.get("last") for params in fake_backfill.params]
    assert cursors[0] == _MIGRATION._ZERO_UUID
    assert cursors[1] == 1
    assert cursors[2] == 2
    assert all(params.get("batch_size") == _MIGRATION._BATCH_SIZE for params in fake_backfill.params)


def test_the_side_connection_is_skipped_when_this_transaction_locks_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0328``'s CREATE INDEX holds SHARE, so a side ALTER self-deadlocks."""
    monkeypatch.setattr(_MIGRATION, "_relation_visible", lambda _conn: True)
    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_eval_results", lambda: True)
    assert _MIGRATION._side_connection_can_write_eval_results(object()) is False

    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_eval_results", lambda: False)
    assert _MIGRATION._side_connection_can_write_eval_results(object()) is True

    monkeypatch.setattr(_MIGRATION, "_relation_visible", lambda _conn: False)
    assert _MIGRATION._side_connection_can_write_eval_results(object()) is False


def test_the_backfill_runs_on_the_migration_connection_when_it_already_locks_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyset loop must not wait on the lock this transaction itself holds."""
    side = _FakeConnection([[(0,)]])
    migration = _FakeConnection([[(1,)], []])
    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", lambda: _FakeConnectionContext(side))
    monkeypatch.setattr(_MIGRATION, "_relation_visible", lambda _conn: True)
    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_eval_results", lambda: True)
    monkeypatch.setattr(_MIGRATION, "op", SimpleNamespace(get_bind=lambda: migration))

    assert _MIGRATION._backfill_outcome() == 1
    assert not side.params
    assert len(migration.params) == 2


def test_rehearsal_mode_reads_the_runner_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the rehearsal runner's ``ALEMBIC_REHEARSAL=1`` means rehearsal."""
    monkeypatch.delenv(_MIGRATION._REHEARSAL_ENV_VAR, raising=False)
    assert _MIGRATION._rehearsal_mode() is False

    monkeypatch.setenv(_MIGRATION._REHEARSAL_ENV_VAR, "1")
    assert _MIGRATION._rehearsal_mode() is True

    monkeypatch.setenv(_MIGRATION._REHEARSAL_ENV_VAR, "0")
    assert _MIGRATION._rehearsal_mode() is False


def test_a_rehearsal_never_opens_a_side_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rehearsal rolls back ONE transaction; a side commit would persist.

    ``env.py`` refuses any side checkout under ``ALEMBIC_REHEARSAL=1``, so both
    the DDL and the keyset backfill must stay on the migration connection.
    """
    migration = _FakeConnection([[(1,)], []])
    executed: list[str] = []

    def _refuse() -> None:
        raise AssertionError("a rehearsal must not open a side connection")

    monkeypatch.setenv(_MIGRATION._REHEARSAL_ENV_VAR, "1")
    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", _refuse)
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(get_bind=lambda: migration, execute=lambda s: executed.append(str(s))),
    )

    _MIGRATION._execute_side(("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS outcome VARCHAR(16)",))

    assert _MIGRATION._backfill_outcome() == 1
    assert len(executed) == 1
    assert len(migration.params) == 2


def test_a_fresh_chain_rehearsal_skips_the_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-rehearsal fresh chain: no side visibility, so nothing to backfill."""
    monkeypatch.delenv(_MIGRATION._REHEARSAL_ENV_VAR, raising=False)
    side = _FakeConnection([[(0,)]])

    def _refuse_get_bind() -> None:
        raise AssertionError("a fresh chain has nothing to backfill")

    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", lambda: _FakeConnectionContext(side))
    monkeypatch.setattr(_MIGRATION, "_relation_visible", lambda _conn: False)
    monkeypatch.setattr(_MIGRATION, "op", SimpleNamespace(get_bind=_refuse_get_bind))

    assert _MIGRATION._backfill_outcome() == 0
    assert not side.params
