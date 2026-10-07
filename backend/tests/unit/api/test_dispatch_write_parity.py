"""FAR-1141 write-path parity — the REST model and the save-time validator.

Companion to ``tests/unit/pipeline_engine/test_dispatch_guards.py`` (the
engine-side guards). Every test here fails without its fix:

* MAJOR 3 — a ``dispatch`` node saved without an explicit ``operation`` used to
  persist the field default ``"query"`` and silently run a query at run time.
  It now inherits the engine's own fallback; an explicit non-dispatch verb is
  rejected.
* MAJOR 4 — ``await_completion`` on a non-``trigger_run`` action, and
  ``wait_timeout`` without ``await_completion``, are declared-but-unread: both
  are rejected instead of silently no-opping.
* MAJOR 8 — a dispatch binding must target a connector whose type implements
  the four CI-runner operations. Checked on the declared binding type by the
  Pydantic model (REST **and** MCP graph writes both run it) and on the bound
  INSTANCE's own ``connector_type_id`` by the GraphValidator at save time.

No DB, no Docker: the validator's session is an in-memory stub.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from modulo.api.routes.pipelines import PipelineGraphNode
from modulo.core.graph_validator import GraphValidator

_CI_TYPE = "github_actions_ci"


def _dispatch_node_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "dispatch",
        "position": {"x": 0, "y": 0},
        "connector_binding": {
            "type": _CI_TYPE,
            "instance_id": str(uuid.uuid4()),
            "operation": "dispatch",
            "dispatch_action": "trigger_run",
        },
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# MAJOR 3 — the dispatch operation default is coherent, never a silent query
# ---------------------------------------------------------------------------


def test_dispatch_node_without_an_operation_persists_the_dispatch_verb():
    """The field default is ``"query"`` (right for every other node type), so
    persisting it verbatim turned a dispatch node into a plain query."""
    payload = _dispatch_node_payload()
    payload["connector_binding"].pop("operation")
    node = PipelineGraphNode.model_validate(payload)
    assert node.connector_binding is not None
    assert node.connector_binding.operation == "dispatch"
    assert node.model_dump(mode="json")["connector_binding"]["operation"] == "dispatch"


def test_dispatch_node_with_an_explicit_query_operation_is_rejected():
    payload = _dispatch_node_payload()
    payload["connector_binding"]["operation"] = "query"
    with pytest.raises(ValidationError, match=r"operation='dispatch'"):
        PipelineGraphNode.model_validate(payload)


def test_a_non_dispatch_node_keeps_the_query_default():
    """The change must be scoped: an agent node's binding still defaults to query."""
    payload = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "position": {"x": 0, "y": 0},
        "agent_id": str(uuid.uuid4()),
        "connector_binding": {"type": "github", "instance_id": str(uuid.uuid4())},
    }
    node = PipelineGraphNode.model_validate(payload)
    assert node.connector_binding is not None
    assert node.connector_binding.operation == "query"


def test_a_router_node_with_a_job_firing_dispatch_binding_is_persisted_non_idempotent():
    """CRITICAL 1: the forced ``idempotent=false`` was keyed on
    ``node_type == "dispatch"``, so a router (or hitl) node carrying
    ``operation="dispatch"`` + ``trigger_run`` — which the engine executes as a
    connector node — stayed ``idempotent=true`` and every retry considered a
    real external job safe to re-run."""
    payload = {
        "id": str(uuid.uuid4()),
        "node_type": "router",
        "position": {"x": 0, "y": 0},
        "router_config": {"rules": [{"target": str(uuid.uuid4())}]},
        "connector_binding": {
            "type": _CI_TYPE,
            "instance_id": str(uuid.uuid4()),
            "operation": "dispatch",
            "dispatch_action": "trigger_run",
        },
    }
    node = PipelineGraphNode.model_validate(payload)
    assert node.idempotent is False


def test_a_router_node_with_a_read_only_dispatch_binding_stays_idempotent():
    """Read-only dispatch actions re-run safely, so nothing is forced."""
    payload = {
        "id": str(uuid.uuid4()),
        "node_type": "router",
        "position": {"x": 0, "y": 0},
        "router_config": {"rules": [{"target": str(uuid.uuid4())}]},
        "connector_binding": {
            "type": _CI_TYPE,
            "instance_id": str(uuid.uuid4()),
            "operation": "dispatch",
            "dispatch_action": "list_runs",
        },
    }
    node = PipelineGraphNode.model_validate(payload)
    assert node.idempotent is True


# ---------------------------------------------------------------------------
# MAJOR 4 — declared-but-unread wait combinations are rejected
# ---------------------------------------------------------------------------


def test_await_completion_requires_trigger_run():
    payload = _dispatch_node_payload(await_completion=True)
    payload["connector_binding"]["dispatch_action"] = "get_run_status"
    with pytest.raises(ValidationError, match=r"requires connector_binding\.dispatch_action='trigger_run'"):
        PipelineGraphNode.model_validate(payload)


def test_wait_timeout_requires_await_completion():
    payload = _dispatch_node_payload(wait_timeout=60.0)
    payload["connector_binding"]["dispatch_action"] = "trigger_run"
    with pytest.raises(ValidationError, match="wait_timeout requires await_completion=True"):
        PipelineGraphNode.model_validate(payload)


def test_the_coherent_wait_configuration_still_saves():
    node = PipelineGraphNode.model_validate(_dispatch_node_payload(await_completion=True, wait_timeout=60.0))
    assert node.await_completion is True
    assert node.wait_timeout == 60.0
    # and a job-firing dispatch node is still persisted non-idempotent
    assert node.idempotent is False


# ---------------------------------------------------------------------------
# MAJOR 8 — save-time CI-runner checks
# ---------------------------------------------------------------------------


def test_dispatch_binding_to_a_non_ci_connector_type_is_rejected_by_the_model():
    payload = _dispatch_node_payload()
    payload["connector_binding"]["type"] = "linear"
    with pytest.raises(ValidationError, match="does not implement the CI-runner operations"):
        PipelineGraphNode.model_validate(payload)


@pytest.mark.parametrize(
    "type_id",
    [
        # FAR-1141 FIX 3: the two ids _HUB_CI_RUNNER_TYPE_IDS exists for — they
        # are NOT ConnectorType members, so the enum path below cannot reach
        # them; without the set the accept path would reject a binding to the
        # hub's own GitHub Actions / GitLab CI runners.
        "github_actions_ci",
        "gitlab_ci",
        # the enum member, then every other enum type whose capability table
        # carries the four CI-runner operations.
        "ci-runner",
        "circleci",
        "jenkins",
        "teamcity",
        "buildkite",
        "azure_pipelines",
    ],
    ids=[
        "hub-github-actions-ci",
        "hub-gitlab-ci",
        "enum-ci-runner",
        "circleci",
        "jenkins",
        "teamcity",
        "buildkite",
        "azure_pipelines",
    ],
)
def test_dispatch_binding_accepts_every_ci_connector_type(type_id: str):
    payload = _dispatch_node_payload()
    payload["connector_binding"]["type"] = type_id
    assert PipelineGraphNode.model_validate(payload).connector_binding is not None


def test_dispatch_binding_to_the_ci_runner_family_label_is_rejected():
    """``ci_runner`` is the library's family label, not a hub-buildable type id.

    ``connector_hub._build_connector`` has no ``case "ci_runner"`` arm, so a
    dispatch binding persisted against it would raise ``Unknown connector type``
    at run time. The accept path must fail CLOSED on it instead.
    """
    from modulo.connectors.base import connector_type_supports_dispatch

    assert connector_type_supports_dispatch("ci_runner") is False
    payload = _dispatch_node_payload()
    payload["connector_binding"]["type"] = "ci_runner"
    with pytest.raises(ValidationError, match="does not implement the CI-runner operations"):
        PipelineGraphNode.model_validate(payload)


def _session_returning(rows_by_fragment: dict[str, list[Any]]) -> AsyncMock:
    """Session stub: the first row set whose fragment appears in the SQL wins."""
    session = AsyncMock()

    async def _execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(statement)
        rows: list[Any] = []
        for fragment, candidate in rows_by_fragment.items():
            if fragment in text:
                rows = candidate
                break
        scalars = MagicMock()
        scalars.all.return_value = rows
        result = MagicMock()
        result.scalars.return_value = scalars
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _instance(cid: uuid.UUID, type_id: str) -> MagicMock:
    instance = MagicMock()
    instance.id = cid
    instance.name = f"conn-{cid}"
    instance.status = "active"
    instance.allowed_operations = []
    instance.config_json = {}
    instance.connector_type_id = type_id
    return instance


async def _validate_dispatch_graph(type_id: str) -> Any:
    cid = uuid.uuid4()
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "dispatch",
        "position": {"x": 0, "y": 0},
        "connector_binding": {
            "type": type_id,
            "instance_id": str(cid),
            "operation": "dispatch",
            "dispatch_action": "trigger_run",
        },
    }
    graph = {"nodes": [node], "edges": []}
    bindings = [{"node_id": node["id"], "connector_instance_id": str(cid)}]
    session = _session_returning({"connector_instances": [_instance(cid, type_id)]})
    return await GraphValidator().validate_definition(graph, session, connector_bindings=bindings)


async def test_graph_validator_rejects_a_dispatch_binding_to_a_non_ci_instance():
    """The INSTANCE's own ``connector_type_id`` is authoritative — a binding that
    claims ``ci-runner`` but points at a Linear connector fails at save time,
    not at run time with an AttributeError."""
    result = await _validate_dispatch_graph("linear")
    codes = [issue.code for issue in result.issues]
    assert "CONNECTOR_DISPATCH_UNSUPPORTED" in codes


async def test_graph_validator_accepts_a_dispatch_binding_to_a_ci_instance():
    result = await _validate_dispatch_graph(_CI_TYPE)
    assert not any(issue.code == "CONNECTOR_DISPATCH_UNSUPPORTED" for issue in result.issues)


async def test_graph_validator_ignores_dispatch_checks_when_no_binding_is_declared():
    """An empty binding list short-circuits before any DB read (parity with the
    pre-existing empty-binding arm)."""
    session = _session_returning({})
    result = await GraphValidator().validate_definition({"nodes": [{"id": str(uuid.uuid4())}], "edges": []}, session)
    assert not any(issue.code == "CONNECTOR_DISPATCH_UNSUPPORTED" for issue in result.issues)


# ---------------------------------------------------------------------------
# FAR-1141 FIX 2 — a dispatch verb the engine cannot route is rejected
# ---------------------------------------------------------------------------


def _sandbox_dispatch_payload(operation: str = "dispatch") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "agent_prompt": "do the thing",
        "agent_commands": ["echo hi"],
        "connector_binding": {
            "type": _CI_TYPE,
            "instance_id": str(uuid.uuid4()),
            "operation": operation,
            "dispatch_action": "trigger_run",
        },
    }
    return payload


def test_a_dispatch_verb_on_a_sandbox_agent_node_is_rejected():
    """The engine routes a sandbox node through the sandbox factory, never its
    binding — so the dispatch would never fire while the binding read as a
    dispatch. REJECTED at save, not silently coerced to ``query``."""
    with pytest.raises(ValidationError, match="do not route their connector_binding"):
        PipelineGraphNode.model_validate(_sandbox_dispatch_payload())


def test_an_agent_node_with_a_dispatch_binding_is_rejected():
    """Same rule for the other non-routed shape (agent + agent_id), which the
    agent validator already refuses with its own message."""
    payload = _dispatch_node_payload()
    payload["node_type"] = "agent"
    payload["agent_id"] = str(uuid.uuid4())
    with pytest.raises(ValidationError, match=r"cannot carry connector_binding\.operation='dispatch'"):
        PipelineGraphNode.model_validate(payload)


def test_a_query_verb_on_a_sandbox_agent_node_still_saves():
    """The rejection is scoped to ``dispatch`` — a plain query binding on the
    same shape stays valid (the convert-to-agent endpoint persists one)."""
    node = PipelineGraphNode.model_validate(_sandbox_dispatch_payload(operation="query"))
    assert node.connector_binding is not None
    assert node.connector_binding.operation == "query"


# ---------------------------------------------------------------------------
# FAR-1141 FIX 6 — the operation default is single-sourced
# ---------------------------------------------------------------------------


def _raw_dispatch_node() -> dict[str, Any]:
    """A dispatch node exactly as a caller may send it: no ``operation`` key."""
    return {
        "id": str(uuid.uuid4()),
        "node_type": "dispatch",
        "position": {"x": 0, "y": 0},
        "connector_binding": {
            "type": _CI_TYPE,
            "instance_id": str(uuid.uuid4()),
        },
    }


def test_every_write_surface_defaults_the_dispatch_verb_to_the_engine_verdict():
    """The API model and the ``modulo apply`` CLI model must persist EXACTLY the
    verb ``connector_binding_operation`` (the declared single source of truth)
    computes for the same raw node — one rule, one answer, on every surface."""
    from modulo.cli.apply.models import ApplyGraphNode
    from modulo.connectors.base import connector_binding_operation

    raw = _raw_dispatch_node()
    engine_verdict = connector_binding_operation(
        {
            "node_type": raw["node_type"],
            "agent_id": None,
            "connector_binding": dict(raw["connector_binding"]),
        },
    )
    assert engine_verdict == "dispatch"

    api_node = PipelineGraphNode.model_validate(raw)
    assert api_node.connector_binding is not None
    assert api_node.connector_binding.operation == engine_verdict

    apply_node = ApplyGraphNode.model_validate(raw)
    assert apply_node.connector_binding is not None
    assert apply_node.connector_binding.operation == engine_verdict

    # An EXPLICIT non-dispatch verb is never overwritten by the defaulting: the
    # API REJECTS it (it is the single authority that refuses a dispatch node
    # which queries) and the CLI leaves the declared value for the API to
    # reject, so neither surface hard-codes a default over a stated value.
    explicit = _raw_dispatch_node()
    explicit["connector_binding"]["operation"] = "query"
    with pytest.raises(ValidationError, match=r"require connector_binding\.operation='dispatch'"):
        PipelineGraphNode.model_validate(explicit)

    explicit_apply = ApplyGraphNode.model_validate(explicit)
    assert explicit_apply.connector_binding is not None
    assert explicit_apply.connector_binding.operation == "query"


def test_each_write_surface_calls_the_shared_resolver_for_the_default():
    """Structural: none of the three write surfaces may restate the
    ``node_type == "dispatch" -> "dispatch"`` rule as its own literal — each
    must DELEGATE to ``connectors.base.connector_binding_operation``. A surface
    that hand-rolls the default again is exactly the drift FIX 6 removes."""
    import inspect

    from modulo.api import mcp_server
    from modulo.cli.apply.models import ApplyGraphNode

    surfaces = {
        "api/routes/pipelines.py": inspect.getsource(PipelineGraphNode._validate_dispatch_node),
        "cli/apply/models.py": inspect.getsource(ApplyGraphNode._default_dispatch_binding_operation),
        "api/mcp_server.py": inspect.getsource(mcp_server._apply_node_connector_binding),
    }
    for path, source in surfaces.items():
        assert "connector_binding_operation(" in source, (
            f"{path} must derive the operation default from the shared resolver, not a local rule"
        )


# ---------------------------------------------------------------------------
# MAJOR 8 — the instance-level capability check SKIPS unreadable shapes
# ---------------------------------------------------------------------------


class TestDispatchCapabilityCheckSkips:
    """A graph we cannot read, a node without a binding, an unparseable instance
    id and an unresolved instance are all SKIPPED — never guessed at, and never
    a crash. The ordinary binding checks report those shapes."""

    def _run(self, graph_json: Any, found: dict[uuid.UUID, Any] | None = None) -> Any:
        from modulo.core.graph_validator._types import ValidationResult

        result = ValidationResult()
        GraphValidator._check_dispatch_binding_capabilities(graph_json, found or {}, result)
        return result

    def test_a_non_dict_graph_is_skipped(self) -> None:
        for graph in (None, [], "nope", 42):
            assert not self._run(graph).issues

    def test_nodes_not_a_list_is_skipped(self) -> None:
        assert not self._run({"nodes": "nope"}).issues

    def test_a_dispatch_node_without_a_binding_is_skipped(self) -> None:
        result = self._run({"nodes": [{"id": "n1", "node_type": "dispatch"}]})
        assert not any(i.code == "CONNECTOR_DISPATCH_UNSUPPORTED" for i in result.issues)

    def test_a_dispatch_node_with_an_unparseable_instance_id_is_skipped(self) -> None:
        node = {
            "id": "n1",
            "node_type": "dispatch",
            "connector_binding": {"operation": "dispatch", "instance_id": "not-a-uuid"},
        }
        result = self._run({"nodes": [node]})
        assert not any(i.code == "CONNECTOR_DISPATCH_UNSUPPORTED" for i in result.issues)

    def test_a_dispatch_node_whose_instance_is_not_found_is_skipped(self) -> None:
        node = {
            "id": "n1",
            "node_type": "dispatch",
            "connector_binding": {"operation": "dispatch", "instance_id": str(uuid.uuid4())},
        }
        result = self._run({"nodes": [node]})
        assert not any(i.code == "CONNECTOR_DISPATCH_UNSUPPORTED" for i in result.issues)
