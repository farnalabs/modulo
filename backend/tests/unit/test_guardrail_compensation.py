"""Unit tests for FAR-213 run-termination compensation.

Covers:
  * the connector compensating-callback contract (default no-op, GitHub
    close-PR, Linear unassign/archive, not-supported, failed-on-error);
  * the ``compensate_blocked_run`` orchestrator (blocked_partial summary
    written, connector-node compensation, failure isolation, guard-the-guard);
  * the blocked_partial summary builder shape;
  * the dependent-trigger suppression guard predicate
    (``is_guardrail_blocked_run``) against in-memory SQLite.

No DB is required for the orchestrator/contract tests — the session, hub and
audit writer are stubbed. The suppression predicate uses a real Run table.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import httpx
import pytest
import respx
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.guardrails.compensation as comp
from modulo.connectors.base import (
    CompensationContext,
    CompensationOperation,
    CompensationOutcome,
    CompensationResult,
    ConnectorBase,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.github import GitHubConnector
from modulo.connectors.linear import LinearConnector
from modulo.core.guardrails.compensation import compensate_blocked_run
from modulo.db.models.base import Base
from modulo.db.models.run import Run

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_CONNECTOR = uuid.UUID("00000000-0000-0000-0000-0000000000e1")

_TABLES: list[Table] = cast(list[Table], [Run.__table__])


def _ctx(node_id: str = "node_a") -> CompensationContext:
    return CompensationContext(
        org_id=str(_ORG),
        run_id=str(_RUN),
        node_id=node_id,
        connector_instance_id=str(_CONNECTOR),
    )


# ---------------------------------------------------------------------------
# Connector compensating-callback contract
# ---------------------------------------------------------------------------


class _NoopConnector(ConnectorBase):
    """Minimal ConnectorBase subclass exercising the default compensate."""

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CUSTOM

    async def health_check(self) -> HealthResult:
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult()

    async def write(self, payload: Any) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_default_compensate_returns_not_supported():
    result = await _NoopConnector().compensate(
        CompensationOperation(resource="pr", data={"repo": "a/b"}, output={"number": 1}),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.NOT_SUPPORTED
    assert result.detail


@respx.mock
@pytest.mark.asyncio
async def test_github_compensate_closes_pr():
    repo = "acme/thing"
    pr_number = 42
    route = respx.patch(f"https://api.github.com/repos/{repo}/pulls/{pr_number}").mock(
        return_value=httpx.Response(200, json={"number": pr_number, "state": "closed"}),
    )
    connector = GitHubConnector(token="ghp_test_token")
    result = await connector.compensate(
        CompensationOperation(
            resource="pr", data={"repo": repo, "head": "b", "base": "main"}, output={"number": pr_number}
        ),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.COMPENSATED
    assert result.resource_id == str(pr_number)
    assert route.called
    assert route.calls.last.request.content == b'{"state":"closed"}'


@respx.mock
@pytest.mark.asyncio
async def test_github_compensate_not_supported_resource():
    connector = GitHubConnector(token="ghp_test_token")
    result = await connector.compensate(
        CompensationOperation(resource="issue", data={}, output={}),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.NOT_SUPPORTED


@respx.mock
@pytest.mark.asyncio
async def test_github_compensate_failed_on_api_error():
    repo = "acme/thing"
    pr_number = 42
    respx.patch(f"https://api.github.com/repos/{repo}/pulls/{pr_number}").mock(
        return_value=httpx.Response(500, text="boom"),
    )
    connector = GitHubConnector(token="ghp_test_token")
    result = await connector.compensate(
        CompensationOperation(resource="pr", data={"repo": repo}, output={"number": pr_number}),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.FAILED
    assert "close PR failed" in result.detail


@respx.mock
@pytest.mark.asyncio
async def test_github_compensate_requires_repo_and_number():
    connector = GitHubConnector(token="ghp_test_token")
    no_repo = await connector.compensate(
        CompensationOperation(resource="pr", data={}, output={"number": 1}),
        context=_ctx(),
        error="guardrail block",
    )
    assert no_repo.outcome == CompensationOutcome.NOT_SUPPORTED
    no_number = await connector.compensate(
        CompensationOperation(resource="pr", data={"repo": "a/b"}, output={}),
        context=_ctx(),
        error="guardrail block",
    )
    assert no_number.outcome == CompensationOutcome.NOT_SUPPORTED


@respx.mock
@pytest.mark.asyncio
async def test_linear_compensate_unassign():
    issue_id = "lin-issue-1"
    route = respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"issueUpdate": {"success": True, "issue": {"id": issue_id}}}},
        ),
    )
    connector = LinearConnector(api_key="lin-test-key")
    result = await connector.compensate(
        CompensationOperation(resource="issue_assign", data={"id": issue_id, "assigneeId": "u1"}, output={}),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.COMPENSATED
    assert result.resource_id == issue_id
    assert route.called
    assert "assigneeId" in route.calls.last.request.content.decode()


@respx.mock
@pytest.mark.asyncio
async def test_linear_compensate_archives_created_issue():
    issue_id = "lin-issue-2"
    route = respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"issueArchive": {"success": True}}},
        ),
    )
    connector = LinearConnector(api_key="lin-test-key")
    result = await connector.compensate(
        CompensationOperation(resource="issue", data={"title": "x"}, output={"id": issue_id}),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.COMPENSATED
    assert result.resource_id == issue_id
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_linear_compensate_not_supported_resource():
    connector = LinearConnector(api_key="lin-test-key")
    result = await connector.compensate(
        CompensationOperation(resource="issue_comment", data={}, output={}),
        context=_ctx(),
        error="guardrail block",
    )
    assert result.outcome == CompensationOutcome.NOT_SUPPORTED


# ---------------------------------------------------------------------------
# compensate_blocked_run orchestrator (session/hub/audit stubbed)
# ---------------------------------------------------------------------------


class _FakeScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Minimal AsyncSession stand-in: graph query + flush only."""

    def __init__(self, graph: dict[str, Any] | None = None) -> None:
        self._graph = graph if graph is not None else {"nodes": []}
        self.flushed: list[bool] = []

    async def execute(self, _stmt: Any) -> _FakeScalar:
        return _FakeScalar(self._graph)

    async def flush(self) -> None:
        self.flushed.append(True)


class _FakeRun:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = _ORG
        self.snapshot_id = _SNAPSHOT
        self.outputs_json: dict[str, Any] | None = None
        self.blocked_partial_summary: dict[str, Any] | None = None


class _StubConnector:
    """Stub connector: configurable compensate outcome / raise behaviour."""

    def __init__(self, result: CompensationResult | None = None, raise_on_compensate: bool = False) -> None:
        self._result = result
        self._raise = raise_on_compensate
        self.calls: list[CompensationOperation] = []

    async def compensate(
        self,
        operation: CompensationOperation,
        *,
        context: CompensationContext,
        error: str,
    ) -> CompensationResult:
        self.calls.append(operation)
        if self._raise:
            raise RuntimeError("compensation boom")
        return self._result or CompensationResult(outcome=CompensationOutcome.NOT_SUPPORTED, detail="nope")


class _FakeHub:
    def __init__(self, connector: Any, raise_on_get: bool = False) -> None:
        self._connector = connector
        self._raise = raise_on_get

    def get(self, _connector_id: Any) -> Any:
        if self._raise:
            raise KeyError("connector not found")
        return self._connector


@pytest.fixture
def audit_patch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub append_audit_event so the orchestrator never touches a DB."""
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    monkeypatch.setattr(comp, "append_audit_event", mock)
    return mock


@pytest.mark.asyncio
async def test_compensate_blocked_run_no_hub_writes_summary(audit_patch: Any):
    session = _FakeSession()
    run = _FakeRun()
    summary = await compensate_blocked_run(
        session,
        run,
        guardrail_block="Guardrail 'secret' blocked: matched credential pattern",
        blocking_eval_name="secret",
    )
    assert summary["blocked"] is True
    assert summary["blocking_eval_name"] == "secret"
    assert summary["executed_nodes"] == []
    assert summary["nodes"] == []
    assert run.blocked_partial_summary == summary
    assert session.flushed
    audit_patch.assert_awaited_once()


@pytest.mark.asyncio
async def test_compensate_blocked_run_compensates_connector_node(audit_patch: Any):
    graph = {
        "nodes": [
            {
                "id": "node_pr",
                "connector_binding": {"instance_id": str(_CONNECTOR), "resource": "pr", "data": {"repo": "a/b"}},
            },
        ]
    }
    session = _FakeSession(graph=graph)
    run = _FakeRun()
    connector = _StubConnector(result=CompensationResult(outcome=CompensationOutcome.COMPENSATED, detail="closed"))
    executed = {"node_pr": {"artifacts": [], "output": {"number": 7}}}

    summary = await compensate_blocked_run(
        session,
        run,
        guardrail_block="blocked",
        connector_hub=_FakeHub(connector),
        executed_nodes=executed,
    )
    assert summary["nodes"][0]["publish_status"] == "compensated"
    assert summary["nodes"][0]["compensation"]["outcome"] == "compensated"
    assert summary["nodes"][0]["output_ref"] == {"run_id": str(run.id), "node_id": "node_pr"}
    assert connector.calls[0].resource == "pr"
    assert connector.calls[0].output == {"number": 7}
    assert connector.calls[0].data == {"repo": "a/b"}
    assert run.blocked_partial_summary is summary
    # one attempt audit per compensated node + one summary audit
    assert audit_patch.await_count == 2


@pytest.mark.asyncio
async def test_compensate_blocked_run_failure_isolation(audit_patch: Any):
    """A raising node must not prevent the sibling node's compensation."""
    graph = {
        "nodes": [
            {
                "id": "node_raise",
                "connector_binding": {"instance_id": str(_CONNECTOR), "resource": "pr", "data": {"repo": "a/b"}},
            },
            {
                "id": "node_ok",
                "connector_binding": {"instance_id": str(_CONNECTOR), "resource": "pr", "data": {"repo": "a/b"}},
            },
        ]
    }
    session = _FakeSession(graph=graph)
    run = _FakeRun()

    class _MixedConnector:
        async def compensate(self, operation, *, context, error):
            if operation.output.get("number") == 1:
                raise RuntimeError("boom")
            return CompensationResult(outcome=CompensationOutcome.COMPENSATED, detail="closed")

    executed = {
        "node_raise": {"output": {"number": 1}},
        "node_ok": {"output": {"number": 2}},
    }
    summary = await compensate_blocked_run(
        session,
        run,
        guardrail_block="blocked",
        connector_hub=_FakeHub(_MixedConnector()),
        executed_nodes=executed,
    )
    statuses = {n["node_id"]: n["publish_status"] for n in summary["nodes"]}
    assert statuses["node_raise"] == "not-compensated"
    assert statuses["node_ok"] == "compensated"
    assert run.blocked_partial_summary is not None


@pytest.mark.asyncio
async def test_compensate_blocked_run_guard_the_guard_hub_get_raises(audit_patch: Any):
    """A connector-hub get() failure must never crash terminalization."""
    graph = {
        "nodes": [
            {
                "id": "node_a",
                "connector_binding": {"instance_id": str(_CONNECTOR), "resource": "pr", "data": {"repo": "a/b"}},
            },
        ]
    }
    session = _FakeSession(graph=graph)
    run = _FakeRun()
    executed = {"node_a": {"output": {"number": 1}}}
    summary = await compensate_blocked_run(
        session,
        run,
        guardrail_block="blocked",
        connector_hub=_FakeHub(_StubConnector(), raise_on_get=True),
        executed_nodes=executed,
    )
    assert summary["nodes"][0]["publish_status"] == "not-compensated"
    assert run.blocked_partial_summary is not None


@pytest.mark.asyncio
async def test_compensate_blocked_run_guard_the_guard_audit_failure(audit_patch: Any):
    """An audit-write failure must be swallowed (fail-open with log)."""

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit table unavailable")

    audit_patch.side_effect = _boom
    session = _FakeSession()
    run = _FakeRun()
    summary = await compensate_blocked_run(
        session,
        run,
        guardrail_block="blocked",
        executed_nodes={"node_a": {"output": {"number": 1}}},
    )
    assert summary["blocked"] is True
    assert run.blocked_partial_summary is not None


@pytest.mark.asyncio
async def test_compensate_blocked_run_executed_nodes_falls_back_to_outputs(audit_patch: Any):
    """Without an explicit executed_nodes, the run's outputs_json is used."""
    graph = {
        "nodes": [
            {
                "id": "node_a",
                "connector_binding": {"instance_id": str(_CONNECTOR), "resource": "pr", "data": {"repo": "a/b"}},
            },
        ]
    }
    session = _FakeSession(graph=graph)
    run = _FakeRun()
    run.outputs_json = {"node_a": {"output": {"number": 5}}}
    connector = _StubConnector(result=CompensationResult(outcome=CompensationOutcome.COMPENSATED, detail="closed"))
    summary = await compensate_blocked_run(
        session,
        run,
        guardrail_block="blocked",
        connector_hub=_FakeHub(connector),
    )
    assert summary["executed_nodes"] == ["node_a"]
    assert summary["nodes"][0]["publish_status"] == "compensated"


# ---------------------------------------------------------------------------
# blocked_partial summary builder
# ---------------------------------------------------------------------------


def test_build_summary_shape():
    run = _FakeRun()
    executed = {"node_a": {"output": {"number": 1}}}
    per_node = [
        {
            "node_id": "node_a",
            "publish_status": "compensated",
            "output_ref": {"run_id": str(run.id), "node_id": "node_a"},
            "compensation": {"outcome": "compensated", "reason": "closed", "resource_id": "1"},
        }
    ]
    summary = comp._build_summary(run, "blocked: pattern", "secret", executed, per_node)
    assert summary == {
        "blocked": True,
        "blocking_eval_name": "secret",
        "block_message": "blocked: pattern",
        "run_id": str(run.id),
        "executed_nodes": ["node_a"],
        "nodes": per_node,
    }


# ---------------------------------------------------------------------------
# Dependent-trigger suppression guard predicate
# ---------------------------------------------------------------------------


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


async def _seed_run(
    session: AsyncSession, *, run_id: uuid.UUID, status: str, error_code: str | None, run_number: int
) -> None:
    session.add(
        Run(
            id=run_id,
            organisation_id=_ORG,
            pipeline_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
            snapshot_id=_SNAPSHOT,
            trigger_type="manual",
            input_hash="hash",
            run_number=run_number,
            langgraph_thread_id=f"t-{run_id}",
            status=status,
            error_code=error_code,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_is_guardrail_blocked_run_true(session: AsyncSession):
    from modulo.core.trigger_engine import is_guardrail_blocked_run

    blocked_id = uuid.uuid4()
    await _seed_run(session, run_id=blocked_id, status="eval_failed", error_code="eval_blocked", run_number=1)
    assert await is_guardrail_blocked_run(session, blocked_id) is True


@pytest.mark.asyncio
async def test_is_guardrail_blocked_run_false_for_other_states(session: AsyncSession):
    from modulo.core.trigger_engine import is_guardrail_blocked_run

    complete_id = uuid.uuid4()
    plain_failed_id = uuid.uuid4()
    missing_id = uuid.uuid4()
    await _seed_run(session, run_id=complete_id, status="complete", error_code=None, run_number=1)
    await _seed_run(session, run_id=plain_failed_id, status="failed", error_code="node_cancelled", run_number=2)
    assert await is_guardrail_blocked_run(session, complete_id) is False
    assert await is_guardrail_blocked_run(session, plain_failed_id) is False
    assert await is_guardrail_blocked_run(session, missing_id) is False
