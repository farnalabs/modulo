"""Collector units (FAR-2305): DB-backed members, rendering, degraded seeding.

Every DB path runs against a fake session. The collectors are pure transforms
over what a session returns, so nothing here needs a live Postgres — the one
thing a mock cannot prove (that the SQL is accepted by Postgres) is listed in
the module docstring of :mod:`modulo.support.collect` and covered by the
integration suite, not here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import modulo.support.collect as collect_module
from modulo.support.collect import (
    MAX_ERROR_MESSAGE_CHARS,
    ROW_COUNT_TABLES,
    AsyncCollected,
    BundleSources,
    collapse_repeated_lines,
    collect_sources,
)


class _NullCM:
    """Async no-op context manager (savepoints on the fake session)."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Just enough AsyncSession surface for the collectors."""

    def __init__(
        self,
        *,
        bind: Any = None,
        execute: Any = None,
        scalar: Any = None,
    ) -> None:
        self._bind = bind if bind is not None else SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self._execute = execute if execute is not None else _FakeResult([])
        self._scalar = scalar
        self.executed: list[str] = []
        self.scalars: list[str] = []

    def get_bind(self) -> Any:
        return self._bind

    def begin(self) -> _NullCM:
        return _NullCM()

    def begin_nested(self) -> _NullCM:
        return _NullCM()

    async def execute(self, statement: Any) -> Any:
        self.executed.append(str(statement))
        if isinstance(self._execute, Exception):
            raise self._execute
        if callable(self._execute):
            return self._execute(statement)
        return self._execute

    async def scalar(self, statement: Any) -> Any:
        self.scalars.append(str(statement))
        if isinstance(self._scalar, Exception):
            raise self._scalar
        if callable(self._scalar):
            return self._scalar(statement)
        return self._scalar


@pytest.fixture
def db_session_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import ``modulo.db.session`` with the settings env it needs at import.

    The module builds its engine at import time from ``Settings``; CI supplies
    ``DATABASE_URL``/``SECRET_KEY`` at the workflow level, so a local run needs
    placeholders for the same two fields. Nothing here ever connects.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")
    monkeypatch.setenv("SECRET_KEY", "collect-unit-test-placeholder-secret-key")
    monkeypatch.setenv("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    import modulo.db.session as module

    return module


class _SessionCtx:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _SessionFactory:
    def __init__(self, session: Any) -> None:
        self._session = session

    def __call__(self) -> _SessionCtx:
        return _SessionCtx(self._session)


# ---------------------------------------------------------------------------
# _dialect
# ---------------------------------------------------------------------------


def test_dialect_reads_the_bind_name() -> None:
    session = _FakeSession(bind=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    assert asyncio.run(collect_module._dialect(session)) == "sqlite"


def test_dialect_awaits_an_async_bind() -> None:
    async def _bind() -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    class _AsyncBindSession(_FakeSession):
        def get_bind(self) -> Any:
            return _bind()

    assert asyncio.run(collect_module._dialect(_AsyncBindSession())) == "postgresql"


def test_dialect_is_unknown_without_a_bind() -> None:
    assert asyncio.run(collect_module._dialect(SimpleNamespace())) == "unknown"


def test_dialect_is_unknown_when_the_name_is_not_a_string() -> None:
    session = _FakeSession(bind=SimpleNamespace(dialect=SimpleNamespace(name=None)))
    assert asyncio.run(collect_module._dialect(session)) == "unknown"


# ---------------------------------------------------------------------------
# _applied_migration_heads / _expected_migration_heads
# ---------------------------------------------------------------------------


def test_applied_migration_heads_are_sorted() -> None:
    session = _FakeSession(execute=_FakeResult(["0346_b", "0346_a"]))
    assert asyncio.run(collect_module._applied_migration_heads(session)) == ["0346_a", "0346_b"]
    assert "alembic_version" in session.executed[0]


def test_applied_migration_heads_are_none_when_the_query_fails() -> None:
    session = _FakeSession(execute=RuntimeError("relation does not exist"))
    assert asyncio.run(collect_module._applied_migration_heads(session)) is None


def test_expected_migration_heads_reads_the_repo_chain() -> None:
    heads = collect_module._expected_migration_heads()
    assert heads
    assert all(isinstance(head, str) for head in heads)


def test_expected_migration_heads_is_none_when_alembic_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.db.health_checks as health_checks

    def _boom() -> Any:
        raise RuntimeError("no alembic.ini")

    monkeypatch.setattr(health_checks, "resolve_alembic_ini", _boom)
    assert collect_module._expected_migration_heads() is None


# ---------------------------------------------------------------------------
# _server_version / _row_counts / _collect_db
# ---------------------------------------------------------------------------


def test_server_version_uses_the_dialect_statement() -> None:
    session = _FakeSession(scalar="16.4")
    assert asyncio.run(collect_module._server_version(session, "postgresql")) == "16.4"
    assert "server_version" in session.scalars[0]


def test_server_version_uses_the_sqlite_statement() -> None:
    session = _FakeSession(scalar="3.45.0")
    assert asyncio.run(collect_module._server_version(session, "sqlite")) == "3.45.0"
    assert session.scalars[0] == "SELECT sqlite_version()"


def test_server_version_is_none_for_a_null_value() -> None:
    assert asyncio.run(collect_module._server_version(_FakeSession(scalar=None), "postgresql")) is None


def test_server_version_is_none_when_the_query_fails() -> None:
    session = _FakeSession(scalar=RuntimeError("no such function"))
    assert asyncio.run(collect_module._server_version(session, "postgresql")) is None


def test_row_counts_returns_every_table_and_none_for_a_failing_one() -> None:
    def _scalar(statement: Any) -> int:
        if "agents" in str(statement):
            raise RuntimeError("permission denied")
        return 7

    counts = asyncio.run(collect_module._row_counts(_FakeSession(scalar=_scalar)))
    assert set(counts) == set(ROW_COUNT_TABLES)
    assert counts["runs"] == 7
    assert counts["agents"] is None


def test_collect_db_assembles_the_inventory_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def _scalar(statement: Any) -> Any:
        if "server_version" in str(statement):
            return "16.4"
        return 3

    session = _FakeSession(execute=_FakeResult(["0346_head"]), scalar=_scalar)
    monkeypatch.setattr(collect_module, "_expected_migration_heads", lambda: ["0346_head"])
    collected = AsyncCollected()

    asyncio.run(collect_module._collect_db(collected, session))

    assert collected.database is not None
    assert collected.database["dialect"] == "postgresql"
    assert collected.database["migration_head"] == ["0346_head"]
    assert collected.database["expected_head"] == ["0346_head"]
    assert collected.database["server_version"] == "16.4"
    assert collected.database["row_counts"]["runs"] == 3


# ---------------------------------------------------------------------------
# _collect_error_groups / _collect_failed_runs
# ---------------------------------------------------------------------------


def _error_group(*, with_event: bool, group_id: int = 1) -> Any:
    event = (
        SimpleNamespace(
            message="boom " * 300,
            source="backend",
            environment="production",
            version="0.3.323",
        )
        if with_event
        else None
    )
    return SimpleNamespace(
        id=uuid.UUID(int=group_id),
        fingerprint=f"fp-{group_id}",
        level_peak="error",
        count=3,
        first_seen=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen=datetime(2026, 1, 2, tzinfo=UTC),
        sample_event=event,
    )


def test_collect_error_groups_sanitises_and_bounds_the_message() -> None:
    session = _FakeSession(execute=_FakeResult([_error_group(with_event=True)]))
    collected = AsyncCollected()

    asyncio.run(collect_module._collect_error_groups(collected, session))

    assert collected.error_groups is not None
    rendered = collected.error_groups[0]
    assert rendered["fingerprint"] == "fp-1"
    assert rendered["source"] == "backend"
    assert len(rendered["message"]) == MAX_ERROR_MESSAGE_CHARS
    assert rendered["omitted"] == ["context_json", "stacktrace"]
    assert rendered["first_seen"].startswith("2026-01-01")


def test_collect_error_groups_tolerates_a_group_without_a_sample_event() -> None:
    session = _FakeSession(execute=_FakeResult([_error_group(with_event=False)]))
    collected = AsyncCollected()

    asyncio.run(collect_module._collect_error_groups(collected, session))

    assert collected.error_groups is not None
    rendered = collected.error_groups[0]
    assert rendered["source"] is None
    assert not rendered["message"]
    assert rendered["environment"] is None


def _failed_run(*, run_id: int, timings: bool) -> Any:
    return SimpleNamespace(
        id=uuid.UUID(int=run_id),
        pipeline_id=uuid.UUID(int=run_id + 100),
        run_number=run_id,
        status="failed",
        error_code="harness.executor_failed",
        started_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC) if timings else None,
        completed_at=datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC) if timings else None,
        total_cost_usd=Decimal("0.25") if timings else None,
    )


def test_collect_failed_runs_reports_timings_and_omits_content() -> None:
    rows = [(_failed_run(run_id=1, timings=True), "nightly"), (_failed_run(run_id=2, timings=False), None)]
    collected = AsyncCollected()

    asyncio.run(collect_module._collect_failed_runs(collected, _FakeSession(execute=_FakeResult(rows))))

    assert collected.failed_runs is not None
    timed = collected.failed_runs[0]
    assert timed["pipeline_name"] == "nightly"
    assert timed["duration_seconds"] == 30.0
    assert timed["total_cost_usd"] == "0.25"
    assert "input_payload" in timed["omitted"]
    untimed = collected.failed_runs[1]
    assert untimed["duration_seconds"] is None
    assert untimed["total_cost_usd"] is None
    assert untimed["started_at"] is None


def test_collect_failed_runs_queries_only_failed_statuses() -> None:
    session = _FakeSession(execute=_FakeResult([]))
    asyncio.run(collect_module._collect_failed_runs(AsyncCollected(), session))
    assert "runs.status IN" in session.executed[0]


# ---------------------------------------------------------------------------
# _clock_drift / _coerce_datetime
# ---------------------------------------------------------------------------


def test_clock_drift_is_a_float_for_an_aware_timestamp() -> None:
    drift = asyncio.run(collect_module._clock_drift(_FakeSession(scalar=datetime.now(UTC))))
    assert drift is not None
    assert drift < 60.0


def test_clock_drift_is_none_when_the_query_fails() -> None:
    assert asyncio.run(collect_module._clock_drift(_FakeSession(scalar=RuntimeError("no db")))) is None


def test_clock_drift_is_none_for_a_null_timestamp() -> None:
    assert asyncio.run(collect_module._clock_drift(_FakeSession(scalar=None))) is None


def test_coerce_datetime_attaches_utc_to_a_naive_value() -> None:
    parsed = collect_module._coerce_datetime(datetime(2026, 1, 1, 12, 0))
    assert parsed == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_coerce_datetime_normalises_an_offset_to_utc() -> None:
    parsed = collect_module._coerce_datetime("2026-01-01T12:00:00+02:00")
    assert parsed == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def test_coerce_datetime_accepts_a_space_separated_string() -> None:
    parsed = collect_module._coerce_datetime("2026-01-01 12:00:00")
    assert parsed is not None
    assert parsed.hour == 12


def test_coerce_datetime_is_none_for_an_unparseable_string() -> None:
    assert collect_module._coerce_datetime("yesterday") is None


def test_coerce_datetime_is_none_for_a_non_datetime_value() -> None:
    assert collect_module._coerce_datetime(1767225600) is None


# ---------------------------------------------------------------------------
# Licence payload + rendering
# ---------------------------------------------------------------------------


def test_licence_payload_defaults_to_community_without_a_licence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.core.feature_flags as feature_flags

    monkeypatch.setattr(feature_flags, "resolve_effective_license", lambda *_args: None)

    payload = collect_module._licence_payload(SimpleNamespace(), None)

    assert payload["tier"] == "community"
    assert payload["state"] == "none"
    assert not payload["features"]
    assert payload["enabled_flags"]


def test_licence_payload_reports_a_resolved_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.feature_flags as feature_flags

    data = SimpleNamespace(
        tier="team",
        expires_at=datetime.now(UTC) + timedelta(days=365),
        licence_id="lic-1",
        renewal_mode="manual",
        features=["sso", "audit"],
    )
    monkeypatch.setattr(feature_flags, "resolve_effective_license", lambda *_args: data)

    payload = collect_module._licence_payload(SimpleNamespace(), None)

    assert payload["tier"] == "team"
    assert payload["state"] == "active"
    assert payload["licence_id"] == "lic-1"
    assert payload["features"] == ["audit", "sso"]


def test_licence_payload_falls_back_when_licence_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.core.feature_flags as feature_flags

    def _boom(*_args: Any) -> Any:
        raise RuntimeError("corrupt licence")

    monkeypatch.setattr(feature_flags, "resolve_effective_license", _boom)

    payload = collect_module._licence_payload(SimpleNamespace(), None)

    assert payload["tier"] == "community"
    assert payload["state"] == "none"


def test_licence_state_covers_every_window() -> None:
    from modulo.core.license import LICENCE_GRACE_DAYS

    now = datetime.now(UTC)
    assert collect_module._licence_state(None) == "none"
    assert collect_module._licence_state(SimpleNamespace(expires_at=None)) == "unknown"
    assert (
        collect_module._licence_state(SimpleNamespace(expires_at=now + timedelta(days=LICENCE_GRACE_DAYS + 10)))
        == "active"
    )
    assert collect_module._licence_state(SimpleNamespace(expires_at=now + timedelta(days=1))) == "renewal_due"
    assert collect_module._licence_state(SimpleNamespace(expires_at=now - timedelta(days=1))) == "grace"
    assert (
        collect_module._licence_state(SimpleNamespace(expires_at=now - timedelta(days=LICENCE_GRACE_DAYS + 10)))
        == "lapsed"
    )


def test_licence_text_never_prints_the_key() -> None:
    text = collect_module._licence_text(
        {
            "tier": "team",
            "state": "active",
            "expires_at": "2027-01-01",
            "licence_id": "lic-1",
            "renewal_mode": "manual",
            "features": ["sso"],
            "enabled_flags": ["eval_system"],
        }
    )
    assert "tier: team" in text
    assert "features: sso" in text
    assert "licence_key: never included" in text


def test_db_text_renders_unknown_when_a_field_is_absent() -> None:
    text = collect_module._db_text({"dialect": "postgresql", "migration_head": None, "row_counts": None})
    assert "migration_head: unknown" in text
    assert "server_version: unknown" in text
    assert "row_counts:" in text


def test_db_text_renders_unknown_for_a_table_with_no_count() -> None:
    text = collect_module._db_text(
        {
            "dialect": "postgresql",
            "migration_head": ["0346"],
            "expected_head": ["0346"],
            "server_version": "16.4",
            "row_counts": {"runs": 12, "pipelines": None},
        }
    )
    assert "migration_head: 0346" in text
    assert "  runs: 12" in text
    assert "  pipelines: unknown" in text


def test_cap_text_records_truncation_only_when_it_truncates() -> None:
    truncated: dict[str, str] = {}
    assert collect_module._cap_text("short", 100, "doctor.txt", truncated) == "short"
    assert not truncated

    capped = collect_module._cap_text("x" * 50, 10, "doctor.txt", truncated)
    assert capped == "x" * 10
    assert truncated["doctor.txt"] == "capped at 10 bytes"


def test_collapse_repeated_lines_keeps_a_single_line_untouched() -> None:
    assert collapse_repeated_lines("only line") == "only line"
    assert not collapse_repeated_lines("")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_url_password_extracts_the_inline_password() -> None:
    assert collect_module._url_password("postgresql://user:pw@host/db") == "pw"
    assert collect_module._url_password("postgresql://host/db") is None


def test_url_password_is_none_for_a_malformed_url() -> None:
    assert collect_module._url_password("http://[::1") is None


def test_seed_values_include_extra_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "env-seeded-password")
    seed = collect_module._seed_values(None, None, {"extra-secret-value": "<redacted>"})
    assert "env-seeded-password" in seed
    assert "extra-secret-value" in seed


def test_seed_values_record_a_degraded_layer_for_an_unreadable_secrets_file(tmp_path: Any) -> None:
    (tmp_path / "secrets.json").write_text("}not json{", encoding="utf-8")
    degraded: dict[str, str] = {}

    collect_module._seed_values(tmp_path, None, None, degraded)

    assert "seed_exact" in degraded
    assert "unreadable" in degraded["seed_exact"]


def test_seed_values_do_not_record_a_degraded_layer_without_a_secrets_file(tmp_path: Any) -> None:
    degraded: dict[str, str] = {}
    collect_module._seed_values(tmp_path, None, None, degraded)
    assert not degraded


# ---------------------------------------------------------------------------
# collect_sources / native member collection
# ---------------------------------------------------------------------------


def test_collect_sources_accepts_container_doctor_output() -> None:
    sources = collect_sources(install_shape="container", doctor_output="doctor: ok", collect_async=False)
    assert sources.members["doctor.txt"] == "doctor: ok"
    assert "degraded.json" in sources.missing


def test_collect_native_records_missing_doctor_output(tmp_path: Any) -> None:
    sources = BundleSources(install_shape="native")
    collect_module._collect_native(sources, tmp_path, None)
    assert "doctor.txt" in sources.missing
    assert "degraded.json" in sources.missing


def test_degraded_text_normalises_crashes() -> None:
    payload = json.loads(
        collect_module._degraded_text(
            {
                "degraded_at": 1767225600.0,
                "reason": "postgres exited",
                "crashes": [
                    "not-a-mapping",
                    {"child": "api", "exit_code": 2, "at": 1.0, "backtrace": 42},
                ],
            }
        )
    )
    assert payload["reason"] == "postgres exited"
    assert payload["crashes"] == [{"child": "api", "exit_code": 2, "at": 1.0, "backtrace": None}]


# ---------------------------------------------------------------------------
# Async collection orchestration
# ---------------------------------------------------------------------------


def test_all_async_missing_records_every_member() -> None:
    collected = collect_module._all_async_missing("disabled")
    assert set(collected.missing) == {"health.json", "db.txt", "errors.json", "runs.json"}
    assert "disabled" in collected.missing["db.txt"]


def test_run_async_collection_refuses_a_running_event_loop() -> None:
    async def _inside() -> AsyncCollected:
        return collect_module._run_async_collection()

    collected = asyncio.run(_inside())
    assert "called from a running event loop" in collected.missing["health.json"]


def test_run_async_collection_swallows_a_broken_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> AsyncCollected:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(collect_module, "collect_async_members", _boom)
    collected = collect_module._run_async_collection()
    assert "RuntimeError" in collected.missing["health.json"]


def test_mark_db_missing_leaves_health_alone() -> None:
    collected = AsyncCollected()
    collect_module._mark_db_missing(collected, RuntimeError("down"))
    assert "health.json" not in collected.missing
    assert "database unavailable (RuntimeError)" in collected.missing["db.txt"]


def test_guarded_records_a_member_failure_without_raising() -> None:
    async def _boom() -> None:
        raise RuntimeError("query failed")

    collected = AsyncCollected()
    asyncio.run(collect_module._guarded(_FakeSession(), collected, "db.txt", _boom))
    assert collected.missing["db.txt"] == "db.txt unavailable (RuntimeError)"


def test_collect_async_into_marks_db_members_missing_when_settings_fail(
    monkeypatch: pytest.MonkeyPatch,
    db_session_module: Any,
) -> None:
    import modulo.api.routes.health as health_module
    import modulo.settings as settings_module

    async def _no_readiness() -> Any:
        raise RuntimeError("health unavailable")

    def _no_settings() -> Any:
        raise RuntimeError("no settings")

    monkeypatch.setattr(health_module, "evaluate_readiness", _no_readiness)
    monkeypatch.setattr(settings_module, "get_settings", _no_settings)
    collected = AsyncCollected()

    asyncio.run(collect_module._collect_async_into(collected))

    assert "health.json" in collected.missing
    assert "db.txt" in collected.missing
    # The settings branch returns without resolving a licence; collect_sources
    # fills it from the env floor afterwards (covered by the collect_sources test).
    assert collected.licence is None


def test_collect_async_into_falls_back_to_the_env_licence_when_the_session_cannot_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    import modulo.api.routes.health as health_module

    async def _no_readiness() -> Any:
        raise RuntimeError("health unavailable")

    monkeypatch.setattr(health_module, "evaluate_readiness", _no_readiness)
    # A None entry makes `import modulo.db.session` raise ImportError, as a
    # broken/absent DB layer would.
    monkeypatch.setitem(sys.modules, "modulo.db.session", None)
    collected = AsyncCollected()

    asyncio.run(collect_module._collect_async_into(collected))

    assert "db.txt" in collected.missing
    assert collected.licence is not None
    assert collected.licence["tier"] == "community"


def test_collect_async_into_collects_every_member(
    monkeypatch: pytest.MonkeyPatch,
    db_session_module: Any,
) -> None:
    """One end-to-end pass over the real DB members with a fake session."""
    import modulo.api.routes.health as health_module
    import modulo.core.license_bootstrap as licence_bootstrap
    import modulo.db.rls as rls_module
    import modulo.settings as settings_module

    class _Check:
        status = "ok"

    class _Readiness:
        status = "ready"
        checks: ClassVar[dict[str, Any]] = {"database": _Check()}

    async def _readiness() -> Any:
        return _Readiness()

    def _execute(statement: Any) -> Any:
        rendered = str(statement)
        if "alembic_version" in rendered:
            return _FakeResult(["0346_head"])
        if "error_groups" in rendered:
            return _FakeResult([_error_group(with_event=True)])
        if "FROM runs" in rendered:
            return _FakeResult([(_failed_run(run_id=1, timings=True), "nightly")])
        return _FakeResult([])

    def _scalar(statement: Any) -> Any:
        rendered = str(statement)
        if "server_version" in rendered:
            return "16.4"
        if "CURRENT_TIMESTAMP" in rendered:
            return datetime.now(UTC)
        return 5

    session = _FakeSession(execute=_execute, scalar=_scalar)

    async def _resolve(_session: Any, _settings: Any) -> Any:
        return SimpleNamespace(id="org-1")

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(health_module, "evaluate_readiness", _readiness)
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(db_session_module, "AsyncSessionLocal", _SessionFactory(session))
    monkeypatch.setattr(rls_module, "set_rls_org", _noop)
    monkeypatch.setattr(rls_module, "set_rls_execution_context", _noop)
    monkeypatch.setattr(licence_bootstrap, "restore_persisted_license", _noop)
    monkeypatch.setattr(collect_module, "_resolve_organisation", _resolve)
    monkeypatch.setattr(collect_module, "_expected_migration_heads", lambda: ["0346_head"])
    monkeypatch.setattr(
        collect_module,
        "_licence_payload",
        lambda *_args: {"tier": "community", "state": "none", "features": [], "enabled_flags": []},
    )

    collected = AsyncCollected()
    asyncio.run(collect_module._collect_async_into(collected))

    assert not collected.missing
    assert collected.health is not None
    assert collected.health["checks"] == [{"name": "database", "status": "ok"}]
    assert collected.database is not None
    assert collected.database["server_version"] == "16.4"
    assert collected.error_groups is not None
    assert len(collected.error_groups) == 1
    assert collected.failed_runs is not None
    assert collected.failed_runs[0]["pipeline_name"] == "nightly"
    assert collected.clock_drift_seconds is not None
    assert collected.licence == {
        "tier": "community",
        "state": "none",
        "features": [],
        "enabled_flags": [],
    }


def test_collect_async_members_disposes_the_shared_engine(
    monkeypatch: pytest.MonkeyPatch,
    db_session_module: Any,
) -> None:
    disposed: list[bool] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    monkeypatch.setattr(db_session_module, "engine", _Engine())
    asyncio.run(collect_module.collect_async_members())
    assert disposed


def test_dispose_shared_engine_swallows_a_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    db_session_module: Any,
) -> None:
    class _Engine:
        async def dispose(self) -> None:
            raise RuntimeError("already closed")

    monkeypatch.setattr(db_session_module, "engine", _Engine())
    assert asyncio.run(collect_module._dispose_shared_engine()) is None


def test_restore_persisted_licence_swallows_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.license_bootstrap as licence_bootstrap

    async def _boom(_settings: Any) -> None:
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(licence_bootstrap, "restore_persisted_license", _boom)
    assert asyncio.run(collect_module._restore_persisted_licence(SimpleNamespace())) is None


# ---------------------------------------------------------------------------
# Remaining branches
# ---------------------------------------------------------------------------


def test_collect_sources_emits_every_async_member(monkeypatch: pytest.MonkeyPatch) -> None:
    collected = AsyncCollected(
        health={"status": "ok", "checks": []},
        database={
            "dialect": "postgresql",
            "migration_head": ["0346"],
            "expected_head": ["0346"],
            "server_version": "16.4",
            "row_counts": {"runs": 4},
        },
        error_groups=[{"id": "00000000-0000-0000-0000-000000000001"}],
        failed_runs=[{"run_id": "00000000-0000-0000-0000-000000000002"}],
        licence={"tier": "community", "state": "none", "features": [], "enabled_flags": []},
        clock_drift_seconds=0.5,
    )
    monkeypatch.setattr(collect_module, "_run_async_collection", lambda: collected)

    sources = collect_sources(install_shape="container")

    assert json.loads(sources.members["health.json"])["status"] == "ok"
    assert "server_version: 16.4" in sources.members["db.txt"]
    assert json.loads(sources.members["errors.json"])["error_groups"][0]["id"].endswith("1")
    assert json.loads(sources.members["runs.json"])["failed_runs"][0]["run_id"].endswith("2")
    assert "tier: community" in sources.members["licence.txt"]
    assert "clock drift: 0.500 s" in sources.members["version.txt"]


def test_deployment_text_reports_a_built_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_IMAGE", "registry.example/modulo:0.3.323")
    text = collect_module._deployment_text("container")
    assert "image_modulo_image: registry.example/modulo:0.3.323" in text


def test_degraded_text_tolerates_a_non_list_crash_field() -> None:
    payload = json.loads(
        collect_module._degraded_text({"degraded_at": 1767225600.0, "reason": "boom", "crashes": "nope"})
    )
    assert not payload["crashes"]
    assert payload["reason"] == "boom"


def test_collect_async_into_keeps_an_already_resolved_licence_when_rls_fails(
    monkeypatch: pytest.MonkeyPatch,
    db_session_module: Any,
) -> None:
    import modulo.api.routes.health as health_module
    import modulo.core.license_bootstrap as licence_bootstrap
    import modulo.db.rls as rls_module
    import modulo.settings as settings_module

    async def _no_readiness() -> Any:
        raise RuntimeError("health unavailable")

    async def _resolve(_session: Any, _settings: Any) -> Any:
        return SimpleNamespace(id="org-1")

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _rls_boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("set_config failed")

    monkeypatch.setattr(health_module, "evaluate_readiness", _no_readiness)
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(db_session_module, "AsyncSessionLocal", _SessionFactory(_FakeSession()))
    monkeypatch.setattr(rls_module, "set_rls_org", _noop)
    monkeypatch.setattr(rls_module, "set_rls_execution_context", _rls_boom)
    monkeypatch.setattr(licence_bootstrap, "restore_persisted_license", _noop)
    monkeypatch.setattr(collect_module, "_resolve_organisation", _resolve)
    monkeypatch.setattr(collect_module, "_licence_payload", lambda *_args: {"tier": "team"})

    collected = AsyncCollected()
    asyncio.run(collect_module._collect_async_into(collected))

    assert collected.licence == {"tier": "team"}
    assert "db.txt" in collected.missing
    assert "set_config failed" not in collected.missing["db.txt"]


def test_collect_async_into_keeps_a_resolved_licence_when_a_later_step_fails(
    monkeypatch: pytest.MonkeyPatch,
    db_session_module: Any,
) -> None:
    """The outer handler must not overwrite a licence it already resolved."""
    import modulo.api.routes.health as health_module
    import modulo.core.license_bootstrap as licence_bootstrap
    import modulo.db.rls as rls_module
    import modulo.settings as settings_module

    async def _no_readiness() -> Any:
        raise RuntimeError("health unavailable")

    async def _resolve(_session: Any, _settings: Any) -> Any:
        return SimpleNamespace(id="org-1")

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _drift_boom(_session: Any) -> None:
        raise RuntimeError("clock query failed")

    monkeypatch.setattr(health_module, "evaluate_readiness", _no_readiness)
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(db_session_module, "AsyncSessionLocal", _SessionFactory(_FakeSession()))
    monkeypatch.setattr(rls_module, "set_rls_org", _noop)
    monkeypatch.setattr(rls_module, "set_rls_execution_context", _noop)
    monkeypatch.setattr(licence_bootstrap, "restore_persisted_license", _noop)
    monkeypatch.setattr(collect_module, "_resolve_organisation", _resolve)
    monkeypatch.setattr(collect_module, "_licence_payload", lambda *_args: {"tier": "team"})
    monkeypatch.setattr(collect_module, "_clock_drift", _drift_boom)

    collected = AsyncCollected()
    asyncio.run(collect_module._collect_async_into(collected))

    assert collected.licence == {"tier": "team"}
    assert collected.database is not None
    assert "db.txt" in collected.missing


def test_resolve_organisation_returns_the_login_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.db.crud.organisation as organisation_crud

    organisation = SimpleNamespace(id="org-1")

    async def _resolve(_session: Any, _settings: Any) -> Any:
        return SimpleNamespace(id="user-1"), organisation

    monkeypatch.setattr(organisation_crud, "resolve_login_tenant", _resolve)

    resolved = asyncio.run(collect_module._resolve_organisation(_FakeSession(), SimpleNamespace()))
    assert resolved is organisation


def test_redaction_map_from_data_dir_returns_the_generated_secrets(tmp_path: Any) -> None:
    import json as json_module

    (tmp_path / "secrets.json").write_text(
        json_module.dumps(
            {
                "postgres_password": "pg-password-value",
                "redis_password": "redis-password-value",
                "state_hmac_key": "b2c4" * 16,
            }
        ),
        encoding="utf-8",
    )
    mapping = collect_module.redaction_map_from_data_dir(tmp_path)
    assert "pg-password-value" in mapping
    assert "redis-password-value" in mapping


def test_seed_values_include_settings_credentials() -> None:
    settings = SimpleNamespace(
        fernet_key="settings-fernet-key-value",
        secret_key="settings-secret-key-value",
        modulo_license_key="settings-licence-key-value",
        modulo_license_private_key="settings-licence-private-key-value",
        database_url="postgresql+asyncpg://user:settings-db-password@db:5432/modulo",
        redis_url="redis://:settings-redis-password@cache:6379/0",
    )
    seed = collect_module._seed_values(None, settings, None)
    for expected in (
        "settings-fernet-key-value",
        "settings-secret-key-value",
        "settings-licence-key-value",
        "settings-licence-private-key-value",
        "settings-db-password",
        "settings-redis-password",
    ):
        assert expected in seed
