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


@pytest.mark.parametrize("type_id", ["ci-runner", "circleci", "jenkins", "teamcity", "buildkite", "azure_pipelines"])
def test_dispatch_binding_accepts_every_ci_connector_type(type_id: str):
    payload = _dispatch_node_payload()
    payload["connector_binding"]["type"] = type_id
    assert PipelineGraphNode.model_validate(payload).connector_binding is not None


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
