"""FAR-2040: the 0328 backfill reports progress, so a long migration is not a hang.

A 10M-row ``eval_results`` table is thousands of batches; emitting only the
final total makes the release command look hung. The cadence is pinned here:
one start line, one progress line per N batches (never per batch), one final
line. The loop itself is exercised with a fake connection — no Postgres needed.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest

_MIGRATION = importlib.import_module("modulo.db.migrations.versions.0328_eval_verdict_contract")
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
