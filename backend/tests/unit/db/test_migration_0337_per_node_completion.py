"""Tests for the FAR-2050 per-node completion migration."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

_MIGRATION = importlib.import_module("modulo.db.migrations.versions.0337_per_node_completion")


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakeConnection:
    def __init__(self, relation_visible: bool) -> None:
        self.relation_visible = relation_visible
        self.executed: list[str] = []

    def execute(self, statement: Any, _params: Any = None) -> _FakeResult:
        sql = str(statement)
        self.executed.append(sql)
        if "FROM pg_locks" in sql:
            return _FakeResult(0)
        if "to_regclass('public.run_node_outputs')" in sql:
            return _FakeResult("run_node_outputs" if self.relation_visible else None)
        return _FakeResult(None)


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, *_args: object) -> bool:
        return False


def test_migration_metadata_and_column_contract() -> None:
    assert _MIGRATION.revision == "0337_per_node_completion"
    assert _MIGRATION.down_revision == "0335_eval_verdict_contract_stage2"
    assert "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE" in _MIGRATION._ADD_COMPLETED_AT


def test_fresh_chain_uses_migration_connection_when_side_connection_cannot_see_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_connection = _FakeConnection(relation_visible=True)
    side_connection = _FakeConnection(relation_visible=False)
    migration_statements: list[str] = []

    monkeypatch.delenv(_MIGRATION._REHEARSAL_ENV_VAR, raising=False)
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: migration_connection,
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    monkeypatch.setattr(
        _MIGRATION,
        "_autocommit_connection",
        lambda: _ConnectionContext(side_connection),
    )
    monkeypatch.setattr(_MIGRATION, "_is_postgres", lambda: True)

    _MIGRATION.upgrade()

    assert side_connection.executed
    assert migration_statements == [_MIGRATION._ADD_COMPLETED_AT]
    assert not any("ADD COLUMN" in statement for statement in side_connection.executed)


def test_existing_install_uses_visible_side_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_connection = _FakeConnection(relation_visible=True)
    side_connection = _FakeConnection(relation_visible=True)
    migration_statements: list[str] = []

    monkeypatch.delenv(_MIGRATION._REHEARSAL_ENV_VAR, raising=False)
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: migration_connection,
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    monkeypatch.setattr(
        _MIGRATION,
        "_autocommit_connection",
        lambda: _ConnectionContext(side_connection),
    )
    monkeypatch.setattr(_MIGRATION, "_is_postgres", lambda: True)
    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_table", lambda: False)

    _MIGRATION.upgrade()

    assert not migration_statements
    assert any("ADD COLUMN" in statement for statement in side_connection.executed)


def test_non_postgres_upgrade_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    migration_statements: list[str] = []
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    _MIGRATION.upgrade()
    assert not migration_statements


def test_existing_install_downgrade_uses_visible_side_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_connection = _FakeConnection(relation_visible=True)
    side_connection = _FakeConnection(relation_visible=True)
    migration_statements: list[str] = []
    monkeypatch.delenv(_MIGRATION._REHEARSAL_ENV_VAR, raising=False)
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: migration_connection,
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", lambda: _ConnectionContext(side_connection))
    monkeypatch.setattr(_MIGRATION, "_is_postgres", lambda: True)
    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_table", lambda: False)

    _MIGRATION.downgrade()

    assert not migration_statements
    assert any(statement == _MIGRATION._DROP_COMPLETED_AT for statement in side_connection.executed)


def test_downgrade_rehearsal_stays_on_migration_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    migration_connection = _FakeConnection(relation_visible=True)
    migration_statements: list[str] = []
    monkeypatch.setenv(_MIGRATION._REHEARSAL_ENV_VAR, "1")
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: migration_connection,
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", lambda: pytest.fail("rehearsal opened side connection"))
    monkeypatch.setattr(_MIGRATION, "_is_postgres", lambda: True)

    _MIGRATION.downgrade()

    assert migration_statements == [_MIGRATION._DROP_COMPLETED_AT]


def test_downgrade_falls_back_when_side_connection_cannot_see_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_connection = _FakeConnection(relation_visible=True)
    side_connection = _FakeConnection(relation_visible=False)
    migration_statements: list[str] = []
    monkeypatch.delenv(_MIGRATION._REHEARSAL_ENV_VAR, raising=False)
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: migration_connection,
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    monkeypatch.setattr(_MIGRATION, "_autocommit_connection", lambda: _ConnectionContext(side_connection))
    monkeypatch.setattr(_MIGRATION, "_is_postgres", lambda: True)
    monkeypatch.setattr(_MIGRATION, "_migration_transaction_locks_table", lambda: False)

    _MIGRATION.downgrade()

    assert migration_statements == [_MIGRATION._DROP_COMPLETED_AT]
    assert not any(statement == _MIGRATION._DROP_COMPLETED_AT for statement in side_connection.executed)


def test_rehearsal_stays_on_migration_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_connection = _FakeConnection(relation_visible=True)
    migration_statements: list[str] = []

    monkeypatch.setenv(_MIGRATION._REHEARSAL_ENV_VAR, "1")
    monkeypatch.setattr(
        _MIGRATION,
        "op",
        SimpleNamespace(
            get_bind=lambda: migration_connection,
            execute=lambda statement: migration_statements.append(str(statement)),
        ),
    )
    monkeypatch.setattr(
        _MIGRATION,
        "_autocommit_connection",
        lambda: pytest.fail("rehearsal must not open a side connection"),
    )
    monkeypatch.setattr(_MIGRATION, "_is_postgres", lambda: True)

    _MIGRATION.upgrade()

    assert migration_statements == [_MIGRATION._ADD_COMPLETED_AT]
