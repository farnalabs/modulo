"""Scheduled/event-driven eval execution (FAR-377) — runner + guards.

Covers, without Docker (in-memory SQLite async engine + EvalEngine):

* the end-to-end runner: build a SuiteRun from a dataset + definitions, execute
  it (regex/human_set deterministic evals), persist per-case EvalResults with
  the FAR-382 ``eval_definition_version`` stamp, reconcile counts, and reach the
  terminal state;
* the artifact contract: empty dataset refuses loudly; a suite with no active
  definitions never silently passes;
* the separate spend pool (``suite_runs`` ledger, never ``runs``);
* the loop guard: eval/family exclusion + no Run/TriggerEvent write;
* the failure sink: an orchestration failure transitions the run to ``failed``
  with ``error_detail`` populated and escalates to the Error Dashboard.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.eval_engine.execute_suite_run import (
    EVAL_WATCH_EVENT_FAMILIES,
    SuiteRunEmptyDatasetError,
    SuiteRunExecutionError,
    _suite_run_transition,
    build_suite_run,
    build_suite_run_tuple,
    exclude_eval_families,
    execute_suite_run,
    is_eval_trigger,
    suite_run_daily_spend_exceeded,
    suite_run_daily_spend_used,
    suite_run_daily_spend_used_for_trigger,
)
from modulo.db.models import Base
from modulo.db.models.eval_dataset import EvalCase, EvalDataset
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.run import Run
from modulo.db.models.trigger_event import TriggerEvent


def _tables() -> list[Any]:
    return [
        EvalSuite.__table__,
        EvalDataset.__table__,
        EvalCase.__table__,
        EvalDefinition.__table__,
        SuiteRun.__table__,
        EvalResult.__table__,
        ModelBackend.__table__,
        Run.__table__,
        TriggerEvent.__table__,
    ]


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _org() -> uuid.UUID:
    return uuid.uuid4()


async def seed_suite(session, org_id: uuid.UUID) -> tuple[EvalSuite, EvalDataset, list[EvalDefinition], ModelBackend]:
    suite = EvalSuite(organisation_id=org_id, name="suite-a", eval_definition_ids=[])
    dataset = EvalDataset(organisation_id=org_id, name="ds-a", version=1)
    backend = ModelBackend(
        organisation_id=org_id,
        name="backend-a",
        display_name="Backend A",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"x",
        account_id=org_id,
    )
    session.add_all([suite, dataset, backend])
    await session.flush()

    regex_def = EvalDefinition(
        organisation_id=org_id,
        pipeline_id=uuid.uuid4(),
        name="category-regex",
        eval_type="regex",
        config_json={"pattern": "^billing$", "field": "output.category"},
        failure_behaviour="warn",
        eval_suite_id=suite.id,
        account_id=org_id,
    )
    session.add(regex_def)
    await session.flush()
    suite.eval_definition_ids = [str(regex_def.id)]
    return suite, dataset, [regex_def], backend


async def seed_cases(
    session, org_id: uuid.UUID, dataset: EvalDataset, payloads: list[dict[str, Any]]
) -> list[EvalCase]:
    from modulo.db.models.eval_dataset import compute_input_hash

    cases = []
    for payload in payloads:
        case = EvalCase(
            organisation_id=org_id,
            dataset_id=dataset.id,
            input_payload=payload,
            input_hash=compute_input_hash(payload),
        )
        cases.append(case)
    session.add_all(cases)
    await session.flush()
    return cases


# --------------------------------------------------------------------------- #
# Runner end-to-end                                                           #
# --------------------------------------------------------------------------- #
async def test_execute_suite_run_completes_and_persists_results(session) -> None:
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}, {"category": "sales"}])

    run = await build_suite_run(
        session,
        org_id=org,
        suite_id=suite.id,
        dataset_id=dataset.id,
        model_backend_id=backend.id,
        scenario_inputs=None,
    )
    assert run.state == SuiteRunState.PENDING.value

    from modulo.core.eval_engine.execute_suite_run import _suite_run_transition

    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    await session.flush()

    stats = await execute_suite_run(session, run, eval_definition_version=7)
    await session.flush()

    assert stats["state"] == SuiteRunState.COMPLETED.value
    assert stats["total_cases"] == 2
    assert stats["passed_cases"] == 1
    assert stats["failed_cases"] == 1
    assert stats["excluded_case_count"] == 0

    # The runner feeds the SEPARATE suite-run daily spend pool by mirroring the
    # row-locked ``claimed_cost`` ledger into ``total_cost_usd``. A deterministic
    # suite claims nothing, so it must be 0 (not NULL) — proving the column the
    # ``suite_run_daily_spend_used`` pool sums is actually written.
    assert run.total_cost_usd == Decimal(0)

    rows = list(
        (
            await session.execute(__import__("sqlalchemy").select(EvalResult).where(EvalResult.suite_run_id == run.id))
        ).scalars()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.suite_run_id == run.id
        assert row.eval_definition_version == 7
        assert row.organisation_id == org


async def test_execute_suite_run_rejects_empty_dataset(session) -> None:
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    # A dataset with zero active cases is refused LOUDLY at construction — never
    # a silent pass (artifact contract).
    with pytest.raises(SuiteRunEmptyDatasetError, match="no active cases"):
        await build_suite_run(
            session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )


async def test_failure_sink_transitions_run_to_failed(session) -> None:
    """An orchestration failure sinks the run to ``failed`` + surfaces detail."""
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}])
    # Add an llm_judge definition with NO judge callable -> execute refuses.
    llm_def = EvalDefinition(
        organisation_id=org,
        pipeline_id=uuid.uuid4(),
        name="judge",
        eval_type="llm_judge",
        config_json={"field": "output", "prompt": "grade"},
        failure_behaviour="warn",
        eval_suite_id=suite.id,
        account_id=org,
    )
    session.add(llm_def)
    await session.flush()

    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )
    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    with pytest.raises(SuiteRunExecutionError, match="llm_judge"):
        await execute_suite_run(session, run)
    assert run.state == SuiteRunState.FAILED.value
    assert "llm_judge" in (run.error_detail or "")


async def test_failure_sink_ingests_error_event(session, monkeypatch) -> None:
    """The monitored failure sink escalates to the Error Dashboard.

    Prove-the-fix: without ``_fail_run``'s ``ErrorIngestionService().ingest``
    call the error event is never surfaced — the run would be failed with
    ``error_detail`` set but no monitored event, so the Error Dashboard and the
    missed-run watchers would see nothing.
    """
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}])

    ingest = AsyncMock()

    class _FakeIngestionService:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.ingest = ingest

    monkeypatch.setattr("modulo.core.error_tracking.ErrorIngestionService", _FakeIngestionService)

    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )
    # No definitions -> orchestration error -> failure sink.
    for d in (await session.execute(__import__("sqlalchemy").select(EvalDefinition))).scalars():
        await session.delete(d)

    with pytest.raises(SuiteRunExecutionError):
        await execute_suite_run(session, run)
    assert run.state == SuiteRunState.FAILED.value
    ingest.assert_awaited_once()
    payload = ingest.await_args.args[2]
    assert payload["level"] == "error"
    assert payload["source"] == "suite_run"
    assert str(run.id) in payload["message"]
    assert payload["context_json"]["suite_run_id"] == str(run.id)


async def test_execute_suite_run_rejects_no_definitions(session) -> None:
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    case = EvalCase(organisation_id=org, dataset_id=dataset.id, input_payload={"a": 1}, input_hash="h" * 64)
    session.add(case)
    await session.flush()
    # Remove all definitions -> no active definitions.
    for d in (await session.execute(__import__("sqlalchemy").select(EvalDefinition))).scalars():
        await session.delete(d)
    await session.flush()

    # build_suite_run refuses a suite with no active definitions (never silent).
    with pytest.raises(SuiteRunExecutionError):
        await build_suite_run(
            session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )


# --------------------------------------------------------------------------- #
# Loop guard                                                                  #
# --------------------------------------------------------------------------- #
def test_is_eval_trigger_discriminator() -> None:
    class _Trigger:
        run_kind = "suite_run"
        eval_suite_id = uuid.uuid4()

    class _RunTrigger:
        run_kind = "run"
        eval_suite_id = None

    assert is_eval_trigger(_Trigger()) is True
    assert is_eval_trigger(_RunTrigger()) is False
    # A plain object without the columns (pre-migration) is a 'run' trigger.
    g = type("G", (), {"run_kind": "run", "eval_suite_id": None})()
    assert is_eval_trigger(g) is False
    s = type("S", (), {"run_kind": "suite_run", "eval_suite_id": None})()
    assert is_eval_trigger(s) is True


def test_exclude_eval_families_drops_eval_events() -> None:
    # Production watch set filtered through exclude_eval_families can never
    # re-fire a trigger from an eval/feedback write.
    watch = {"run_failed", "run_stalled", "eval_regression", "feedback", "suite_run"}
    assert exclude_eval_families(watch) == {"run_failed", "run_stalled"}
    assert {"eval_regression", "eval_blocked", "suite_run", "eval_result", "feedback"} <= EVAL_WATCH_EVENT_FAMILIES
    assert not exclude_eval_families(None)


async def test_suite_run_completion_writes_only_eval_results(session) -> None:
    """A SuiteRun execution persists EvalResult rows and nothing else.

    The ``runs`` and ``trigger_events`` surfaces are created alongside the eval
    tables here, so a loop-guard regression that wrote a ``Run`` or a
    ``TriggerEvent`` would be observable. Reaching ``completed`` with zero rows
    in BOTH proves the loop-guard: a finished eval writes ONLY into the eval
    surface, never into the webhook/trigger-watch source.
    """
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}])
    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )
    from modulo.core.eval_engine.execute_suite_run import _suite_run_transition

    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    stats = await execute_suite_run(session, run, eval_definition_version=1)
    await session.flush()
    assert stats["state"] == SuiteRunState.COMPLETED.value

    # Loop guard: a finished eval writes NO pipeline Run and NO TriggerEvent.
    runs = list((await session.execute(__import__("sqlalchemy").select(Run))).scalars())
    assert runs == []
    events = list((await session.execute(__import__("sqlalchemy").select(TriggerEvent))).scalars())
    assert events == []
    # But the per-case EvalResults ARE persisted.
    results = list((await session.execute(__import__("sqlalchemy").select(EvalResult))).scalars())
    assert len(results) == 1
    assert results[0].suite_run_id == run.id


# --------------------------------------------------------------------------- #
# Runner from ``pending`` (the SAQ execute_suite_run path)                    #
# --------------------------------------------------------------------------- #
async def test_execute_suite_run_from_pending_self_transitions_to_running(session) -> None:
    """The runner handles a run still in ``pending`` — the SAQ job path.

    ``fire_suite_run_trigger`` (cron_helpers) builds a ``pending`` SuiteRun and
    the SAQ ``execute_suite_run`` job hands it straight to this runner WITHOUT a
    prior ``pending -> running`` transition. The runner's own contract is
    "(pending -> terminal)", so it must self-transition a ``pending`` run to
    ``running`` before evaluation.

    Prove-the-fix: without this self-transition the runner calls
    ``_suite_run_transition(session, run, COMPLETED)`` while the run is still
    ``pending`` — an ILLEGAL edge (``pending`` only allows ``{running,
    cancelled}``) that raises ``IllegalStateTransitionError`` and leaves the run
    stuck ``pending`` forever, never surfaced as failed.
    """
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}])
    # build_suite_run leaves the run PENDING — exactly the fire_path state.
    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )
    assert run.state == SuiteRunState.PENDING.value
    # NO manual _suite_run_transition to RUNNING here — the runner must do it.
    stats = await execute_suite_run(session, run, eval_definition_version=1)
    await session.flush()
    assert stats["state"] == SuiteRunState.COMPLETED.value
    assert run.state == SuiteRunState.COMPLETED.value


# --------------------------------------------------------------------------- #
# Separate spend pool                                                         #
# --------------------------------------------------------------------------- #
def test_suite_run_spend_is_independent_of_production_pool() -> None:
    # The suite-run daily limit is enforced over suite_runs cost; a production
    # run at its limit does NOT gate a suite-run trigger and vice versa.
    assert suite_run_daily_spend_exceeded(Decimal("10.0"), Decimal("10.0")) is True
    assert suite_run_daily_spend_exceeded(Decimal("9.99"), Decimal("10.0")) is False
    assert suite_run_daily_spend_exceeded(Decimal(5), None) is False


async def test_suite_run_daily_spend_used_ignores_production_runs(session) -> None:
    """The suite-run spend pool SUMs ``suite_runs`` cost — never ``runs``.

    Prove-the-fix: if ``suite_run_daily_spend_used`` queried the production
    ``runs`` table (the pool production triggers enforce over), a run costing
    $100 would be counted against the suite-run limit. Here a production Run
    costs $100 and a SuiteRun costs $2; the suite-run today-pool must be $2.
    """
    org = await _org()
    prod_run = Run(
        organisation_id=org,
        pipeline_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        trigger_type="cron",
        run_number=1,
        input_hash="h" * 64,
        langgraph_thread_id=str(uuid.uuid4()),
        total_cost_usd=Decimal("100.00"),
    )
    suite_run = SuiteRun(
        organisation_id=org,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        definition_checksum="a" * 64,
        model_backend_id=uuid.uuid4(),
        state=SuiteRunState.COMPLETED.value,
        total_cost_usd=Decimal("2.00"),
    )
    session.add_all([prod_run, suite_run])
    await session.flush()

    used = await suite_run_daily_spend_used(session, org)
    assert used == Decimal("2.00")
    # A second org never sees the first's suite-run spend (org isolation).
    other = await suite_run_daily_spend_used(session, uuid.uuid4())
    assert other == Decimal(0)


async def test_suite_run_daily_spend_used_scoped_per_trigger(session) -> None:
    """The suite-run daily spend pool is PER TRIGGER, not per org.

    ``daily_spend_limit`` is a per-trigger knob. Two suite-run triggers in the
    same org at different limits must gate independently: a spend-hungry trigger
    must NOT consume a quieter trigger's pool. The owning trigger id is stamped
    onto ``suite_runs.extra`` at fire time, so the per-trigger pool sums only
    rows whose ``extra->>'trigger_id'`` matches (matching production's
    ``_spend_limit_skip`` over ``runs.trigger_id``).

    Prove-the-fix (FAR-377 reviewer major finding): the previous implementation
    summed the whole org's ``suite_runs`` cost, so the smallest limit gated
    every trigger and the configured per-trigger limits were meaningless.
    """
    org = await _org()
    trigger_a = uuid.uuid4()
    trigger_b = uuid.uuid4()
    suite_run_a = SuiteRun(
        organisation_id=org,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        definition_checksum="a" * 64,
        model_backend_id=uuid.uuid4(),
        state=SuiteRunState.COMPLETED.value,
        total_cost_usd=Decimal("40.00"),
        extra={"trigger_id": str(trigger_a)},
    )
    suite_run_b = SuiteRun(
        organisation_id=org,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        definition_checksum="b" * 64,
        model_backend_id=uuid.uuid4(),
        state=SuiteRunState.COMPLETED.value,
        total_cost_usd=Decimal("3.00"),
        extra={"trigger_id": str(trigger_b)},
    )
    session.add_all([suite_run_a, suite_run_b])
    await session.flush()

    used_a = await suite_run_daily_spend_used_for_trigger(session, org, trigger_a)
    used_b = await suite_run_daily_spend_used_for_trigger(session, org, trigger_b)
    assert used_a == Decimal("40.00")
    assert used_b == Decimal("3.00")
    # A trigger at limit does NOT pull from a different trigger's pool.
    assert used_a + used_b == Decimal("43.00")


# --------------------------------------------------------------------------- #
# Baseline tuple snapshot                                                     #
# --------------------------------------------------------------------------- #
def test_build_suite_run_tuple_snapshots_version() -> None:
    tup, checksum, sig = build_suite_run_tuple(
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_version=3,
        definition_snapshots=[{"id": str(uuid.uuid4()), "eval_type": "regex", "config_json": {"pattern": "x"}}],
        model_backend_id=uuid.uuid4(),
        scenario_inputs=None,
    )
    assert tup["dataset_version"] == 3
    assert sig is None
    assert len(checksum) == 64


# --------------------------------------------------------------------------- #
# Trigger dispatch (prove-the-fix)                                            #
# --------------------------------------------------------------------------- #
def _suite_run_trigger() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        run_kind="suite_run",
        eval_suite_id=uuid.uuid4(),
        active=True,
        max_concurrent_runs=1,
        daily_spend_limit=None,
        config_json={
            "dataset_id": uuid.uuid4(),
            "model_backend_id": uuid.uuid4(),
            "scenario_inputs": {"lang": "en"},
            "entity_thresholds": {"absolute_drop": 0.15},
            "suite_ceiling": "5.00",
            "eval_definition_version": 1,
        },
    )


def test_suite_run_execution_jobs_are_registered_on_runs_worker() -> None:
    """The dispatch wiring exists: the runs worker registers both suite jobs.

    Prove-the-fix: without the FAR-377 wiring, a ``suite_run`` trigger would
    enqueue ``execute_run`` (a pipeline Run). The runs worker must register the
    SuiteRun execution + per-item fire jobs so a dispatched SuiteRun actually
    runs.
    """
    from modulo.core.saq_worker import _runs_functions

    names = {n for n, _ in _runs_functions()}
    assert "modulo.core.saq_worker.execute_suite_run" in names
    assert "modulo.core.saq_worker.fire_suite_run_trigger" in names


# --------------------------------------------------------------------------- #
# Transaction-boundary failure persistence (FAR-377 Critical)                 #
# --------------------------------------------------------------------------- #
async def test_saq_job_persists_orchestration_failure_across_rollback(monkeypatch) -> None:
    """Prove-the-fix (Critical): an orchestration failure in the SAQ job must
    leave the SuiteRun ``failed`` -- NEVER stranded ``pending``.

    The runner (`execute_suite_run`) transitions the run to ``failed`` + writes
    ``error_detail`` + ingests the Error-Ingestion event, but it runs INSIDE the
    job's ``async with factory() as session, session.begin():`` block. When the
    runner re-raises, that context manager ROLLS BACK the transaction, silently
    discarding the ``failed`` transition + ``error_detail`` + event — the run
    reverts to ``pending`` and is stranded forever (never terminal, never
    surfaced). The job must re-persist the failure in its OWN committed
    transaction (``_persist_suite_run_execution_failure``) so it survives.

    Without the fix the final re-read (re-loading fresh) is ``pending``; with it,
    the run is ``failed`` with ``error_detail`` populated.
    """
    from datetime import UTC, datetime

    import modulo.core.saq_worker as sw

    org = await _org()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s, s.begin():
        suite, dataset, _, backend = await seed_suite(s, org)
        # Seed the run DIRECTLY (not via build_suite_run — it refuses a suite
        # with no active definitions): the job loads it and the runner must fail
        # on the empty definition set.
        run = SuiteRun(
            organisation_id=org,
            suite_id=suite.id,
            dataset_id=dataset.id,
            dataset_version=1,
            definition_checksum="a" * 64,
            model_backend_id=backend.id,
            baseline_tuple={},
            scenario_signature=None,
            state=SuiteRunState.PENDING.value,
            version=0,
            total_cases=0,
            passed_cases=0,
            failed_cases=0,
            excluded_case_count=0,
            claimed_cost=Decimal(0),
            created_at=datetime.now(UTC),
        )
        s.add(run)
        await s.flush()
        # Remove every definition so the runner raises "no active definitions".
        for d in (await s.execute(__import__("sqlalchemy").select(EvalDefinition))).scalars():
            await s.delete(d)
        await s.flush()
        rid, oid = str(run.id), str(org)

    class _FakeIngestionService:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def ingest(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr("modulo.core.error_tracking.ErrorIngestionService", _FakeIngestionService)
    monkeypatch.setattr(sw, "_make_session_factory", lambda: factory)

    with pytest.raises(SuiteRunExecutionError, match="no active eval definitions"):
        await sw.execute_suite_run({}, suite_run_id=rid, org_id=oid)

    async with factory() as s:
        fresh = await s.get(SuiteRun, uuid.UUID(rid))
        assert fresh is not None
        assert fresh.state == SuiteRunState.FAILED.value
        assert "no active eval definitions" in (fresh.error_detail or "")

    await engine.dispose()


async def test_saq_job_persists_raw_db_error_as_failed(monkeypatch) -> None:
    """Prove-the-fix: a RAW ``SQLAlchemyError`` (not ``SuiteRunExecutionError``)
    thrown inside the runner also terminalises the SuiteRun to ``failed``.

    Previously a raw DB error (IntegrityError/ProgrammingError/...) escaped the
    runner UNCAUGHT — the run was never terminalised and the error surfaced only
    via the after_process sink. The runner now maps it to a status code
    (ProgrammingError -> 501), routes it through ``_fail_run``, and re-raises a
    typed error; the SAQ job persists the failure in its OWN committed
    transaction. The     final re-read must be ``failed`` with the mapped code in
    ``error_detail``.
    """
    from datetime import UTC, datetime

    from sqlalchemy.exc import ProgrammingError

    import modulo.core.saq_worker as sw

    org = await _org()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s, s.begin():
        suite, dataset, _, backend = await seed_suite(s, org)
        run = SuiteRun(
            organisation_id=org,
            suite_id=suite.id,
            dataset_id=dataset.id,
            dataset_version=1,
            definition_checksum="a" * 64,
            model_backend_id=backend.id,
            baseline_tuple={},
            scenario_signature=None,
            state=SuiteRunState.PENDING.value,
            version=0,
            total_cases=0,
            passed_cases=0,
            failed_cases=0,
            excluded_case_count=0,
            claimed_cost=Decimal(0),
            created_at=datetime.now(UTC),
        )
        s.add(run)
        await s.flush()
        rid, oid = str(run.id), str(org)

    async def _raise_programming_error(*a: Any, **k: Any) -> None:
        raise ProgrammingError("SELECT ...", {}, Exception("boom"))

    monkeypatch.setattr("modulo.core.eval_engine.execute_suite_run.load_suite_definitions", _raise_programming_error)

    class _FakeIngestionService:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def ingest(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr("modulo.core.error_tracking.ErrorIngestionService", _FakeIngestionService)
    monkeypatch.setattr(sw, "_make_session_factory", lambda: factory)

    with pytest.raises(SuiteRunExecutionError, match="501"):
        await sw.execute_suite_run({}, suite_run_id=rid, org_id=oid)

    async with factory() as s:
        fresh = await s.get(SuiteRun, uuid.UUID(rid))
        assert fresh is not None
        assert fresh.state == SuiteRunState.FAILED.value
        assert "501" in (fresh.error_detail or "")

    await engine.dispose()
