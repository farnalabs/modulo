"""Suite-run LLM-judge wiring (FAR-1712).

Covers, without Docker (in-memory SQLite async engine + a stub judge backend):

* the org-scoped hub builder: no active backend -> ``None``; active rows ->
  an initialised hub; a cross-org active backend is never resolvable;
* the per-definition dispatch callable: missing / malformed
  ``model_backend_id`` fails THAT eval closed; two definitions with different
  backends each resolve their OWN backend;
* the SAQ ``execute_suite_run`` job: a judge-bearing suite runs end-to-end,
  the judge is actually invoked, the hub is torn down; a judge suite with no
  active backend still surfaces the runner's loud refusal (never a silent
  pass); and a suite with no judge definition builds no hub.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.eval_engine.execute_suite_run import (
    SuiteRunExecutionError,
    build_suite_run,
)
from modulo.core.eval_engine.suite_run_judge import (
    build_org_model_backend_hub,
    build_suite_run_llm_judge_callable,
)
from modulo.core.model_backend_hub import ModelBackendHub
from modulo.db.models import Base
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.eval import Eval
from modulo.db.models.eval_dataset import EvalCase, EvalDataset
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.run import Run
from modulo.db.models.trigger_event import TriggerEvent
from modulo.model_backends.base import ModelBackendBase


def _tables() -> list[Any]:
    return [
        EvalSuite.__table__,
        EvalDataset.__table__,
        EvalCase.__table__,
        Eval.__table__,
        SuiteRun.__table__,
        EvalResult.__table__,
        ModelBackend.__table__,
        Run.__table__,
        TriggerEvent.__table__,
        AuditEvent.__table__,
        AuditChainHead.__table__,
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


class _FixedJudgeAdapter(ModelBackendBase):
    """Async stub backend returning a fixed judge JSON response.

    Records every prompt so a test can prove the judge was actually invoked.
    """

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[str] = []

    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        self.calls.append(str(messages[0].content if messages else ""))
        return AIMessage(content=self._response)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[BaseMessage]:
        raise NotImplementedError("the suite-run judge only exercises invoke")

    @property
    def backend_id(self) -> str:
        return "stub-judge"


def _judge_def(org: uuid.UUID, backend_id: str | None, *, field: str = "output") -> EvalDefinition:
    config: dict[str, Any] = {"field": field}
    if backend_id is not None:
        config["model_backend_id"] = backend_id
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=org,
        name="judge",
        eval_type=EvalType.LLM_JUDGE,
        config=config,
    )


async def _seed_suite(
    session,
    org: uuid.UUID,
    *,
    subject_status: str = "active",
) -> tuple[EvalSuite, EvalDataset, ModelBackend]:
    suite = EvalSuite(organisation_id=org, name="suite", eval_definition_ids=[])
    dataset = EvalDataset(organisation_id=org, name="ds", version=1)
    backend = ModelBackend(
        organisation_id=org,
        name="subject",
        display_name="Subject",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"x",
        account_id=org,
        status=subject_status,
    )
    session.add_all([suite, dataset, backend])
    await session.flush()
    return suite, dataset, backend


async def _add_judge_def(session, org: uuid.UUID, suite: EvalSuite, config: dict[str, Any]) -> Eval:
    row = Eval(
        organisation_id=org,
        pipeline_id=uuid.uuid4(),
        name="judge",
        eval_type="llm_judge",
        config_json=config,
        eval_suite_id=suite.id,
        account_id=org,
    )
    session.add(row)
    await session.flush()
    suite.eval_definition_ids = [str(row.id)]
    return row


async def _add_regex_def(session, org: uuid.UUID, suite: EvalSuite) -> Eval:
    row = Eval(
        organisation_id=org,
        pipeline_id=uuid.uuid4(),
        name="category-regex",
        eval_type="regex",
        config_json={"pattern": "^billing$", "field": "output.category"},
        eval_suite_id=suite.id,
        account_id=org,
    )
    session.add(row)
    await session.flush()
    suite.eval_definition_ids = [str(row.id)]
    return row


async def _add_case(session, org: uuid.UUID, dataset: EvalDataset, payload: dict[str, Any]) -> EvalCase:
    case = EvalCase(
        organisation_id=org,
        dataset_id=dataset.id,
        input_payload=payload,
        input_hash="h" * 64,
    )
    session.add(case)
    await session.flush()
    return case


# --------------------------------------------------------------------------- #
# build_org_model_backend_hub                                                 #
# --------------------------------------------------------------------------- #
async def test_build_hub_returns_none_without_active_backends(session) -> None:
    org = uuid.uuid4()
    session.add(
        ModelBackend(
            organisation_id=org,
            name="inactive",
            display_name="Inactive",
            provider="openai",
            model_id="gpt-4o",
            credentials_ciphertext=b"x",
            account_id=org,
            status="inactive",
        )
    )
    await session.flush()

    hub = await build_org_model_backend_hub(session, org)

    assert hub is None


async def test_build_hub_never_resolves_a_cross_org_backend(session) -> None:
    org = uuid.uuid4()
    other = uuid.uuid4()
    session.add(
        ModelBackend(
            organisation_id=other,
            name="other-org",
            display_name="Other org",
            provider="openai",
            model_id="gpt-4o",
            credentials_ciphertext=b"x",
            account_id=other,
            status="active",
        )
    )
    await session.flush()

    hub = await build_org_model_backend_hub(session, org)

    assert hub is None


async def test_build_hub_builds_and_initialises_active_rows(session, monkeypatch) -> None:
    org = uuid.uuid4()
    backend = ModelBackend(
        organisation_id=org,
        name="local",
        display_name="Local",
        provider="ollama",
        model_id="llama3",
        credentials_ciphertext=b"",
        account_id=org,
        status="active",
    )
    session.add(backend)
    await session.flush()

    seen: list[list[Any]] = []

    async def _fake_initialise(self: Any, instances: Any, secrets_backend: Any) -> None:
        seen.append(list(instances))

    monkeypatch.setattr(ModelBackendHub, "initialise", _fake_initialise)
    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", lambda **_: object())
    monkeypatch.setattr("modulo.settings.get_settings", lambda: type("_S", (), {"fernet_key": "k"})())

    hub = await build_org_model_backend_hub(session, org)

    assert hub is not None
    assert len(seen) == 1
    assert [row.id for row in seen[0]] == [backend.id]
    await hub.__aexit__(None, None, None)


# --------------------------------------------------------------------------- #
# build_suite_run_llm_judge_callable                                          #
# --------------------------------------------------------------------------- #
def test_judge_callable_missing_backend_id_fails_closed() -> None:
    hub = ModelBackendHub()
    judge = build_suite_run_llm_judge_callable(hub)

    result = judge({"output": "x"}, _judge_def(uuid.uuid4(), None))

    assert result["passed"] is False
    assert result["score"] == 0.0
    assert "model_backend_id" in result["detail"]


def test_judge_callable_malformed_backend_id_fails_closed() -> None:
    hub = ModelBackendHub()
    judge = build_suite_run_llm_judge_callable(hub)

    result = judge({"output": "x"}, _judge_def(uuid.uuid4(), "not-a-uuid"))

    assert result["passed"] is False
    assert result["score"] == 0.0
    assert "malformed" in result["detail"]


def test_judge_callable_dispatches_each_definition_to_its_own_backend() -> None:
    org = uuid.uuid4()
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()
    hub = ModelBackendHub()
    hub.register(id_a, _FixedJudgeAdapter('{"passed": true, "score": 0.13, "detail": "A"}'))
    hub.register(id_b, _FixedJudgeAdapter('{"passed": true, "score": 0.87, "detail": "B"}'))
    judge = build_suite_run_llm_judge_callable(hub)

    result_a = judge({"output": "x"}, _judge_def(org, str(id_a)))
    result_b = judge({"output": "x"}, _judge_def(org, str(id_b)))

    assert result_a["score"] == pytest.approx(0.13)
    assert result_b["score"] == pytest.approx(0.87)


# --------------------------------------------------------------------------- #
# _maybe_wire_suite_judge                                                     #
# --------------------------------------------------------------------------- #
async def test_maybe_wire_no_judge_defs_builds_no_hub(session, monkeypatch) -> None:
    import modulo.core.saq_worker as sw
    from modulo.core.eval_engine import suite_run_judge

    org = uuid.uuid4()
    suite, dataset, backend = await _seed_suite(session, org)
    await _add_regex_def(session, org, suite)
    await _add_case(session, org, dataset, {"category": "billing"})
    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )

    calls: list[uuid.UUID] = []

    async def _spy(session_: Any, org_: uuid.UUID) -> Any:
        calls.append(org_)
        return None

    monkeypatch.setattr(suite_run_judge, "build_org_model_backend_hub", _spy)

    kwargs: dict[str, Any] = {}
    hub = await sw._maybe_wire_suite_judge(session, run, kwargs)

    assert hub is None
    assert not calls
    assert "llm_judge_callable" not in kwargs


async def test_maybe_wire_judge_def_sets_callable_and_returns_hub(session, monkeypatch) -> None:
    import modulo.core.saq_worker as sw
    from modulo.core.eval_engine import suite_run_judge

    org = uuid.uuid4()
    suite, dataset, backend = await _seed_suite(session, org)
    await _add_judge_def(session, org, suite, {"field": "output", "model_backend_id": str(uuid.uuid4())})
    await _add_case(session, org, dataset, {"text": "hello"})
    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )

    sentinel = object()

    async def _fake(session_: Any, org_: uuid.UUID) -> Any:
        assert org_ == org
        return sentinel

    monkeypatch.setattr(suite_run_judge, "build_org_model_backend_hub", _fake)

    kwargs: dict[str, Any] = {}
    hub = await sw._maybe_wire_suite_judge(session, run, kwargs)

    assert hub is sentinel
    assert callable(kwargs["llm_judge_callable"])


async def test_maybe_wire_no_active_backend_leaves_kwargs_untouched(session, monkeypatch) -> None:
    import modulo.core.saq_worker as sw
    from modulo.core.eval_engine import suite_run_judge

    org = uuid.uuid4()
    suite, dataset, backend = await _seed_suite(session, org, subject_status="inactive")
    await _add_judge_def(session, org, suite, {"field": "output", "model_backend_id": str(uuid.uuid4())})
    await _add_case(session, org, dataset, {"text": "hello"})
    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )

    async def _none(session_: Any, org_: uuid.UUID) -> Any:
        return None

    monkeypatch.setattr(suite_run_judge, "build_org_model_backend_hub", _none)

    kwargs: dict[str, Any] = {}
    hub = await sw._maybe_wire_suite_judge(session, run, kwargs)

    assert hub is None
    assert "llm_judge_callable" not in kwargs


# --------------------------------------------------------------------------- #
# SAQ job end-to-end                                                          #
# --------------------------------------------------------------------------- #
async def test_saq_job_runs_judge_end_to_end_and_tears_down_hub(monkeypatch) -> None:
    import modulo.core.saq_worker as sw
    from modulo.core.eval_engine import suite_run_judge

    org = uuid.uuid4()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    judge_backend_id = uuid.uuid4()
    async with factory() as s, s.begin():
        suite, dataset, backend = await _seed_suite(s, org)
        await _add_judge_def(s, org, suite, {"field": "output", "model_backend_id": str(judge_backend_id)})
        await _add_case(s, org, dataset, {"text": "hello"})
        run = await build_suite_run(
            s, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )
        rid, oid = str(run.id), str(org)

    adapter = _FixedJudgeAdapter('{"passed": true, "score": 0.77, "detail": "judged"}')
    hub = ModelBackendHub()
    await hub.__aenter__()
    hub.register(judge_backend_id, adapter)

    async def _fake_build(session_: Any, org_: uuid.UUID) -> Any:
        return hub

    monkeypatch.setattr(suite_run_judge, "build_org_model_backend_hub", _fake_build)
    monkeypatch.setattr(sw, "_make_session_factory", lambda: factory)

    stats = await sw.execute_suite_run({}, suite_run_id=rid, org_id=oid)

    assert stats["state"] == SuiteRunState.COMPLETED.value
    assert stats["passed_cases"] == 1
    # The judge really ran (the adapter saw the rendered prompt)...
    assert adapter.calls
    # ...and the job tore the hub down (no leak).
    assert not hub.backend_ids

    async with factory() as s:
        rows = list((await s.execute(select(EvalResult).where(EvalResult.suite_run_id == uuid.UUID(rid)))).scalars())
    assert len(rows) == 1
    assert rows[0].score == pytest.approx(0.77)
    assert rows[0].passed is True

    await engine.dispose()


async def test_saq_job_missing_backend_id_fails_eval_closed_and_suite_completes(monkeypatch) -> None:
    """A judge def with NO ``model_backend_id`` fails THAT eval, suite still completes.

    AC4: the malformed/missing backend id must never raise out of the per-case
    loop. The whole suite must still reach its terminal state, with a failed
    (not excluded/errored) case — never an orchestration failure.
    """
    import modulo.core.saq_worker as sw
    from modulo.core.eval_engine import suite_run_judge

    org = uuid.uuid4()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s, s.begin():
        suite, dataset, backend = await _seed_suite(s, org)
        # No model_backend_id at all in the judge config.
        await _add_judge_def(s, org, suite, {"field": "output"})
        await _add_case(s, org, dataset, {"text": "hello"})
        run = await build_suite_run(
            s, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )
        rid, oid = str(run.id), str(org)

    # A hub IS built (an active backend exists); the callable itself fails the
    # eval closed on the missing id without raising.
    hub = ModelBackendHub()
    await hub.__aenter__()

    async def _fake_build(session_: Any, org_: uuid.UUID) -> Any:
        return hub

    monkeypatch.setattr(suite_run_judge, "build_org_model_backend_hub", _fake_build)
    monkeypatch.setattr(sw, "_make_session_factory", lambda: factory)

    stats = await sw.execute_suite_run({}, suite_run_id=rid, org_id=oid)

    assert stats["state"] == SuiteRunState.COMPLETED.value
    assert stats["failed_cases"] == 1
    assert stats["excluded_case_count"] == 0

    async with factory() as s:
        rows = list((await s.execute(select(EvalResult).where(EvalResult.suite_run_id == uuid.UUID(rid)))).scalars())
    assert len(rows) == 1
    assert rows[0].passed is False
    assert "model_backend_id" in (rows[0].detail or "")

    await engine.dispose()


async def test_saq_job_judge_suite_without_active_backend_still_refuses(monkeypatch) -> None:
    import modulo.core.saq_worker as sw

    org = uuid.uuid4()
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s, s.begin():
        suite, dataset, backend = await _seed_suite(s, org, subject_status="inactive")
        await _add_judge_def(s, org, suite, {"field": "output", "model_backend_id": str(uuid.uuid4())})
        await _add_case(s, org, dataset, {"text": "hello"})
        run = await build_suite_run(
            s, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )
        rid, oid = str(run.id), str(org)

    class _FakeIngestionService:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def ingest(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr("modulo.core.error_tracking.ErrorIngestionService", _FakeIngestionService)
    monkeypatch.setattr(sw, "_make_session_factory", lambda: factory)

    with pytest.raises(SuiteRunExecutionError, match="judge callable"):
        await sw.execute_suite_run({}, suite_run_id=rid, org_id=oid)

    async with factory() as s:
        fresh = await s.get(SuiteRun, uuid.UUID(rid))
    assert fresh is not None
    assert fresh.state == SuiteRunState.FAILED.value

    await engine.dispose()
