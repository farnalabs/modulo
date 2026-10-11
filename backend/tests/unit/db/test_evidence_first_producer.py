"""The evidence store's first production producer (FAR-1293).

Proves the append-only ``evidence`` store is no longer dormant: the
guardrail/eval persist seam inside the REAL ``create_run`` path appends one
fact per guardrail eval decision through ``append_evidence`` — the store's
single write path, which runs ``assert_write_authorisation`` before any row
is added.

- **(a) the write lands, namespaced** — a real ``create_run`` with a bound
  guardrail produces an ``Evidence`` row in the ``eval`` producer's owned
  ``eval_`` namespace (key ``eval_guardrail_<eval_id>``, subject ``run``,
  value = the eval decision), and it reads back through the production
  ``fetch()`` round-trip.
- **(b) unowned namespace refused, FAIL-CLOSED** — patching the ownership map
  so ``eval`` owns nothing makes ``create_run`` raise
  ``EvidenceWriteAuthorisationError`` and land zero rows (no evidence, no
  eval result, no run); the write helper itself refuses cross-namespace and
  unknown-producer writes without adding anything to the session.
- **(c) a standing producer, not a one-off** — a clean payload through the
  same seam also writes its decision fact, while a run with NO guardrails
  writes no evidence at all.

No mocks on the write path: in-memory SQLite (aiosqlite) with the real ORM
rows throughout, and every assertion reads back from the database.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.eval_engine.evidence_layer import (
    KEY_NAMESPACE_OWNERSHIP,
    EvidenceWriteAuthorisationError,
    append_evidence,
    assert_write_authorisation,
    fetch,
)
from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.eval import Eval
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.evidence import Evidence
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput
from modulo.db.models.team import Team

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")

_VIOLATION_PAYLOAD = {"body": "leak SECRET_ABC12345"}
_CLEAN_PAYLOAD = {"body": "clean text"}

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
        Eval.__table__,
        AuditEvent.__table__,
        AuditChainHead.__table__,
        EnvironmentProfile.__table__,
        RunNodeOutput.__table__,
        Evidence.__table__,
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
            guardrail_pins_json=None,
            run_context_defaults={},
        )
    )
    await session.flush()


async def _seed_guardrail(session: AsyncSession, *, name: str, action: str) -> uuid.UUID:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    guardrail_id = uuid.uuid4()
    session.add(
        EvalDefinition(
            id=guardrail_id,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            node_id=None,
            name=name,
            eval_type="guardrail",
            config_json=cfg,
            failure_behaviour="block",
            account_id=_ACCOUNT,
        )
    )
    session.add(
        Eval(
            id=guardrail_id,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            node_id=None,
            name=name,
            eval_type="guardrail",
            config_json=cfg,
            account_id=_ACCOUNT,
        )
    )
    await session.flush()
    return guardrail_id


async def _create(session: AsyncSession, *, input_payload: dict[str, Any]) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload=input_payload,
    )


async def _all_evidence(session: AsyncSession) -> list[Evidence]:
    result = await session.execute(select(Evidence).where(Evidence.organisation_id == _ORG))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# (a) — the write lands with a correctly-namespaced key, on the real seam
# ---------------------------------------------------------------------------


async def test_first_production_write_lands_with_namespaced_key(session: AsyncSession) -> None:
    """A real ``create_run`` with a bound guardrail lands one evidence fact.

    This is the dormancy test in (a)+(c): the row is produced by the
    PRODUCTION call site (``db/crud/run.py::_persist_guardrail_eval_results``
    invoked from ``create_run``), so deleting that wiring fails this test —
    a helper-only test could not catch it.
    """
    await _seed(session)
    guardrail_id = await _seed_guardrail(session, name="no-secrets", action="block")

    run = await _create(session, input_payload=_VIOLATION_PAYLOAD)
    await session.flush()

    facts = await _all_evidence(session)
    assert len(facts) == 1
    fact = facts[0]

    # Key namespace: the ``eval`` producer owns ``eval_`` — the key is built
    # from the eval definition id and authorised against the real map.
    assert fact.key == f"eval_guardrail_{guardrail_id}"
    assert fact.key.startswith("eval_")
    assert_write_authorisation(fact.producer_type, fact.key)

    # Producer + subject + value shape the fact the way the seam documents.
    assert fact.producer_type == "eval"
    assert fact.producer_id == guardrail_id
    assert fact.subject_type == "run"
    assert fact.subject_id == str(run.id)
    assert fact.value is True  # regex matched = violation detected
    assert fact.organisation_id == _ORG

    # Round-trip through the production READ path: the store is live in both
    # directions, not write-only telemetry.
    fetched = await fetch("run", str(run.id), _ORG, session)
    assert len(fetched) == 1
    assert fetched[0].key == fact.key


# ---------------------------------------------------------------------------
# (b) — an unowned namespace is refused, fail-closed, at the production seam
# ---------------------------------------------------------------------------


async def test_unowned_namespace_refused_fail_closed_at_production_seam(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership is enforced INSIDE ``create_run`` — a refusal lands nothing.

    Patching ``KEY_NAMESPACE_OWNERSHIP`` so the ``eval`` producer owns no
    namespace proves the production path routes through
    ``assert_write_authorisation`` (a write that bypassed it would succeed
    regardless of the map). Fail-closed: the refusal aborts the run's
    transaction, so no evidence row, no eval result and no run survive.
    """
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    monkeypatch.setitem(KEY_NAMESPACE_OWNERSHIP, "eval", set())

    with pytest.raises(EvidenceWriteAuthorisationError):
        await _create(session, input_payload=_VIOLATION_PAYLOAD)
    await session.rollback()

    assert not await _all_evidence(session)
    eval_results = (await session.execute(select(EvalResult))).scalars().all()
    assert not eval_results
    runs = (await session.execute(select(Run))).scalars().all()
    assert not runs


async def test_append_evidence_refuses_unowned_namespace_without_landing(session: AsyncSession) -> None:
    """The write helper itself fails closed for cross-namespace and unknown producers."""
    subject_id = str(uuid.uuid4())
    forbidden: list[tuple[str, str]] = [
        ("eval", "node_has_work"),  # ``node_`` belongs to the ``run`` producer
        ("run", "eval_guardrail_00000000-0000-0000-0000-000000000000"),  # ``eval_`` belongs to ``eval``
        ("system_state", "eval_passed"),  # ``eval_`` is not a system_state namespace
        ("intruder", "eval_passed"),  # unknown producer type
    ]
    for producer_type, key in forbidden:
        with pytest.raises(EvidenceWriteAuthorisationError):
            append_evidence(
                session,
                organisation_id=_ORG,
                key=key,
                subject_type="run",
                subject_id=subject_id,
                value=True,
                producer_type=producer_type,
            )

    # Nothing was added: a refused write never reaches the session.
    assert not session.new
    assert not await _all_evidence(session)


# ---------------------------------------------------------------------------
# (c) — a standing producer: every guardrail decision writes, nothing else does
# ---------------------------------------------------------------------------


async def test_clean_guardrail_decision_is_also_written(session: AsyncSession) -> None:
    """The writer is wired for every decision, not only the block outcome."""
    await _seed(session)
    guardrail_id = await _seed_guardrail(session, name="no-secrets", action="block")

    run = await _create(session, input_payload=_CLEAN_PAYLOAD)
    await session.flush()

    facts = await _all_evidence(session)
    assert len(facts) == 1
    assert facts[0].key == f"eval_guardrail_{guardrail_id}"
    assert facts[0].value is False  # no violation detected
    assert facts[0].subject_id == str(run.id)


async def test_run_without_guardrails_writes_no_evidence(session: AsyncSession) -> None:
    """Bounded producer: a run with no bound guardrails writes no evidence."""
    await _seed(session)

    await _create(session, input_payload=_CLEAN_PAYLOAD)
    await session.flush()

    assert not await _all_evidence(session)
