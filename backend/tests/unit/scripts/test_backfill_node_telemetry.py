"""Unit tests for the node-telemetry backfill + verify CLI tools (FAR-129 P5).

Covers the per-row split decision (lockstep columns), the whole-row skip rule
(never best-effort replace), idempotency predicates, dry-run vs --apply, the
date/limit filters, and the verify tool's legacy-pattern detection (::jsonb
cast) + exit codes. The DB is mocked with SimpleNamespace rows and a fake
connection that records every SQL statement, matching the analytics-facts unit
test style. Regression coverage pins the two reviewer findings: jsonb-adapted
(Jsonb-wrapped) UPDATE binds on Postgres and a per-batch commit so --apply
actually persists (plus rollback on DB error), including a real-driver SQLite
persistence test.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_script(name: str):
    script_path = PROJECT_ROOT / "scripts" / f"{name}.py"
    loader = SourceFileLoader(name, str(script_path))
    mod = module_from_spec(spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


backfill = _load_script("backfill_node_telemetry")
verify = _load_script("verify_backfill_node_telemetry")


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows) if rows is not None else []
        self._scalar = scalar

    def fetchall(self):
        return list(self._rows)

    def scalar_one(self):
        if self._scalar is not None:
            return self._scalar
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._scalar


class _FakeConn:
    """A connection that records SQL and serves canned rows / snapshot graphs."""

    def __init__(self):
        self.executed: list[tuple[str, dict]] = []
        self.runs = []
        self.graph_json: dict = {}
        self.total = 0
        # Default to the production dialect so --apply exercises the Jsonb
        # binding path; tests for other dialects override this.
        self.dialect = SimpleNamespace(name="postgresql")
        self.commits = 0
        self.rollbacks = 0
        self.raise_on_update = False

    def execute(self, stmt, params=None):
        params = dict(params or {})
        self.executed.append((str(stmt), params))
        s = str(stmt)
        if self.raise_on_update and s.startswith("UPDATE"):
            raise RuntimeError("boom")
        if s.startswith("SELECT count(*) FROM runs"):
            return _FakeResult(scalar=self.total)
        if "FROM pipeline_snapshots" in s:
            return _FakeResult(scalar=self.graph_json.get(params.get("sid")))
        if "FROM runs" in s:
            # Keyset pagination: only rows past the recorded after_id, capped
            # at the requested batch size.
            rows = self.runs
            after = params.get("after_id")
            if after is not None:
                rows = [r for r in rows if r.id > after]
            batch_size = params.get("batch_size")
            if batch_size is not None:
                rows = rows[:batch_size]
            return _FakeResult(rows=rows)
        return _FakeResult(rows=[])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeConnCM:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return _FakeConnCM(self._conn)

    def dispose(self):
        pass


def _sandbox_envelope() -> dict:
    """A realistic pre-split sandbox_agent envelope."""
    return {
        "artifacts": [
            {"output": {"output_json": {"answer": 42}, "status": "ok", "tokens": 3}},
        ],
        "output": {"exit_code": 0, "status": "complete"},
        "wall_clock_time_ms": 1234,
    }


def _run_row(run_id: str, outputs: dict | None, snapshot_id: str = "s1"):
    return SimpleNamespace(
        id=run_id,
        snapshot_id=snapshot_id,
        outputs_json=outputs,
        node_telemetry_json=None,
    )


# ---------------------------------------------------------------------------
# _split_row -- lockstep columns
# ---------------------------------------------------------------------------


def test_split_row_sandbox_agent_produces_lockstep_columns():
    ok, new_outputs, new_telemetry, reason = backfill._split_row(
        {"n1": _sandbox_envelope()}, {"n1": "sandbox_agent"}, "run-1"
    )
    assert ok is True
    assert reason is None
    assert new_outputs["n1"] == {"answer": 42}
    tel = new_telemetry["n1"]
    assert tel["status"] == "ok"
    assert tel["tokens"] == 3
    assert tel["exit_code"] == 0
    assert tel["wall_clock_time_ms"] == 1234
    assert "artifacts" not in tel


def test_split_row_regular_agent_returns_outer_output():
    ok, new_outputs, new_telemetry, reason = backfill._split_row(
        {"n1": {"output": {"text": "hello"}}}, {"n1": "agent"}, "run-1"
    )
    assert ok is True
    assert reason is None
    assert new_outputs["n1"] == {"text": "hello"}
    assert not new_telemetry["n1"]


def test_split_row_skipped_recovery_marker_is_telemetry_only():
    ok, new_outputs, new_telemetry, reason = backfill._split_row(
        {"n1": {"input": {"q": 1}, "output": {}, "skipped": True}}, {"n1": "agent"}, "run-1"
    )
    assert ok is True
    assert reason is None
    assert "n1" not in new_outputs
    assert new_telemetry["n1"] == {"skipped": True}


def test_split_row_skipped_flag_with_artifacts_keeps_return():
    """A top-level ``skipped`` key alongside an artifacts list is NOT a recovery
    marker: the node-type splitter folds ``skipped`` into telemetry AND returns
    the real return value. Mirror finalize._split_merge_outputs -- the return
    must be kept in outputs_json, never dropped (lossless).
    """
    envelope = {
        "artifacts": [
            {"output": {"output_json": {"answer": 42}, "status": "ok"}},
        ],
        "output": {"status": "complete"},
        "skipped": True,
    }
    ok, new_outputs, new_telemetry, reason = backfill._split_row({"n1": envelope}, {"n1": "sandbox_agent"}, "run-1")
    assert ok is True
    assert reason is None
    assert "n1" in new_outputs
    assert new_outputs["n1"] == {"answer": 42}
    assert new_telemetry["n1"]["skipped"] is True


def test_split_row_empty_outputs_splits_trivially():
    ok, new_outputs, new_telemetry, reason = backfill._split_row(None, None, "run-1")
    assert ok is True
    assert reason is None
    assert new_outputs is None
    assert new_telemetry == {}
    ok, new_outputs, new_telemetry, reason = backfill._split_row({}, None, "run-1")
    assert ok is True
    assert reason is None
    assert new_outputs == {}
    assert new_telemetry == {}


# ---------------------------------------------------------------------------
# _split_row -- whole-row skip (never best-effort replace)
# ---------------------------------------------------------------------------


def test_split_row_unknown_node_type_skips_whole_row():
    outputs = {
        "n1": _sandbox_envelope(),
        "mystery": {"output": {"x": 1}},
    }
    ok, new_outputs, new_telemetry, reason = backfill._split_row(outputs, {"n1": "sandbox_agent"}, "run-1")
    assert ok is False
    assert "mystery" in (reason or "")
    assert new_outputs == {}
    assert new_telemetry == {}


def test_split_row_malformed_node_value_skips_whole_row():
    outputs = {"n1": ["not", "a", "dict"]}
    ok, _, _, reason = backfill._split_row(outputs, {"n1": "agent"}, "run-1")
    assert ok is False
    assert "n1" in (reason or "")


def test_split_row_non_dict_outputs_skips_whole_row():
    ok, _, _, reason = backfill._split_row(["boom"], None, "run-1")
    assert ok is False
    assert "expected dict" in (reason or "")


def test_split_row_missing_snapshot_type_map_skips_whole_row():
    ok, _, _, reason = backfill._split_row({"n1": _sandbox_envelope()}, None, "run-1")
    assert ok is False
    assert "graph_json" in (reason or "")


# ---------------------------------------------------------------------------
# Backfill loop -- dry-run vs apply, batching, filters, skips
# ---------------------------------------------------------------------------


def test_loop_dry_run_does_not_write_and_samples():
    conn = _FakeConn()
    conn.runs = [_run_row("r1", {"n1": _sandbox_envelope()})]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    summary = backfill._run_backfill(conn, apply=False, batch_size=10, limit=None, since=None, until=None)
    assert summary["mode"] == "dry-run"
    assert summary["batches"] == 1
    assert summary["rows_processed"] == 1
    assert summary["rows_split"] == 1
    assert not summary["skips"]
    assert summary["sample"] == ["r1"]
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.executed)


def test_loop_apply_issues_conditional_parameterised_update():
    conn = _FakeConn()
    conn.runs = [_run_row("r1", {"n1": _sandbox_envelope()})]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    summary = backfill._run_backfill(conn, apply=True, batch_size=10, limit=None, since=None, until=None)
    assert summary["rows_split"] == 1
    updates = [item for item in conn.executed if item[0].startswith("UPDATE runs")]
    assert len(updates) == 1
    sql, params = updates[0]
    # Parameterised (no run id interpolated into the SQL text) + idempotent.
    assert "r1" not in sql
    assert ":new" in sql
    assert ":tel" in sql
    assert ":rid" in sql
    assert "node_telemetry_json IS NULL" in sql
    assert params["rid"] == "r1"
    # Postgres dialect -> the JSON dicts are Jsonb-wrapped at the bind site.
    assert isinstance(params["new"], Jsonb) and isinstance(params["tel"], Jsonb)
    assert params["new"].obj["n1"] == {"answer": 42}
    assert params["tel"].obj["n1"]["tokens"] == 3
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_loop_skips_unsplittable_row_whole_and_counts():
    conn = _FakeConn()
    conn.runs = [
        _run_row("good", {"n1": _sandbox_envelope()}),
        _run_row("bad", {"n1": _sandbox_envelope(), "mystery": {"output": {"x": 1}}}),
    ]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    summary = backfill._run_backfill(conn, apply=True, batch_size=10, limit=None, since=None, until=None)
    assert summary["rows_processed"] == 2
    assert summary["rows_split"] == 1
    assert len(summary["skips"]) == 1
    assert summary["skips"][0][0] == "bad"
    # The good row was updated; the bad row never was.
    updates = [item for item in conn.executed if item[0].startswith("UPDATE runs")]
    assert len(updates) == 1
    assert updates[0][1]["rid"] == "good"


def test_scan_and_update_predicates_exclude_existing_telemetry():
    """Idempotency is enforced by the WHERE predicates, not the script."""
    conn = _FakeConn()
    conn.runs = [_run_row("r1", {"n1": _sandbox_envelope()})]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    backfill._run_backfill(conn, apply=True, batch_size=10, limit=None, since=None, until=None)
    scans = [item for item in conn.executed if "FROM runs" in item[0] and not item[0].startswith("UPDATE")]
    updates = [item for item in conn.executed if item[0].startswith("UPDATE runs")]
    assert len(scans) == 1
    assert "WHERE node_telemetry_json IS NULL" in scans[0][0]
    assert len(updates) == 1
    assert "node_telemetry_json IS NULL" in updates[0][0]


def test_loop_limit_and_date_filters_reach_scan():
    conn = _FakeConn()
    conn.runs = []
    since = datetime(2026, 1, 1)
    until = datetime(2026, 1, 31, 23, 59, 59)
    backfill._run_backfill(conn, apply=False, batch_size=5, limit=3, since=since, until=until)
    scan = next(item for item in conn.executed if "FROM runs" in item[0])
    sql, params = scan
    assert "created_at >= :since" in sql
    assert "created_at <= :until" in sql
    assert "LIMIT :batch_size" in sql
    assert params["batch_size"] == 3
    assert params["since"] == since
    assert params["until"] == until


def test_loop_processes_multiple_batches_via_keyset():
    conn = _FakeConn()
    conn.runs = [
        _run_row("r1", {"n1": _sandbox_envelope()}),
        _run_row("r2", {"n1": _sandbox_envelope()}),
        _run_row("r3", {"n1": _sandbox_envelope()}),
    ]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    summary = backfill._run_backfill(conn, apply=True, batch_size=2, limit=None, since=None, until=None)
    assert summary["batches"] == 2
    assert summary["rows_split"] == 3
    updates = [item for item in conn.executed if item[0].startswith("UPDATE runs")]
    assert [params["rid"] for _, params in updates] == ["r1", "r2", "r3"]
    # Keyset: the second batch must page past the last id of the first.
    scans = [item for item in conn.executed if "FROM runs" in item[0] and not item[0].startswith("UPDATE")]
    assert len(scans) == 2
    assert "id > :after_id" in scans[1][0]
    assert scans[1][1]["after_id"] == "r2"


# ---------------------------------------------------------------------------
# Regression tests -- Jsonb binding + commit persistence (reviewer findings)
# ---------------------------------------------------------------------------


def test_apply_wraps_jsonb_params_on_postgres():
    """psycopg3 cannot adapt a raw dict; the UPDATE must receive Jsonb wraps."""
    conn = _FakeConn()
    conn.runs = [_run_row("r1", {"n1": _sandbox_envelope()})]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    backfill._run_backfill(conn, apply=True, batch_size=10, limit=None, since=None, until=None)
    updates = [item for item in conn.executed if item[0].startswith("UPDATE runs")]
    assert len(updates) == 1
    params = updates[0][1]
    assert isinstance(params["new"], Jsonb)
    assert isinstance(params["tel"], Jsonb)
    assert params["new"].obj["n1"] == {"answer": 42}
    assert params["tel"].obj["n1"]["tokens"] == 3


def test_apply_commits_after_each_batch():
    """engine.connect() autobegins a tx that rolls back on close -- commit per batch."""
    conn = _FakeConn()
    conn.runs = [
        _run_row("r1", {"n1": _sandbox_envelope()}),
        _run_row("r2", {"n1": _sandbox_envelope()}),
        _run_row("r3", {"n1": _sandbox_envelope()}),
    ]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    summary = backfill._run_backfill(conn, apply=True, batch_size=2, limit=None, since=None, until=None)
    assert summary["batches"] == 2
    assert conn.commits == 2
    assert conn.rollbacks == 0


def test_dry_run_never_commits():
    conn = _FakeConn()
    conn.runs = [_run_row("r1", {"n1": _sandbox_envelope()})]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    backfill._run_backfill(conn, apply=False, batch_size=10, limit=None, since=None, until=None)
    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_apply_rolls_back_on_db_error():
    """A DB failure must roll back the in-flight batch (no autocommit exists)."""
    conn = _FakeConn()
    conn.raise_on_update = True
    conn.runs = [_run_row("r1", {"n1": _sandbox_envelope()})]
    conn.graph_json = {"s1": {"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]}}
    engine = _FakeEngine(conn)
    with pytest.raises(RuntimeError, match="boom"):
        backfill._backfill_with_engine(engine, apply=True, batch_size=10, limit=None, since=None, until=None)
    assert conn.rollbacks == 1


def test_apply_persists_on_real_sqlite_engine(monkeypatch):
    """Strongest regression: --apply must actually persist through a real driver.

    Uses a real in-memory SQLite engine so the UPDATE + per-batch commit + the
    read-back are executed by the driver, not a mock. SQLite returns the stored
    JSON as TEXT (not a dict) for raw ``text()`` scans, so the scan and the
    snapshot type-map are stubbed to feed dicts exactly as Postgres' jsonb
    columns do -- the persistence path under test (bind -> commit -> visible to
    another connection) stays fully real.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE pipeline_snapshots (id VARCHAR(36) PRIMARY KEY, graph_json JSON)"))
        conn.execute(
            text(
                "CREATE TABLE runs (id VARCHAR(36) PRIMARY KEY, snapshot_id VARCHAR(36), "
                "created_at DATETIME, outputs_json JSON, node_telemetry_json JSON)"
            )
        )
        conn.execute(
            text("INSERT INTO pipeline_snapshots (id, graph_json) VALUES (:id, :g)"),
            {"id": "s1", "g": json.dumps({"nodes": [{"id": "n1", "node_type": "sandbox_agent"}]})},
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, snapshot_id, created_at, outputs_json, node_telemetry_json) "
                "VALUES (:id, :sid, :c, :o, NULL)"
            ),
            {
                "id": "r1",
                "sid": "s1",
                # ISO string, not a datetime: sqlite3's datetime adapter raises a
                # DeprecationWarning under Python 3.12, which pytest escalates.
                "c": "2026-08-11T00:00:00Z",
                "o": json.dumps({"n1": _sandbox_envelope()}),
            },
        )

    def _scan(conn, *, after_id, batch_size, since, until):
        # Emulate the real predicate + keyset: the first batch consumes the
        # legacy row, so no further batch ever returns it.
        if after_id is not None:
            return []
        return [_run_row("r1", {"n1": _sandbox_envelope()})]

    monkeypatch.setattr(backfill, "_scan_batch", _scan)
    monkeypatch.setattr(backfill, "_node_type_map", lambda conn, cache, snapshot_id: {"n1": "sandbox_agent"})

    with engine.connect() as conn:
        summary = backfill._run_backfill(conn, apply=True, batch_size=1, limit=None, since=None, until=None)
        assert summary["rows_split"] == 1

    # A separate connection must see the committed write (the missing-commit
    # bug left it NULL here).
    with engine.connect() as conn:
        tel = conn.execute(text("SELECT node_telemetry_json FROM runs WHERE id = 'r1'")).scalar_one_or_none()
        assert tel is not None
        assert json.loads(tel)["n1"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Verify tool
# ---------------------------------------------------------------------------


def test_legacy_sql_uses_jsonb_cast_for_json_comparisons():
    sql = str(verify._LEGACY_SQL)
    assert "outputs_json::jsonb" in sql
    assert "jsonb_each" in sql
    assert "jsonb_typeof" in sql
    assert "node_telemetry_json IS NULL" in sql


def test_verify_zero_legacy_returns_zero():
    conn = _FakeConn()
    conn.total = 10
    conn.runs = []
    buf = io.StringIO()
    assert verify._run_verify(conn, out=buf) == 0
    assert "total runs: 10" in buf.getvalue()
    assert "legacy rows remaining: 0" in buf.getvalue()


def test_verify_legacy_found_returns_one_and_lists_ids():
    conn = _FakeConn()
    conn.total = 10
    conn.runs = [
        SimpleNamespace(id="r1", reason="envelope-pattern"),
        SimpleNamespace(id="r2", reason="NULL both"),
    ]
    buf = io.StringIO()
    assert verify._run_verify(conn, out=buf) == 1
    text = buf.getvalue()
    assert "legacy rows remaining: 2" in text
    assert "envelope-pattern: 1" in text
    assert "NULL both: 1" in text
    assert "r1" in text
    assert "r2" in text
