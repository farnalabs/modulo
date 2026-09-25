"""Tests for EvalDefinition/EvalSuite versioning + EvalResult version snapshots (FAR-382).

Covers, without Docker (in-memory SQLite + pure functions):

* the new columns exist on the ORM models (``version`` / ``pre_version_raw`` on
  EvalDefinition and EvalSuite, ``eval_definition_version`` on EvalResult);
* the migration is reversible, non-null from cutover, and its chain head is
  correct;
* the NULL-version lookup: an unpinned version resolves to the definition's
  current (latest) version, while a pinned version is returned unchanged;
* the result-stamping seam attaches the scored version snapshot;
* the API helper stamps an update as a version-scoped event (bump + prior-config
  snapshot) and the serialiser surfaces ``version``.
"""

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from modulo.core.eval_engine.suite_run import (
    SuiteRunError,
    resolve_eval_definition_version,
)
from modulo.db.models import Base, EvalDefinition, EvalResult, EvalSuite
from modulo.db.models.eval_suite_run import SuiteRun

_MIGRATION_PATH = (
    Path(__file__).parents[4]
    / "backend"
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0138_eval_versioning.py"
)


def _make_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[EvalDefinition.__table__, EvalResult.__table__, EvalSuite.__table__, SuiteRun.__table__],
    )
    return Session(engine)


def _make_definition(org_id: uuid.UUID, *, version: int = 1, config: dict | None = None) -> EvalDefinition:
    return EvalDefinition(
        organisation_id=org_id,
        pipeline_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        name="eval",
        eval_type="regex",
        config_json=config or {"pattern": "a"},
        failure_behaviour="warn",
        version=version,
    )


# --------------------------------------------------------------------------- #
# Model surface                                                               #
# --------------------------------------------------------------------------- #
def test_eval_definition_version_columns_present() -> None:
    cols = EvalDefinition.__table__.columns
    assert "version" in cols
    assert "pre_version_raw" in cols


def test_eval_definition_version_defaults_to_one() -> None:
    col = EvalDefinition.__table__.c.version
    assert col.nullable is False
    assert col.default is not None
    assert col.default.arg == 1


def test_eval_suite_version_columns_present() -> None:
    cols = EvalSuite.__table__.columns
    assert "version" in cols
    assert "pre_version_raw" in cols


def test_eval_result_has_definition_version_snapshot() -> None:
    cols = EvalResult.__table__.columns
    assert "eval_definition_version" in cols
    assert cols["eval_definition_version"].nullable is True


# --------------------------------------------------------------------------- #
# Migration                                                                   #
# --------------------------------------------------------------------------- #
def test_migration_down_revision_points_at_0137() -> None:
    content = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0137_eval_suite_run"' in content


def test_migration_is_reversible() -> None:
    content = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'drop_column("eval_results", "eval_definition_version")' in content
    assert 'drop_column("eval_suites", "version")' in content
    assert 'drop_column("eval_definitions", "version")' in content


def test_migration_versions_are_non_null_from_cutover() -> None:
    content = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'server_default="1"' in content
    assert "nullable=False" in content


# --------------------------------------------------------------------------- #
# NULL-version lookup (latest-at-time)                                        #
# --------------------------------------------------------------------------- #
class _FakeScalarResult:
    def __init__(self, row) -> None:
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _async_session(row=None) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeScalarResult(row))
    return session


def test_resolve_returns_pinned_version_unchanged() -> None:
    session = _async_session()
    version = asyncio.run(resolve_eval_definition_version(session, uuid.uuid4(), uuid.uuid4(), pinned_version=7))
    assert version == 7
    session.execute.assert_not_called()


def test_resolve_unpinned_looks_up_latest_definition_version() -> None:
    org_id = uuid.uuid4()
    row = _make_definition(org_id, version=3)
    session = _async_session(row=row)
    version = asyncio.run(resolve_eval_definition_version(session, org_id, row.id))
    assert version == 3


def test_resolve_unpinned_raises_for_missing_definition() -> None:
    session = _async_session(row=None)
    with pytest.raises(SuiteRunError, match="not found while resolving version"):
        asyncio.run(resolve_eval_definition_version(session, uuid.uuid4(), uuid.uuid4()))


# --------------------------------------------------------------------------- #
# Redirect helper — update is a version-scoped event                          #
# --------------------------------------------------------------------------- #
def test_create_or_update_eval_bumps_and_snapshots_on_update() -> None:
    """Version stamping (FAR-382) now lives in ``create_or_update_eval``.

    An update (``existing_eval_id`` given) bumps ``version`` and snapshots the
    prior config into ``pre_version_raw``.
    """
    from modulo.core.eval_engine.eval_definition_write import create_or_update_eval
    from modulo.db.models.eval import Eval

    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    row_id = uuid.uuid4()
    row = Eval(
        id=row_id,
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        account_id=uuid.uuid4(),
        name="eval",
        eval_type="regex",
        config_json={"pattern": "old"},
        version=1,
    )

    class _Result:
        def scalar_one_or_none(self):
            return row

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result())

    updated = asyncio.run(
        create_or_update_eval(
            session,
            org_id=org_id,
            account_id=uuid.uuid4(),
            pipeline_id=pipeline_id,
            node_id=None,
            name="eval",
            eval_type="regex",
            config_json={"pattern": "new"},
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
            existing_eval_id=row_id,
        )
    )

    assert updated.version == 2
    assert updated.pre_version_raw == {"config_json": {"pattern": "old"}}


def test_eval_def_to_dict_surfaces_version() -> None:
    from modulo.api.routes.evals import _eval_def_to_dict

    session = _make_session()
    org_id = uuid.uuid4()
    row = _make_definition(org_id, version=5, config={"pattern": "x"})
    session.add(row)
    session.flush()

    payload = _eval_def_to_dict(row)
    assert payload["version"] == 5
    assert "pre_version_raw" in payload
    session.close()


# --------------------------------------------------------------------------- #
# Version snapshot actually persisted at a write site (FAR-382)               #
# --------------------------------------------------------------------------- #
async def test_gate_eval_persist_stamps_definition_version(monkeypatch) -> None:
    """The DB ``EvalResult`` row built by a write site must carry the definition's
    version snapshot. Regression guard for the FAR-382 write-site fix: without
    ``eval_definition_version=eval_def.version`` the captured row is NULL and
    ``resolve_eval_definition_version`` would resolve it to the definition's
    current/latest version, silently erasing the version timeline.

    Exercises ``eval_persist_order.run_evals_persist_before_decide`` with a
    capturing session factory and RLS helpers stubbed to no-ops.
    """
    from modulo.core.eval_engine import EvalDefinition as EvalDefDTO
    from modulo.core.eval_engine import EvalType
    from modulo.core.pipeline_engine import eval_persist_order

    async def _noop(session, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(eval_persist_order, "set_rls_org", _noop)
    monkeypatch.setattr(eval_persist_order, "set_rls_execution_context", _noop)

    captured: list[EvalResult] = []

    class _Begin:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return _Begin()

        def add(self, obj: EvalResult) -> None:
            captured.append(obj)

    class _Factory:
        def __call__(self):
            return _FakeSession()

    org_id = uuid.uuid4()
    # A real EvalDefinition DTO whose regex evaluation passes — the shared
    # helper computes via EvalEngine.evaluate_result, so a SimpleNamespace
    # eval_result is no longer valid input.
    eval_def = EvalDefDTO(
        id=uuid.uuid4(),
        org_id=org_id,
        name="eval",
        eval_type=EvalType.REGEX,
        config={"pattern": "a", "field": "text"},
        failure_behaviour="warn",
        version=9,
    )

    await eval_persist_order.run_evals_persist_before_decide(
        eval_defs=[eval_def],
        resolve_eval_target=lambda ed: {"text": "a"},
        run_id=uuid.uuid4(),
        org_id=org_id,
        session_factory=_Factory(),
    )

    assert captured, "write site must persist an EvalResult row"
    assert captured[0].eval_definition_version == 9
