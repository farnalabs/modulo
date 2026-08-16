"""create_run guardrail_summary telemetry seam (FAR-223 item 11) against
in-memory SQLite.

These tests exercise the REAL ``create_run`` path and assert that the
guardrail interception snapshot is persisted on the run row (``bound /
evaluated / passed / violated / observed / errored / redacted / skipped /
expected_skips / unexpected_skips``), that the
``evaluated + errored + skipped == bound`` invariant holds, and that an
EXPECTED (soft-deleted pin) skip never fires the unexpected-skip alert.
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import guardrails as guardrails_module
from modulo.core.guardrails import serialize_guardrail_pin
from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        Pipeline.__table__,
        Account.__table__,
        Team.__table__,
        Run.__table__,
        PipelineSnapshot.__table__,
        Journey.__table__,
        EvalDefinition.__table__,
        EvalResult.__table__,
        AuditEvent.__table__,
        AuditChainHead.__table__,
        EnvironmentProfile.__table__,
    ],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug="test-org"))
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    await session.flush()


async def _seed_guardrail(
    session: AsyncSession,
    *,
    name: str,
    action: str,
    config: dict[str, Any] | None = None,
) -> uuid.UUID:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    if config:
        cfg.update(config)
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=name,
        eval_type="guardrail",
        config_json=cfg,
        failure_behaviour="block" if action in ("block", "redact") else "warn",
        account_id=_ACCOUNT,
    )
    session.add(eval_def)
    await session.flush()
    return eval_def.id


async def _get_guardrail_row(session: AsyncSession, guardrail_id: uuid.UUID) -> EvalDefinition:
    return (await session.execute(select(EvalDefinition).where(EvalDefinition.id == guardrail_id))).scalar_one()


async def _seed_snapshot_with_pins(session: AsyncSession, *, guardrail_defs: list[EvalDefinition]) -> None:
    session.add(
        PipelineSnapshot(
            id=_SNAPSHOT,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=1,
            graph_json={"nodes": [], "edges": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            guardrail_pins_json=[serialize_guardrail_pin(d) for d in guardrail_defs],
            run_context_defaults={},
        )
    )
    await session.flush()


async def _create(
    session: AsyncSession,
    *,
    input_payload: dict[str, Any] | None = None,
    is_replay: bool | None = None,
) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload=input_payload or {},
        is_replay=is_replay,
    )


def _summary(run: Run) -> dict[str, int]:
    assert isinstance(run.guardrail_summary_json, dict)
    return cast(dict[str, int], run.guardrail_summary_json)


def _invariant(s: dict[str, int]) -> bool:
    return s["evaluated"] + s["errored"] + s["skipped"] == s["bound"]


# ---------------------------------------------------------------------------
# Summary persisted on the run row
# ---------------------------------------------------------------------------


async def test_create_run_persists_guardrail_summary_violation(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "eval_failed"
    s = _summary(run)
    assert s["bound"] == 1
    assert s["evaluated"] == 1
    assert s["passed"] == 0
    assert s["violated"] == 1  # regex matched = violation
    assert s["observed"] == 0
    assert s["errored"] == 0
    assert s["redacted"] == 0
    assert s["skipped"] == 0
    assert s["expected_skips"] == 0
    assert s["unexpected_skips"] == 0
    assert _invariant(s)


async def test_create_run_summary_passed_on_clean_payload(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "clean text"})
    assert run.status == "pending"
    s = _summary(run)
    assert s["evaluated"] == 1
    assert s["passed"] == 1  # regex no match = no violation
    assert s["violated"] == 0
    assert _invariant(s)


async def test_create_run_summary_observed_for_observe_mode(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="shadow", action="observe")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "pending"
    s = _summary(run)
    assert s["observed"] == 1
    assert s["violated"] == 1
    assert _invariant(s)


async def test_create_run_summary_redacted_count(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(
        session,
        name="redact-key",
        action="redact",
        config={"redaction": [{"path": "credentials.api_key", "mode": "transform"}]},
    )
    run = await _create(session, input_payload={"credentials": {"api_key": "sk-live-123"}, "body": "clean"})
    assert run.status == "pending"
    s = _summary(run)
    assert s["redacted"] == 1
    assert _invariant(s)


async def test_create_run_zero_guardrails_has_no_summary(session: AsyncSession):
    await _seed(session)
    run = await _create(session, input_payload={"body": "no guards bound"})
    assert run.status == "pending"
    assert run.guardrail_summary_json is None


async def test_create_run_cap_violation_errored_absorbs_bound(session: AsyncSession):
    """A cap violation blocks BEFORE any detection — errored absorbs all 9
    bound guardrails so the invariant holds."""
    await _seed(session)
    for i in range(9):
        await _seed_guardrail(session, name=f"g-{i}", action="observe")
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "eval_failed"
    s = _summary(run)
    assert s["bound"] == 9
    assert s["evaluated"] == 0
    assert s["errored"] == 9
    assert _invariant(s)


# ---------------------------------------------------------------------------
# Expected (soft-deleted pin) skip — never an unexpected-skip alert
# ---------------------------------------------------------------------------


async def test_create_run_expected_skip_no_unexpected_alert(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """A soft-deleted pinned guardrail is an EXPECTED skip (explained by pin
    state): the summary records skipped=1 / expected_skips=1 / unexpected_skips=0
    and the unexpected-skip alert is NOT fired."""
    unexpected_alerts: list[dict[str, Any]] = []

    async def _fake_unexpected(org_id: uuid.UUID, run_id: uuid.UUID, skip: Any) -> None:
        unexpected_alerts.append({"guardrail": skip.name, "reason": skip.reason})

    monkeypatch.setattr(guardrails_module, "alert_unexpected_guardrail_skip", _fake_unexpected)
    await _seed(session)
    pinned = await _seed_guardrail(session, name="ghost-block", action="block")
    pinned_row = await _get_guardrail_row(session, pinned)
    await _seed_snapshot_with_pins(session, guardrail_defs=[pinned_row])
    await session.execute(EvalDefinition.__table__.delete().where(EvalDefinition.__table__.c.id == pinned))
    await session.flush()

    run = await _create(session, input_payload={"body": "clean"}, is_replay=True)
    assert run.status == "pending"
    s = _summary(run)
    assert s["bound"] == 1
    assert s["evaluated"] == 0
    assert s["errored"] == 0
    assert s["skipped"] == 1
    assert s["expected_skips"] == 1
    assert s["unexpected_skips"] == 0
    assert _invariant(s)
    # Expected skip → no unexpected-skip alert.
    assert unexpected_alerts == []


async def test_create_run_emits_fired_signature_log(session: AsyncSession, caplog: pytest.LogCaptureFixture):
    """The per-pattern fired-signature regression log fires per clean detection."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    caplog.set_level(logging.INFO, logger="modulo.core.guardrails")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "eval_failed"
    records = [r for r in caplog.records if r.message == "guardrails.fired_signature"]
    assert len(records) == 1
    assert records[0].guardrail == "no-secrets"
    assert records[0].fired is True
    assert len(records[0].pattern_hash) == 12


async def test_create_run_summary_derive_failure_fails_open(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """A summary-derivation failure must NEVER break run creation — the run is
    created (enforcement intact) with no summary and the failure is logged."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("summary derivation exploded")

    monkeypatch.setattr(guardrails_module, "build_guardrail_summary", _boom)
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    # The enforcement outcome is unaffected — the run is still blocked.
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert run.guardrail_summary_json is None
