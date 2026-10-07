"""FAR-1141 structural guards — the dispatch invariants that must hold for EVERY writer.

The pre-PR QA pass found each of these resting on a single write-path side
effect (a Pydantic model mutating ``idempotent``) or on a ``node_type`` check
while the engine routes on the binding's ``operation``. Every test here fails
without its fix:

* CRITICAL 1 — a graph that FIRES an external job is non-idempotent and its
  node is never re-invoked, derived from the BINDING (not the stored flag), so
  an MCP-authored or hand-written graph is covered too.
* CRITICAL 2 — the run classifier and the engine route on the SAME helper.
* MAJOR 3/4 — the engine fails loud on a dispatch node that would silently
  query, and on wait config nothing reads.
* MAJOR 5 — the poll loop checks its deadline BEFORE polling, bounds every
  poll, retries transient poll faults inside the window, and always loses the
  race to the typed ``dispatch.wait_timeout``.
* MAJOR 6 — every dispatch result shape carries provenance, including on the
  failed-artifact path.
* MAJOR 7 — a dispatch node with no binding fails graph build instead of
  falling through to an LLM node.

No DB, no Docker, no network: the connector and session are in-memory stubs.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.connectors.base import (
    CIRun,
    CIRunStatus,
    ConnectorPermissionError,
    connector_binding_operation,
    node_fires_dispatch_job,
)
from modulo.core.node_output_split import DISPATCH_PROVENANCE_FIELDS, split_node_output
from modulo.core.pipeline_engine.decorator import set_connector_hub
from modulo.core.pipeline_engine.executor import _graph_is_idempotent, _retry_policy_applies
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json
from modulo.core.pipeline_engine.node_runner import make_connector_fn
from modulo.core.pipeline_engine.runtime_retry import make_retrying_node_fn
from modulo.db.crud.run import graph_contains_dispatch

_CI_TYPE = "github_actions_ci"


def _binding(**overrides: Any) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "instance_id": str(uuid.uuid4()),
        "type": _CI_TYPE,
        "operation": "dispatch",
        "dispatch_action": "trigger_run",
    }
    binding.update(overrides)
    return binding


def _job_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "dispatch-job",
        "node_type": "connector",
        "connector_binding": _binding(),
    }
    node.update(overrides)
    return node


def _graph_with(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": []}


# ---------------------------------------------------------------------------
# CRITICAL 1 — non-idempotency derived from the binding, not the stored flag
# ---------------------------------------------------------------------------


def test_graph_is_idempotent_false_for_an_unflagged_job_firing_binding():
    """MCP-authored shape: raw node dict, NO ``idempotent`` key at all.

    Before the fix only ``idempotent is False`` made a graph non-idempotent,
    so a graph whose dispatch binding fires a job but never went through the
    REST model stayed retryable and a retry fired a SECOND job.
    """
    assert _graph_is_idempotent(_graph_with(_job_node())) is False


def test_graph_is_idempotent_false_even_when_the_flag_claims_true():
    """An explicit ``idempotent=true`` on a job-firing node cannot re-arm retry."""
    assert _graph_is_idempotent(_graph_with(_job_node(idempotent=True))) is False


def test_graph_is_idempotent_true_for_a_read_only_dispatch_action():
    """``get_run_status`` is a read — re-running it fires nothing."""
    node = _job_node(connector_binding=_binding(dispatch_action="get_run_status"))
    assert _graph_is_idempotent(_graph_with(node)) is True


def test_graph_is_idempotent_true_when_only_a_query_binding_is_present():
    node = _job_node(connector_binding=_binding(operation="query"))
    assert _graph_is_idempotent(_graph_with(node)) is True


def test_run_level_retry_gate_is_closed_by_the_binding_alone():
    """The binding alone must close the run-level ``retry_policy`` gate."""
    budget = _retry_after_budget()
    assert (
        _retry_policy_applies(
            budget, is_correction_run=False, graph_idempotent=_graph_is_idempotent(_graph_with(_job_node()))
        )
        is False
    )
    # control: the same budget applies when the graph really is idempotent
    query_only = _job_node(connector_binding=_binding(operation="query"))
    assert (
        _retry_policy_applies(
            budget, is_correction_run=False, graph_idempotent=_graph_is_idempotent(_graph_with(query_only))
        )
        is True
    )


def _retry_after_budget() -> int:
    from modulo.core.pipeline_engine.executor import _retry_after_policy

    policy = {"max_retries": 3}
    budget = _retry_after_policy(policy, "failed", "DispatchWaitTimeoutError", "wait window expired")
    assert budget == 3
    return budget


# ---------------------------------------------------------------------------
# CRITICAL 1 — the node is never re-invoked (per-node retry + per-edge retry)
# ---------------------------------------------------------------------------


async def test_node_retry_never_reinvokes_a_job_firing_dispatch_node():
    """The pipeline's own ``retry_policy`` must not re-run a job-firing node."""
    calls: list[int] = []

    async def _raw(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise TimeoutError("transient")

    wrapped = make_retrying_node_fn(
        _raw,
        node_id="dispatch-job",
        node_def=_job_node(),
        pipeline_retry_policy={"max_retries": 3, "backoff": 0.0},
    )
    with pytest.raises(TimeoutError):
        await wrapped({})
    assert len(calls) == 1


async def test_edge_retry_never_reexecutes_a_dispatch_source():
    """The SOURCE of an incoming retry edge is never a job-firing node."""
    source_calls: list[int] = []

    async def _source(state: dict[str, Any]) -> dict[str, Any]:
        source_calls.append(1)
        return {"src": "fresh"}

    async def _target(state: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("transient")

    source_node = _job_node(id="src")
    wrapped = make_retrying_node_fn(
        _target,
        node_id="target",
        node_def={"id": "target"},
        pipeline_retry_policy=None,
        incoming_edges=[
            {"source": "src", "retry": {"max_attempts": 2, "backoff": 0.0, "events": ["error", "timeout"]}}
        ],
        raw_fn_resolver={"src": _source}.__getitem__,
        node_defs={"src": source_node, "target": {"id": "target"}},
    )
    with pytest.raises(TimeoutError):
        await wrapped({})
    assert not source_calls


async def test_edge_retry_never_reinvokes_the_failing_dispatch_node():
    """An edge retry re-runs the SOURCE and then THIS node — so a job-firing
    failing node must short-circuit the whole edge-retry path."""
    target_calls: list[int] = []

    async def _source(state: dict[str, Any]) -> dict[str, Any]:
        return {"src": "fresh"}

    async def _target(state: dict[str, Any]) -> dict[str, Any]:
        target_calls.append(1)
        raise TimeoutError("transient")

    failing_node = _job_node(id="target")
    wrapped = make_retrying_node_fn(
        _target,
        node_id="target",
        node_def=failing_node,
        pipeline_retry_policy=None,
        incoming_edges=[
            {"source": "src", "retry": {"max_attempts": 2, "backoff": 0.0, "events": ["error", "timeout"]}}
        ],
        raw_fn_resolver={"src": _source}.__getitem__,
        node_defs={"src": {"id": "src"}, "target": failing_node},
    )
    with pytest.raises(TimeoutError):
        await wrapped({})
    assert len(target_calls) == 1


# ---------------------------------------------------------------------------
# CRITICAL 2 — one routing helper, every consumer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node", "expected_operation", "expected_dispatched"),
    [
        # (a) under-claim before the fix: a connector node firing a real job
        (
            {
                "node_type": "connector",
                "connector_binding": {
                    "instance_id": "i",
                    "type": _CI_TYPE,
                    "operation": "dispatch",
                    "dispatch_action": "trigger_run",
                },
            },
            "dispatch",
            True,
        ),
        # (b) over-claim before the fix: a dispatch node left on the query default
        (
            {
                "node_type": "dispatch",
                "connector_binding": {"instance_id": "i", "type": _CI_TYPE, "operation": "query"},
            },
            "query",
            False,
        ),
        # the engine's fallback for a dispatch node whose binding omits the verb
        (
            {"node_type": "dispatch", "connector_binding": {"instance_id": "i", "type": _CI_TYPE}},
            "dispatch",
            True,
        ),
        ({"node_type": "agent"}, "query", False),
        (
            {"node_type": "hitl", "connector_binding": {"instance_id": "i", "type": _CI_TYPE, "operation": "write"}},
            "write",
            False,
        ),
    ],
)
def test_run_classifier_and_engine_route_on_the_same_condition(
    node: dict[str, Any],
    expected_operation: str,
    expected_dispatched: bool,
):
    """``graph_contains_dispatch`` (run ``execution_origin``) and the engine's
    verb resolution must agree node-for-node — CRITICAL 2's whole point."""
    graph = _graph_with(node)
    assert connector_binding_operation(node) == expected_operation
    assert graph_contains_dispatch(graph) is expected_dispatched
    if expected_operation == "dispatch":
        assert node_fires_dispatch_job(node) is (
            node["connector_binding"].get("dispatch_action", "trigger_run") == "trigger_run"
        )
    else:
        assert node_fires_dispatch_job(node) is False


class _RecordingHub:
    def __init__(self, connector: Any) -> None:
        self._connector = connector

    def get(self, instance_id: uuid.UUID) -> Any:
        return self._connector


class _VerbRecorder:
    """Records which verb the node actually executed."""

    def __init__(self) -> None:
        self.verbs: list[str] = []

    async def query(self, q: Any) -> Any:
        self.verbs.append("query")
        return {"records": []}

    async def write(self, payload: Any) -> Any:
        self.verbs.append("write")
        return {"written": True}

    async def get_run_status(self, run_id: str) -> Any:
        self.verbs.append("get_run_status")
        return CIRun(id=run_id, pipeline_id="p", status=CIRunStatus.SUCCESS)

    async def trigger_run(
        self, pipeline_id: str = "", branch: str = "", variables: dict[str, str] | None = None
    ) -> Any:
        self.verbs.append("trigger_run")
        return CIRun(id="job-1", pipeline_id=pipeline_id, status=CIRunStatus.QUEUED, branch=branch)


async def test_connector_node_with_a_dispatch_binding_executes_the_ci_method():
    """A ``connector``-typed node carrying ``operation="dispatch"`` must run the
    CI method — not fall through to ``query`` (CRITICAL 1's second half)."""
    recorder = _VerbRecorder()
    set_connector_hub(_RecordingHub(recorder))
    try:
        node_def = {
            "id": "conn-dispatch",
            "node_type": "connector",
            "connector_binding": {
                "instance_id": str(uuid.uuid4()),
                "type": _CI_TYPE,
                "operation": "dispatch",
                "dispatch_action": "get_run_status",
            },
        }
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {"run_id": "r-1"}}})
    finally:
        set_connector_hub(None)
    assert recorder.verbs == ["get_run_status"]
    assert result["artifacts"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# iteration 2 (FAR-1141) FIX D — the classifier never claims a dispatch the
# engine will not fire
# ---------------------------------------------------------------------------


def _agent_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": "a1", "node_type": "agent", "agent_id": str(uuid.uuid4())}
    node.update(overrides)
    return node


def _engine_route(node: dict[str, Any]) -> str:
    """Which node factory ``graph_cache._make_node_fn`` picks for *node*.

    Every factory it can select is patched with a recorder, so the routing is
    OBSERVED (which branch ran) rather than inferred from the source.
    """
    import modulo.core.pipeline_engine.graph_cache as gc

    picked: list[str] = []

    def _record(name: str):
        def _fn(*args: Any, **kwargs: Any) -> str:
            picked.append(name)
            return f"<{name}>"

        return _fn

    with (
        patch.object(gc, "make_connector_fn", _record("connector")),
        patch.object(gc, "make_node_fn", _record("agent")),
        patch.object(gc, "make_manual_node_fn", _record("manual")),
        patch.object(gc, "make_sandbox_agent_fn", _record("sandbox_agent")),
    ):
        gc._make_node_fn(node, timeout=5, session_factory=None, single_sandbox_node=False)
    return picked[0] if picked else "unrouted"


@pytest.mark.parametrize(
    ("node", "expected_operation", "expected_route"),
    [
        # routed: the engine builds the connector node fn, so the binding's verb is real
        (
            {"id": "c1", "node_type": "connector", "connector_binding": _binding()},
            "dispatch",
            "connector",
        ),
        (
            {"id": "d1", "node_type": "dispatch", "connector_binding": _binding()},
            "dispatch",
            "connector",
        ),
        (
            {"id": "h1", "node_type": "hitl", "connector_binding": _binding()},
            "dispatch",
            "connector",
        ),
        # a hitl node with an agent still routes its BINDING to the connector
        (
            _agent_node(id="h2", node_type="hitl", connector_binding=_binding()),
            "dispatch",
            "connector",
        ),
        # NOT routed: the engine runs the LLM factory for agent + agent_id, so
        # the binding is dead configuration and the run must NOT read dispatched
        (
            _agent_node(connector_binding=_binding()),
            "query",
            "agent",
        ),
        # NOT routed: sandbox_agent builds the sandbox fn before the binding
        (
            {
                "id": "s1",
                "node_type": "sandbox_agent",
                "template_id": "opencode",
                "connector_binding": _binding(),
            },
            "query",
            "sandbox_agent",
        ),
        (_agent_node(), "query", "agent"),
        ({"id": "m1", "node_type": "manual"}, "query", "manual"),
    ],
    ids=[
        "connector-dispatch-binding",
        "dispatch-node-dispatch-binding",
        "hitl-dispatch-binding",
        "hitl-agent-dispatch-binding",
        "agent-agent-id-dispatch-binding",
        "sandbox-agent-dispatch-binding",
        "agent-no-binding",
        "manual-no-binding",
    ],
)
def test_classifier_and_engine_agree_on_every_node_type(
    node: dict[str, Any],
    expected_operation: str,
    expected_route: str,
):
    """CRITICAL 2 extended to the ROUTING gate (FAR-1141 criterion 4): a graph
    the run classifier calls ``dispatched`` must contain a node the engine
    actually routes to a connector — and the two must agree node-for-node on
    the verb too."""
    assert connector_binding_operation(node) == expected_operation
    assert _engine_route(node) == expected_route
    if graph_contains_dispatch(_graph_with(node)):
        assert expected_route == "connector", (
            f"graph_contains_dispatch claimed dispatch for a node the engine routes to {expected_route!r}",
        )


def test_an_agent_node_with_a_dispatch_binding_fires_nothing():
    """The over-claim itself: an agent node's binding is never routed, so it is
    never a dispatch and never fires a job (the save path now rejects it — see
    ``test_api_agent_node_rejects_a_dispatch_connector_binding``)."""
    node = _agent_node(connector_binding=_binding())
    assert connector_binding_operation(node) == "query"
    assert node_fires_dispatch_job(node) is False
    assert graph_contains_dispatch(_graph_with(node)) is False


# ---------------------------------------------------------------------------
# MAJOR 3 / MAJOR 4 — the engine fails loud instead of silently no-opping
# ---------------------------------------------------------------------------


def test_dispatch_node_with_a_query_binding_fails_graph_build():
    node = {
        "id": "d",
        "node_type": "dispatch",
        "connector_binding": _binding(operation="query"),
    }
    with pytest.raises(ValueError, match=r"must route connector_binding\.operation='dispatch'"):
        make_connector_fn(node)


def test_await_completion_without_trigger_run_fails_graph_build():
    node = {
        "id": "d",
        "node_type": "dispatch",
        "connector_binding": _binding(dispatch_action="get_run_status"),
        "await_completion": True,
    }
    with pytest.raises(ValueError, match="requires dispatch_action='trigger_run'"):
        make_connector_fn(node)


def test_wait_timeout_without_await_completion_fails_graph_build():
    node = {
        "id": "d",
        "node_type": "dispatch",
        "connector_binding": _binding(),
        "wait_timeout": 60,
    }
    with pytest.raises(ValueError, match="wait_timeout requires await_completion=True"):
        make_connector_fn(node)


def test_dispatch_node_without_a_binding_fails_graph_build():
    """MAJOR 7: it used to fall through to the agent/LLM node factory."""
    graph = _graph_with({"id": "d", "node_type": "dispatch"})
    with pytest.raises(ValueError, match="no connector_binding"):
        build_graph_from_json(graph)


# ---------------------------------------------------------------------------
# MAJOR 5 — the await_completion poll loop
# ---------------------------------------------------------------------------


class _PollConnector:
    """``get_run_status`` stub: records entry timestamps and replays a script.

    Script items: an exception -> raise it, a number -> sleep then report
    ``in_progress``, a ``CIRunStatus`` -> report it.
    """

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script: list[Any] = list(script or [])
        self.calls: list[float] = []
        self.trigger_calls = 0

    async def query(self, q: Any) -> Any:
        raise AssertionError("dispatch node must never query")

    async def write(self, payload: Any) -> Any:
        raise AssertionError("dispatch node must never write")

    async def trigger_run(
        self, pipeline_id: str = "", branch: str = "", variables: dict[str, str] | None = None
    ) -> Any:
        self.trigger_calls += 1
        return CIRun(id="job-1", pipeline_id=pipeline_id, status=CIRunStatus.QUEUED, branch=branch)

    async def get_run_status(self, run_id: str) -> Any:
        self.calls.append(asyncio.get_running_loop().time())
        if not self.script:
            return CIRun(id=run_id, pipeline_id="p", status=CIRunStatus.IN_PROGRESS)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int | float):
            await asyncio.sleep(item)
            return CIRun(id=run_id, pipeline_id="p", status=CIRunStatus.IN_PROGRESS)
        return CIRun(id=run_id, pipeline_id="p", status=item)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """A real ``httpx`` status fault, shaped exactly as a connector's own
    ``raise_for_status()`` raises it (request + response attached)."""
    request = httpx.Request("GET", "https://api.example.com/runs/1")
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=request,
        response=httpx.Response(status, request=request),
    )


def _wrapped_http_status_error(status: int, message: str) -> ValueError:
    """The connector's fail-loud wrap of an HTTP fault: ``raise ValueError(...)
    from httpx.HTTPStatusError`` — the shape GitHub Actions and GitLab CI
    actually produce, so the retry classifier has to read the cause chain."""
    error = ValueError(message)
    error.__cause__ = _http_status_error(status)
    return error


class _SlowStatusConnector:
    """``get_run_status`` that always takes *delay* seconds, then reports a
    terminal status — one call, one outcome (no script, so a cancelled call
    cannot consume the next scripted item)."""

    def __init__(self, delay: float, status: CIRunStatus = CIRunStatus.SUCCESS) -> None:
        self.delay = delay
        self.status = status
        self.calls: list[float] = []

    async def get_run_status(self, run_id: str) -> Any:
        self.calls.append(asyncio.get_running_loop().time())
        await asyncio.sleep(self.delay)
        return CIRun(id=run_id, pipeline_id="p", status=self.status)


async def test_deadline_is_checked_before_each_poll_so_no_poll_outlives_the_window():
    """The old loop checked the deadline only AFTER a poll, so every wait fired
    one extra poll once the window had closed."""
    connector = _PollConnector()
    with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 5.0):
        wait_timeout = 0.5
        start = asyncio.get_running_loop().time()
        with pytest.raises(nr.DispatchWaitTimeoutError):
            await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=wait_timeout)
        end = asyncio.get_running_loop().time()
    assert len(connector.calls) == 1, "a poll must never start after the wait window closed"
    assert connector.calls[0] < start + wait_timeout
    assert end < start + wait_timeout + 1.0


async def test_transient_poll_errors_are_retried_inside_the_window():
    """A single 429/5xx/network blip must NOT abort the wait — the external job
    is already fired and abandoning the observation misreports the run.

    The faults are the real transient shapes (an HTTP 429 and an HTTP 503,
    exactly what a CI status endpoint raises), not a generic stand-in: the loop
    classifies before retrying, so only a genuinely transient fault may land
    here (``test_a_permanent_poll_error_is_never_retried_and_never_relabelled``
    pins the other side of that split).
    """
    connector = _PollConnector([_http_status_error(429), _http_status_error(503), CIRunStatus.SUCCESS])
    with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001):
        state = await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=10)
    assert state["status"] == "success"
    assert len(connector.calls) == 3


async def test_a_persistent_poll_failure_surfaces_as_itself_not_a_wait_timeout():
    """A transient fault that never recovers surfaces AS ITSELF once the
    consecutive-error cap is hit — never as ``dispatch.wait_timeout`` (which
    would blame the substrate for a dead status endpoint)."""
    connector = _PollConnector([TimeoutError("status endpoint keeps timing out")] * 50)
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001),
        pytest.raises(TimeoutError, match="status endpoint keeps timing out"),
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=30)
    assert len(connector.calls) == nr._DISPATCH_WAIT_MAX_CONSECUTIVE_POLL_ERRORS + 1


# ---------------------------------------------------------------------------
# iteration 2 (FAR-1141) FIX 4 — the error budget is time-scaled, not a count
# ---------------------------------------------------------------------------


def test_the_poll_error_budget_scales_with_the_remaining_window():
    """The tolerance must grow with the window that is actually left.

    The old fixed count was ~16 s of slack for a wait_timeout up to an hour, so
    a single 60 s rate-limit window on a long wait aborted a live job.
    """
    floor = nr._DISPATCH_WAIT_MAX_CONSECUTIVE_POLL_ERRORS

    # A short window keeps exactly the old behaviour (the floor).
    assert nr._dispatch_poll_error_budget(0) == floor
    assert nr._dispatch_poll_error_budget(30) == floor

    # A long window buys strictly more tolerance, one step per budgeted period.
    long_window = 3600.0
    expected = floor + int(long_window // nr._DISPATCH_POLL_ERROR_BUDGET_SECONDS)
    assert nr._dispatch_poll_error_budget(long_window) == expected
    assert expected > floor

    # ...and the budget is monotonic in the remaining window.
    assert nr._dispatch_poll_error_budget(1800) > nr._dispatch_poll_error_budget(600)


async def test_a_long_wait_survives_many_more_transient_failures_than_the_old_fixed_cap():
    """FAR-1141 FIX 4: 40 consecutive 429s inside a 1-hour window must NOT abort
    the wait — the old fixed cap of 8 aborted on the 9th, stranding a live
    external job behind a misleading failure."""
    connector = _PollConnector([_http_status_error(429)] * 40 + [CIRunStatus.SUCCESS])
    with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001):
        state = await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=3600)

    assert state["status"] == "success"
    assert len(connector.calls) == 41
    # the run survived strictly past what the old fixed count allowed.
    assert len(connector.calls) > nr._DISPATCH_WAIT_MAX_CONSECUTIVE_POLL_ERRORS + 1


def test_retry_after_is_read_off_the_wrapped_http_fault():
    """The provider's ``Retry-After`` is parsed from the same ``__cause__``
    chain the transient classifier walks, and only in delta-seconds form."""
    request = httpx.Request("GET", "https://api.example.com/runs/1")

    def _fault(status: int, header: str | None) -> httpx.HTTPStatusError:
        headers = {"Retry-After": header} if header is not None else {}
        return httpx.HTTPStatusError(
            f"HTTP {status}",
            request=request,
            response=httpx.Response(status, headers=headers, request=request),
        )

    # a connector's fail-loud wrap still exposes the header through the cause.
    wrapped = ValueError("GitHub API error (429): rate limited")
    wrapped.__cause__ = _fault(429, "60")
    assert nr._transient_poll_retry_after(wrapped) == 60.0

    # clamped so a broken/hostile header cannot park a run slot.
    assert nr._transient_poll_retry_after(_fault(429, "99999")) == nr._DISPATCH_WAIT_RETRY_AFTER_MAX_SECONDS

    # absent header, HTTP-date form, non-positive, and non-HTTP faults: fall
    # back to the flat poll interval rather than guessing.
    assert nr._transient_poll_retry_after(_fault(429, None)) is None
    assert nr._transient_poll_retry_after(_fault(429, "Wed, 21 Oct 2026 07:28:00 GMT")) is None
    assert nr._transient_poll_retry_after(_fault(429, "0")) is None
    assert nr._transient_poll_retry_after(ValueError("plain")) is None


async def test_a_retry_after_header_is_honoured_as_the_backoff():
    """FAR-1141 FIX 4: the loop waits out the header instead of hammering the
    rate limiter at the flat 1 ms interval (which burned the error budget in
    seconds and turned a polite back-off window into an aborted wait)."""
    request = httpx.Request("GET", "https://api.example.com/runs/1")
    fault = httpx.HTTPStatusError(
        "HTTP 429",
        request=request,
        response=httpx.Response(429, headers={"Retry-After": "0.25"}, request=request),
    )
    connector = _PollConnector([fault, CIRunStatus.SUCCESS])
    with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001):
        started = asyncio.get_running_loop().time()
        state = await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=10)
        elapsed = asyncio.get_running_loop().time() - started

    assert state["status"] == "success"
    assert len(connector.calls) == 2
    # the flat interval is 1 ms: only the header can explain ~250 ms of back-off.
    assert elapsed >= 0.25


async def test_a_slow_poll_loses_the_race_to_the_typed_wait_timeout():
    """A poll slower than the remaining budget must be CUT OFF at the budget and
    terminalise as the typed ``dispatch.wait_timeout``, never as the node's
    generic deadline — and never by waiting out the slow call."""
    connector = _PollConnector([0.8])  # a call that outlives the whole window
    wait_timeout = 0.1
    start = asyncio.get_running_loop().time()
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 5.0),
        pytest.raises(nr.DispatchWaitTimeoutError),
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=wait_timeout)
    elapsed = asyncio.get_running_loop().time() - start
    # the poll is bounded by min(poll_interval, remaining): it cannot consume
    # the stub's full 0.8 s sleep.
    assert elapsed < 0.8


async def test_a_wait_timeout_only_reads_never_fires_a_job():
    connector = _PollConnector()  # never terminal
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001),
        pytest.raises(nr.DispatchWaitTimeoutError),
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=0.05)
    assert connector.calls  # it really polled ...
    assert connector.trigger_calls == 0  # ... and only ever READ


# ---------------------------------------------------------------------------
# iteration 2 (FAR-1141) FIX A — the per-CALL budget is not the poll interval
# ---------------------------------------------------------------------------


async def test_a_status_call_slower_than_the_poll_interval_is_not_cancelled():
    """The call gets its own budget (the connectors' ~30 s HTTP client timeout,
    clamped by the remaining window); the poll INTERVAL only bounds the SLEEP
    between polls.

    Before the fix the call was capped at the interval, so a status endpoint
    slower than ~2 s was cancelled on EVERY attempt, each cancellation counted
    as a consecutive poll error, and the wait failed after the cap while the
    external job was still running.
    """
    connector = _SlowStatusConnector(delay=0.4)  # 8x the patched interval below
    with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.05):
        state = await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=30)
    assert state["status"] == "success"
    assert len(connector.calls) == 1, "the slow call must complete on its own budget, not be cancelled at the interval"


async def test_a_capped_per_call_timeout_never_escapes_with_an_empty_message():
    """A bare ``asyncio.TimeoutError`` renders as ``""`` — an operator would
    read a nameless, causeless failure. When the consecutive-error cap lets one
    through it must carry a message naming the run and the cap that fired."""
    connector = _SlowStatusConnector(delay=0.2)  # far beyond the patched cap
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001),
        patch.object(nr, "_DISPATCH_WAIT_POLL_CALL_TIMEOUT_SECONDS", 0.01),
        pytest.raises(TimeoutError) as excinfo,
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=30)
    message = str(excinfo.value)
    assert message, "the surfaced timeout must not be an empty-message TimeoutError"
    assert "job-1" in message
    assert "per-call cap" in message


# ---------------------------------------------------------------------------
# iteration 2 (FAR-1141) FIX B — permanent faults propagate, never relabelled
# ---------------------------------------------------------------------------


async def test_a_permanent_poll_error_is_never_retried_and_never_relabelled():
    """An ACL denial (raised before any I/O) surfaces on the FIRST poll, as
    itself. The blanket ``except Exception`` used to retry it until the window
    closed and then raise ``dispatch.wait_timeout`` over the top, so an ACL
    denial read as a substrate-side timeout."""
    denial = ConnectorPermissionError("Operation 'trigger_run' is not in allowed_operations")
    connector = _PollConnector([denial])
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001),
        pytest.raises(ConnectorPermissionError, match="not in allowed_operations"),
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=0.05)
    assert len(connector.calls) == 1


async def test_a_connector_contract_value_error_propagates_on_the_first_poll():
    """Jenkins' intentional queue-404 (and every connector's fail-loud
    ``ValueError``) is permanent: retrying cannot resurrect the run, and
    relabelling it would hide the real cause."""
    connector = _PollConnector([ValueError("Jenkins queue item 7 for run job/queue/7 no longer exists")])
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001),
        pytest.raises(ValueError, match="no longer exists"),
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=0.05)
    assert len(connector.calls) == 1


async def test_an_http_4xx_poll_fault_is_permanent():
    """A 4xx is not a fault a second poll can fix — and when a connector wraps
    it (``raise ValueError(...) from HTTPStatusError``) the STATUS in the cause
    chain decides, not the ValueError's presence."""
    connector = _PollConnector([_wrapped_http_status_error(404, "GitHub API error (404): no such run")])
    with (
        patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001),
        pytest.raises(ValueError, match="404"),
    ):
        await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=0.05)
    assert len(connector.calls) == 1


async def test_a_wrapped_5xx_is_still_classified_transient():
    """The same wrap for a 503 MUST be retried — the connectors fail loud by
    wrapping the HTTP status, so classifying only the outer ``ValueError``
    would have abandoned a live run over a recoverable upstream blip."""
    connector = _PollConnector(
        [_wrapped_http_status_error(503, "GitHub API error (503): upstream"), CIRunStatus.SUCCESS]
    )
    with patch.object(nr, "_DISPATCH_WAIT_POLL_INTERVAL_SECONDS", 0.001):
        state = await nr._await_dispatch_terminal(connector, {"id": "job-1"}, wait_timeout=10)
    assert state["status"] == "success"
    assert len(connector.calls) == 2


# ---------------------------------------------------------------------------
# MAJOR 6 — provenance on every dispatch result shape
# ---------------------------------------------------------------------------


def test_stamp_dispatch_provenance_wraps_a_list_result():
    stamped = nr._stamp_dispatch_provenance([{"id": "r-1"}, {"id": "r-2"}], "inst-1")
    assert isinstance(stamped, dict)
    assert stamped["result"] == [{"id": "r-1"}, {"id": "r-2"}]
    assert stamped["witnessed_via"] == "inst-1"
    assert stamped["execution_identity"] == "customer_substrate"
    assert stamped["declared_external_cost"] is None


def test_stamp_dispatch_provenance_stamps_a_dict_in_place():
    stamped = nr._stamp_dispatch_provenance({"id": "job-1", "status": "success"}, "inst-1")
    assert stamped["substrate_status"] == "success"
    assert stamped["execution_identity"] == "customer_substrate"


def test_failed_dispatch_envelope_carries_every_provenance_key():
    """The failure envelope lifts the WHOLE provenance set, not just
    ``substrate_status``, and the splitter keeps it in telemetry."""
    stamped = nr._stamp_dispatch_provenance({"id": "job-1", "status": "failure"}, "inst-1")
    envelope = nr._dispatch_trigger_failure_envelope("node-1", stamped)
    assert envelope is not None
    for key in DISPATCH_PROVENANCE_FIELDS:
        assert key in envelope, key
    value, telemetry = split_node_output(envelope, "dispatch", None)
    assert value is None
    for key in DISPATCH_PROVENANCE_FIELDS:
        assert telemetry[key] == envelope[key], key


async def test_list_runs_node_output_carries_provenance():
    class _ListRunsConnector(_VerbRecorder):
        async def list_runs(self, pipeline_id: str | None = None, status: Any = None, limit: int = 20) -> Any:
            self.verbs.append("list_runs")
            return [CIRun(id="r-1", pipeline_id=pipeline_id or "p", status=CIRunStatus.SUCCESS)]

    connector = _ListRunsConnector()
    set_connector_hub(_RecordingHub(connector))
    try:
        node_def = {
            "id": "list-node",
            "node_type": "dispatch",
            "connector_binding": {
                "instance_id": str(uuid.uuid4()),
                "type": _CI_TYPE,
                "operation": "dispatch",
                "dispatch_action": "list_runs",
            },
        }
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}})
    finally:
        set_connector_hub(None)
    output = result["output"]
    assert isinstance(output, dict), "a list result must be wrapped so provenance has somewhere to live"
    assert output["result"][0]["id"] == "r-1"
    assert output["witnessed_via"] == node_def["connector_binding"]["instance_id"]
    assert output["execution_identity"] == "customer_substrate"
    assert output["declared_external_cost"] is None
