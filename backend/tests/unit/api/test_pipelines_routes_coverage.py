"""Route-level coverage tests for the pipeline endpoints (FAR-618).

Complements ``test_pipelines_endpoint.py`` (CRUD + graph happy paths),
``test_pipeline_copy_errors.py`` (clone failures), ``test_pipeline_team_visibility.py``
(team gates) and the per-feature pipeline test modules by covering:

* the graph-write denial translation (``_deny_hitl_review`` /
  ``_handle_graph_write_denials`` — 403/503 mapping + audited denial),
* the ``update_pipeline`` surfaces (team-transition re-validation, autonomy
  audit, graph-in-PATCH path, active-runs 409, 404s, denial mapping),
* restore/archive/unarchive, clone viewer-403 + failure branches,
* the whole ``save-as-composite`` endpoint (parameter-port detection),
* the quality-report endpoint + its helpers,
* the full snapshot suite (list/save-edit/tag/rollback/delete/diff — 404s,
  channel 422, latest-delete 409, non-admin 403),
* folder move (422/404),
* the whole node conversion endpoints (convert-to-agent / revert-to-manual),
* the pure helpers and Pydantic node validators not exercised elsewhere.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.sql import Select

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.api.routes.pipelines import GraphValidationIssue, _finalize_locked_graph_save
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.hitl_review_guard import (
    GuardrailBindingStripDenied,
    HitlReviewWeakeningDenied,
    denial_http_status,
)
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_TEAM_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")

_PREFIX = "modulo.api.routes.pipelines."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline(**overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PIPELINE_ID)
    p.organisation_id = _ORG_ID
    p.name = overrides.get("name", "Test Pipeline")
    p.description = None
    p.visibility = overrides.get("visibility", "org")
    p.owner_team_id = overrides.get("owner_team_id")
    p.folder_id = overrides.get("folder_id")
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = overrides.get("default_autonomy_level", "manual_approval")
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.retry_policy = {}
    p.archived_at = None
    p.snapshot_count = 0
    # FAR-1530: real columns, not auto-Mock attributes — the pause/resume
    # routes branch on the circuit-breaker witness and serialise the run-state
    # columns, so the defaults must be the ordinary row (running, not tripped).
    p.run_enabled = True
    p.run_disabled_reason = None
    p.run_disabled_at = None
    p.circuit_breaker_tripped = False
    p.graph_nodes_json = overrides.get("graph_nodes_json", [])
    p.account_id = _USER_ID
    p.created_by = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalar_one.return_value = 0
    result.scalar.return_value = 0
    result.first.return_value = None
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    result.scalars.return_value.first.return_value = None

    default_result = result

    async def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        if isinstance(stmt, Select) and "FOR UPDATE" in str(stmt).upper() and "FROM pipelines" in str(stmt):
            # #1801: the in-txn team gate's locked row-read — serve an
            # org-visible pipeline so the re-check short-circuits.
            gate_row = MagicMock()
            gate_row.visibility = "org"
            gate_row.owner_team_id = None
            gate_result = MagicMock()
            gate_result.scalar_one_or_none.return_value = gate_row
            return gate_result
        return default_result

    session.execute = AsyncMock(side_effect=_execute)
    session.refresh = AsyncMock(return_value=None)
    return session


def _install_auth(role: str = "admin") -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{role}@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role=role
    )


def _install_common(session: AsyncMock) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    _install_auth("admin")


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_common(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client() -> Generator[TestClient, AsyncMock, None]:
    """A non-admin operator whose pipeline team-scope read returns an org-visible row."""
    session = _make_session()

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        if isinstance(stmt, Select) and "FROM pipelines" in str(stmt):
            if "FOR UPDATE" in str(stmt).upper():
                # #1801: the in-txn team gate's locked row-read — serve an
                # org-visible pipeline so the re-check short-circuits.
                gate_row = MagicMock()
                gate_row.visibility = "org"
                gate_row.owner_team_id = None
                gate_result = MagicMock()
                gate_result.scalar_one_or_none.return_value = gate_row
                return gate_result
            row = MagicMock()
            row.first.return_value = (None, "org")
            return row
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.first.return_value = None
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="operator"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _rls() -> list:
    return [
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ]


def _assert_error_matrix(
    http: TestClient,
    *,
    method: str,
    url: str,
    json_body: dict | None,
    patch_target: str,
    expected: dict,
    query: dict | None = None,
) -> None:
    for exc, status_code in expected:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}{patch_target}", new=AsyncMock(side_effect=exc)))
            for p in _rls():
                stack.enter_context(p)
            kwargs = {"json": json_body} if json_body is not None else {}
            if query:
                kwargs["params"] = query
            resp = getattr(http, method.lower())(url, **kwargs)
        assert resp.status_code == status_code, f"{patch_target} {exc!r}: {resp.text}"


def _start_rls() -> list:
    started = _rls()
    for p in started:
        p.start()
    return started


def _stop_all(started: list) -> None:
    for p in reversed(started):
        p.stop()


def _saved_graph(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    return nodes, edges


def _agent_node_dict(agent_id: uuid.UUID | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(agent_id or uuid.uuid4()),
        "position": {"x": 0.0, "y": 0.0},
        "label": "Agent step",
    }


def _manual_node_dict() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "node_type": "manual",
        "position": {"x": 1.0, "y": 1.0},
        "label": "Manual step",
        "output_schema_id": str(uuid.uuid4()),
    }


def _edge_dict() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source_node_id": str(uuid.uuid4()),
        "target_node_id": str(uuid.uuid4()),
        "edge_type": "normal",
    }


# ---------------------------------------------------------------------------
# Denial translation — audit + HTTP mapping
# ---------------------------------------------------------------------------


async def test_deny_hitl_review_audits_and_raises_403() -> None:
    from modulo.api.routes.pipelines import _deny_hitl_review

    session = _make_session()
    exc = HitlReviewWeakeningDenied(
        reason_code="gate-removal",
        correlation_keys=[("a", "b", "default")],
        weakening_types=["removed"],
        detail="edge removed",
    )
    with (
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock) as audit,
        pytest.raises(HTTPException) as excinfo,
    ):
        await _deny_hitl_review(
            session,
            org_id=_ORG_ID,
            account_id=_USER_ID,
            pipeline_id=_PIPELINE_ID,
            exc=exc,
        )

    assert excinfo.value.status_code == denial_http_status("gate-removal")
    assert "Gate weakening denied (gate-removal)" in str(excinfo.value.detail)
    audit.assert_awaited_once()


async def test_deny_hitl_review_audit_failure_still_raises() -> None:
    from modulo.api.routes.pipelines import _deny_hitl_review

    session = _make_session()
    exc = HitlReviewWeakeningDenied(reason_code="gate-removal")
    with (
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}append_audit_event", new=AsyncMock(side_effect=RuntimeError("audit down"))),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _deny_hitl_review(
            session,
            org_id=_ORG_ID,
            account_id=_USER_ID,
            pipeline_id=_PIPELINE_ID,
            exc=exc,
        )

    assert excinfo.value.status_code == 403


async def test_handle_graph_write_denials_maps_both_types() -> None:
    from modulo.api.routes.pipelines import _handle_graph_write_denials

    session = _make_session()
    principal = MagicMock()
    principal.organisation_id = _ORG_ID
    principal.account_id = _USER_ID
    principal.request_id = None

    hitl_exc = HitlReviewWeakeningDenied(reason_code="gate-removal")
    with (
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _handle_graph_write_denials(session, principal=principal, pipeline_id=_PIPELINE_ID, exc=hitl_exc)
    assert excinfo.value.status_code == 403

    strip_exc = GuardrailBindingStripDenied(stripped_node_ids=["n1"], detail="node n1")
    with pytest.raises(HTTPException) as excinfo:
        await _handle_graph_write_denials(session, principal=principal, pipeline_id=_PIPELINE_ID, exc=strip_exc)
    assert excinfo.value.status_code == denial_http_status(strip_exc.reason_code)


# ---------------------------------------------------------------------------
# Request-model validation helpers
# ---------------------------------------------------------------------------


def test_validate_retry_policy_rejects_malformed() -> None:
    from modulo.api.routes.pipelines import _validate_retry_policy

    assert _validate_retry_policy(None) is None
    with pytest.raises(ValueError, match="bogus"):
        _validate_retry_policy({"on": ["bogus"], "max_retries": 0})


def test_reject_graph_validation_issues_blocks_cap_and_redact() -> None:
    from modulo.api.routes.pipelines import _reject_graph_validation_issues

    issue = MagicMock()
    issue.code = "GUARDRAIL_CAP_EXCEEDED"
    issue.message = "too many guardrails"
    with pytest.raises(HTTPException) as excinfo:
        _reject_graph_validation_issues([issue])
    assert excinfo.value.status_code == 422

    benign = MagicMock()
    benign.code = "ADVISORY"
    _reject_graph_validation_issues([benign])


def test_schema_pin_rejects_non_concrete_versions() -> None:
    from modulo.api.routes.pipelines import SchemaPin

    for bad in ("latest", "*", "", "x" * 51):
        with pytest.raises(ValidationError):
            SchemaPin(schema_id=uuid.uuid4(), schema_version=bad)


def test_graph_node_validator_matrix() -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode

    base = {"id": str(uuid.uuid4()), "position": {"x": 0.0, "y": 0.0}}

    def expect_error(node: dict, fragment: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            PipelineGraphNode.model_validate(node)
        assert fragment in str(excinfo.value)

    expect_error({**base, "node_type": "manual", "agent_id": str(uuid.uuid4())}, "cannot reference an agent")
    expect_error({**base, "node_type": "manual"}, "require an output schema")
    expect_error(
        {**base, "node_type": "manual", "output_schema_id": str(uuid.uuid4())},
        "require a label",
    )
    expect_error({**base, "node_type": "router"}, "require a router_config")
    expect_error({**base, "node_type": "router", "router_config": {}}, "at least one rule")
    expect_error(
        {**base, "node_type": "router", "router_config": {"rules": [{"guard": "x"}]}},
        "requires a target",
    )
    expect_error({**base, "node_type": "hitl"}, "require a hitl_config")
    expect_error({**base, "node_type": "composite"}, "require a composite_ref")
    expect_error(
        {**base, "node_type": "composite", "composite_ref": str(uuid.uuid4()), "agent_id": str(uuid.uuid4())},
        "cannot reference an agent",
    )
    expect_error({**base, "node_type": "agent"}, "require an agent")
    expect_error(
        {
            **base,
            "node_type": "manual",
            "label": "m",
            "output_schema_id": str(uuid.uuid4()),
            "parameter_set_id": str(uuid.uuid4()),
        },
        "Only agent nodes",
    )
    expect_error(
        {
            **base,
            "node_type": "agent",
            "agent_id": str(uuid.uuid4()),
            "read_only": True,
        },
        "Only sandbox_agent nodes",
    )
    expect_error(
        {
            **base,
            "node_type": "agent",
            "agent_id": str(uuid.uuid4()),
            "agent_commands": ["ls"],
        },
        "Only sandbox_agent nodes",
    )
    expect_error(
        {
            **base,
            "node_type": "agent",
            "agent_id": str(uuid.uuid4()),
            "git_credentials": "scoped",
        },
        "Only sandbox_agent nodes",
    )
    expect_error(
        {
            **base,
            "node_type": "agent",
            "agent_id": str(uuid.uuid4()),
            "commands_concatenation_string": " ; ",
        },
        "Only sandbox_agent nodes",
    )
    expect_error(
        {
            **base,
            "node_type": "sandbox_agent",
            "template_id": "opencode",
            "agent_prompt": "Analyse the repo",
            "agent_commands": ["run analysis"],
            "env_vars": {"MODULO_SECRET": "x"},
        },
        "reserved prefix",
    )
    expect_error(
        {
            **base,
            "node_type": "sandbox_agent",
            "template_id": "opencode",
            "agent_prompt": "Analyse the repo",
            "agent_commands": ["run analysis"],
            "context_files": {"../rel": "/abs"},
        },
        "absolute path",
    )


def test_graph_node_sandbox_command_exclusivity_and_missing_template() -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode

    base = {"id": str(uuid.uuid4()), "position": {"x": 0.0, "y": 0.0}, "node_type": "sandbox_agent"}
    # agent_commands with valid list should accept
    node = PipelineGraphNode.model_validate(
        {
            **base,
            "template_id": "opencode",
            "agent_prompt": "Analyse the repo",
            "agent_commands": ["a", "b"],
        }
    )
    assert node.agent_commands == ["a", "b"]

    with pytest.raises(ValidationError) as excinfo:
        PipelineGraphNode.model_validate(
            {**base, "agent_prompt": "Analyse the repo", "agent_commands": ["run analysis"]}
        )
    assert "template_id" in str(excinfo.value)


def test_graph_node_output_pin_mismatch_rejected() -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode

    out_pin = str(uuid.uuid4())
    with pytest.raises(ValidationError) as excinfo:
        PipelineGraphNode.model_validate(
            {
                "id": str(uuid.uuid4()),
                "position": {"x": 0.0, "y": 0.0},
                "node_type": "manual",
                "label": "m",
                "output_schema_id": str(uuid.uuid4()),
                "output_schema_pin": {"schema_id": out_pin, "schema_version": "1.0"},
            }
        )
    assert "does not match output_schema_id" in str(excinfo.value)


def test_pipeline_graph_update_rejects_duplicates_and_overflow() -> None:
    from modulo.api.routes.pipelines import PipelineGraphUpdate

    node = _agent_node_dict()
    dup_node = dict(node)
    with pytest.raises(ValidationError) as excinfo:
        PipelineGraphUpdate.model_validate({"nodes": [node, dup_node], "edges": []})
    assert "must be unique" in str(excinfo.value)

    edge = _edge_dict()
    with pytest.raises(ValidationError) as excinfo:
        PipelineGraphUpdate.model_validate({"nodes": [node], "edges": [edge, dict(edge)]})
    assert "must be unique" in str(excinfo.value)

    edge_b = {**edge, "id": str(uuid.uuid4())}
    with pytest.raises(ValidationError) as excinfo:
        PipelineGraphUpdate.model_validate({"nodes": [node], "edges": [edge, edge_b]})
    assert "paths must be unique" in str(excinfo.value)


def test_graph_response_invalid_data_returns_with_issues() -> None:
    """FAR-874: _graph_response must never 422 on read. Invalid stored data
    is returned with validation_issues instead of raising."""
    from modulo.api.routes.pipelines import _graph_response

    resp = _graph_response([{"id": "not-a-uuid"}], [])
    # Should return a response, not raise
    assert resp is not None
    # Should have validation_issues for the invalid node
    assert resp.validation_issues
    assert resp.validation_issues[0].severity == "warning"


def test_collect_schema_ids_and_pin_builder() -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode, _build_schema_and_backend_pins, _collect_schema_ids

    schema_a = uuid.uuid4()
    schema_b = uuid.uuid4()
    manual = PipelineGraphNode.model_validate(
        {
            "id": str(uuid.uuid4()),
            "node_type": "manual",
            "position": {"x": 0, "y": 0},
            "label": "m",
            "output_schema_id": str(schema_a),
            "input_schema_pin": {"schema_id": str(schema_b), "schema_version": "1.0"},
        }
    )
    collected = _collect_schema_ids([manual])
    assert schema_a in collected
    assert schema_b in collected

    agent_id = uuid.uuid4()
    agent = MagicMock()
    agent.id = agent_id
    agent.input_schema_id = uuid.uuid4()
    agent.output_schema_id = uuid.uuid4()
    agent.model_backend_id = uuid.uuid4()
    agent_node = PipelineGraphNode.model_validate(_agent_node_dict(agent_id))
    schema_pins, backend_pins = _build_schema_and_backend_pins([agent_node, manual], {agent_id: agent})
    assert any(p["direction"] == "input" and p["schema_id"] == str(agent.input_schema_id) for p in schema_pins)
    assert backend_pins == [{"node_id": str(agent_node.id), "model_backend_id": str(agent.model_backend_id)}]
    manual_input_pin = next(p for p in schema_pins if p["node_id"] == str(manual.id) and p["direction"] == "input")
    assert manual_input_pin["schema_version"] == "1.0"


def test_is_admin_and_team_private_helpers() -> None:
    from modulo.api.routes.pipelines import _is_admin, _is_owner_reassignment, _is_team_private

    admin = MagicMock()
    admin.org_role = "admin"
    assert _is_admin(admin)
    operator = MagicMock()
    operator.org_role = "operator"
    assert not _is_admin(operator)

    assert _is_team_private("team", _TEAM_ID)
    assert not _is_team_private("org", _TEAM_ID)
    assert not _is_team_private("team", None)
    assert _is_owner_reassignment(_TEAM_ID, None)
    assert not _is_owner_reassignment(None, _TEAM_ID)


def test_find_node_and_edge_helpers() -> None:
    from modulo.api.routes.pipelines import _edge_to_dict, _find_node_in_list

    node_id = uuid.uuid4()
    node = {"id": node_id, "node_type": "agent"}
    assert _find_node_in_list([node], node_id) is node
    assert _find_node_in_list([{"id": str(node_id)}], node_id)["id"] == str(node_id)
    assert _find_node_in_list([{"node_type": "x"}], node_id) is None
    assert _find_node_in_list([{"id": None}], node_id) is None

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = uuid.uuid4()
    edge.target_node_id = uuid.uuid4()
    edge.edge_type = "default"
    edge.condition_expression = None
    edge.hitl_review_config = {"label": "gate"}
    edge.source_port = "out"
    edge.target_port = "in"
    d = _edge_to_dict(edge)
    assert d["source_node_id"] == str(edge.source_node_id)
    assert d["source_port"] == "out"
    assert d["hitl_review_config"] == {"label": "gate"}


def test_extract_agent_command_sync_updates_rejects_bad_shapes() -> None:
    from modulo.api.routes.pipelines import _extract_agent_command_sync_updates

    agent_id = uuid.uuid4()
    nodes: list[object] = [
        "not-a-dict",
        {"agent_id": agent_id, "agent_commands": ["cmd"]},
        {"agent_id": "not-a-uuid", "agent_commands": ["cmd"]},
        {"agent_id": agent_id, "agent_commands": [""]},
    ]
    updates = _extract_agent_command_sync_updates(nodes)  # type: ignore[arg-type]
    assert updates == {agent_id: ["cmd"]}


def test_endpoint_events_normalisation() -> None:
    from modulo.api.routes.pipelines import _endpoint_events

    assert _endpoint_events(["quality_report"]) == ["quality_report"]
    assert _endpoint_events('["quality_report"]') == ["quality_report"]
    assert not _endpoint_events("{bad json")
    assert not _endpoint_events('{"not": "a list"}')
    assert not _endpoint_events(None)
    assert not _endpoint_events(42)


async def test_quality_report_recipient_urls_filters_subscribers() -> None:
    from modulo.api.routes.pipelines import _quality_report_recipient_urls

    session = _make_session()
    subscribed = MagicMock()
    subscribed.url = "https://hooks.example/one"
    subscribed.events = ["quality_report"]
    other = MagicMock()
    other.url = "https://hooks.example/two"
    other.events = ["run_failed"]
    result = MagicMock()
    result.scalars = MagicMock(return_value=[subscribed, other])
    session.execute = AsyncMock(return_value=result)

    urls = await _quality_report_recipient_urls(session, _ORG_ID)
    assert urls == ["https://hooks.example/one"]


# ---------------------------------------------------------------------------
# Lifecycle endpoints — update/delete/restore/archive/unarchive
# ---------------------------------------------------------------------------


def test_update_pipeline_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(side_effect=HTTPException(status_code=404, detail="x"))),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "x"})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_update_pipeline_write_returns_none_maps_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "x"})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_update_pipeline_programming_error_maps_501(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(side_effect=_PROG)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "x"})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 501


def test_update_pipeline_active_runs_conflict_maps_409(client: tuple[TestClient, AsyncMock]) -> None:
    from modulo.db.crud.pipeline import PipelineHasActiveRunsError

    http, _session = client
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=_make_pipeline())),
        patch(
            f"{_PREFIX}update_pipeline",
            new=AsyncMock(side_effect=PipelineHasActiveRunsError(2)),
        ),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}",
                json={"owner_team_id": str(_TEAM_ID)},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 409, resp.text
    assert "pipeline_has_active_runs" in resp.json()["detail"]
    assert "2 run(s)" in resp.json()["detail"]


def test_update_pipeline_guardrail_strip_denial_maps_403(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=_make_pipeline())),
        patch(
            f"{_PREFIX}update_pipeline",
            new=AsyncMock(side_effect=GuardrailBindingStripDenied(stripped_node_ids=["n1"], detail="n1")),
        ),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "x"})

    assert resp.status_code == 403, resp.text


def test_update_pipeline_autonomy_change_emits_audit(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    current = _make_pipeline()
    previous = _make_pipeline(default_autonomy_level="manual_approval")
    updated = _make_pipeline(default_autonomy_level="fully_autonomous")
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(side_effect=[current, previous])),
        patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock) as audit,
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}",
                json={"default_autonomy_level": "fully_autonomous"},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    audit.assert_awaited_once()


def test_update_pipeline_graph_payload_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    node = _agent_node_dict()
    agent_id = uuid.UUID(node["agent_id"])
    agent = MagicMock()
    agent.id = agent_id
    agent.input_schema_id = uuid.uuid4()
    agent.output_schema_id = uuid.uuid4()
    agent.model_backend_id = uuid.uuid4()
    current = _make_pipeline()
    updated = _make_pipeline(graph_nodes_json=[node])
    saved_nodes, saved_edges = [node], []
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=current)),
        patch(f"{_PREFIX}replace_pipeline_graph", new=AsyncMock(return_value=(saved_nodes, saved_edges))),
        patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
        patch(f"{_PREFIX}_enforce_connector_team_bindings", new_callable=AsyncMock),
        patch(f"{_PREFIX}_load_agents_by_ids", new=AsyncMock(return_value={agent_id: agent})),
        patch(f"{_PREFIX}_load_existing_schema_ids", new=AsyncMock(return_value=set())),
        patch(f"{_PREFIX}_sync_agent_row_commands", new_callable=AsyncMock, return_value=0),
        patch(f"{_PREFIX}_guardrail_config.load_pipeline_guardrail_rows", new_callable=AsyncMock, return_value=[]),
        patch(f"{_PREFIX}GraphValidator.validate_definition", new_callable=AsyncMock) as validate,
    ):
        validate.return_value = MagicMock(issues=[])
        _rls_started = _start_rls()
        try:
            resp = http.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}",
                json={"graph_json": {"nodes": [node], "edges": []}},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    assert resp.json()["node_count"] == 1


def test_update_pipeline_team_visibility_without_owner_rejected(operator_client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = operator_client
    current = _make_pipeline()
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"visibility": "team"})

    assert resp.status_code == 422, resp.text
    assert "owner_team_id is required" in resp.json()["detail"]


def test_update_pipeline_reassignment_to_non_member_team_denied(operator_client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = operator_client
    current = _make_pipeline()
    updated = _make_pipeline(owner_team_id=_TEAM_ID)
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
        patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"owner_team_id": str(_TEAM_ID)})

    assert resp.status_code == 403, resp.text
    assert "not a member" in resp.json()["detail"]


def test_update_pipeline_current_team_non_member_denied(operator_client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = operator_client
    current = _make_pipeline(visibility="team", owner_team_id=_TEAM_ID)
    updated = _make_pipeline(visibility="org", owner_team_id=_TEAM_ID)
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
        patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"visibility": "org"})

    assert resp.status_code == 403, resp.text
    assert "Not a member of the team" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE / restore / archive / unarchive
# ---------------------------------------------------------------------------


def test_delete_pipeline_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}soft_delete_pipeline", new=AsyncMock(return_value=False)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.delete(f"/api/v1/pipelines/{_PIPELINE_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_delete_pipeline_programming_error_maps_501(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}soft_delete_pipeline", new=AsyncMock(side_effect=_PROG)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.delete(f"/api/v1/pipelines/{_PIPELINE_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 501


@pytest.mark.parametrize("action", ["restore", "archive", "unarchive"])
def test_lifecycle_actions_happy_and_missing(client: tuple[TestClient, AsyncMock], action: str) -> None:
    http, _session = client
    crud = {"restore": "restore_pipeline", "archive": "archive_pipeline", "unarchive": "unarchive_pipeline"}[action]
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}{crud}", new=AsyncMock(return_value=_make_pipeline())),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text

    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


@pytest.mark.parametrize("action", ["archive", "unarchive"])
def test_lifecycle_actions_refresh_flushed_row(client: tuple[TestClient, AsyncMock], action: str) -> None:
    """The UPDATE flush expires ``updated_at``; the endpoint must refresh in-txn.

    Regression (staging E2E): ``POST /pipelines/{id}/archive`` and ``/unarchive``
    returned 422 "Data validation failed." because ``archive_pipeline`` /
    ``unarchive_pipeline`` flush an UPDATE whose ``onupdate`` column
    (``updated_at``) is then expired; reading it after commit made Pydantic's
    attribute extraction raise outside the async greenlet, which
    ``handle_db_errors`` maps to 422. Both endpoints now mirror
    ``update_pipeline_endpoint``'s in-transaction ``session.refresh``.

    This is behavioural, not a mere presence-guard: ``session.refresh`` stands
    in for the DB reload that surfaces the DB-computed ``updated_at``, so the
    response body must carry the refreshed value. Without the in-txn refresh the
    endpoint returns the stale ``updated_at`` and this test fails.
    """
    http, session = client
    crud = {"archive": "archive_pipeline", "unarchive": "unarchive_pipeline"}[action]
    refreshed = _make_pipeline()
    db_updated_at = _NOW + timedelta(seconds=5)

    async def _refresh(instance: object) -> None:
        # Mirror the DB-computed onupdate value becoming visible after the
        # in-transaction reload.
        instance.updated_at = db_updated_at

    session.refresh = AsyncMock(side_effect=_refresh)
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=refreshed)),
        patch(f"{_PREFIX}{crud}", new=AsyncMock(return_value=refreshed)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    # The response must carry the DB-computed value the refresh loaded (the
    # behavioural guarantee); the awaited-refresh check is a belt-and-braces
    # signal for the same in-txn reload.
    assert datetime.fromisoformat(resp.json()["updated_at"]) == db_updated_at
    session.refresh.assert_awaited()


@pytest.mark.parametrize("action", ["archive", "unarchive"])
def test_lifecycle_actions_write_none_maps_404(client: tuple[TestClient, AsyncMock], action: str) -> None:
    """``archive_pipeline`` / ``unarchive_pipeline`` returning ``None`` -> 404.

    Covers the shared toggle helper's no-row path: the CRUD write is the only
    thing that can report the row vanished between the gate read and the write.
    """
    http, _session = client
    crud = {"archive": "archive_pipeline", "unarchive": "unarchive_pipeline"}[action]
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}{crud}", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_restore_pipeline_write_none_maps_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}restore_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/restore")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Pause / Resume (FAR-1530: per-pipeline Paused execution state)
# ---------------------------------------------------------------------------


def _make_paused_pipeline() -> MagicMock:
    p = _make_pipeline()
    p.run_enabled = False
    p.run_disabled_reason = "operator"
    p.run_disabled_at = _NOW
    return p


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_run_state_actions_happy_and_missing(client: tuple[TestClient, AsyncMock], action: str) -> None:
    """POST /pipelines/{id}/{pause,resume}: 200 with the run-state response
    fields, 404 when the row is absent (entry filter) or the write misses."""
    http, _session = client
    crud = {"pause": "pause_pipeline", "resume": "resume_pipeline"}[action]
    result = _make_paused_pipeline() if action == "pause" else _make_pipeline()

    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}{crud}", new=AsyncMock(return_value=result)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The response model must carry the run-state triple (the UI badges it and
    # modulo apply's drift hash reads run_enabled from this same payload).
    assert body["run_enabled"] is (action != "pause")
    assert "run_disabled_reason" in body
    assert "run_disabled_at" in body

    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404

    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}{crud}", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/{action}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_resume_refused_while_circuit_breaker_tripped(client: tuple[TestClient, AsyncMock]) -> None:
    """REGRESSION (FAR-1530 trap): operator resume is REFUSED 409 while the
    spend circuit breaker's witness holds.

    Without the in-txn witness check this test FAILS — the resume would clear
    ``run_enabled`` on a tripped pipeline and let spend run away again until
    the next breaker check. The CRUD write must never be reached.
    """
    http, _session = client
    tripped = _make_pipeline()
    tripped.circuit_breaker_tripped = True

    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=tripped)),
        patch(f"{_PREFIX}resume_pipeline", new=AsyncMock()) as resume_mock,
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/resume")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 409, resp.text
    assert "circuit_breaker_tripped" in resp.json()["detail"]
    resume_mock.assert_not_awaited()


def test_pause_does_not_consider_the_circuit_breaker(client: tuple[TestClient, AsyncMock]) -> None:
    """Pausing a tripped pipeline is allowed (idempotent no-op server-side:
    the first cause owns the reason, so the trip's cause is untouched)."""
    http, _session = client
    tripped = _make_paused_pipeline()
    tripped.circuit_breaker_tripped = True

    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=tripped)),
        patch(f"{_PREFIX}pause_pipeline", new=AsyncMock(return_value=tripped)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/pause")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


def test_clone_viewer_denied(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _install_auth("viewer")
    try:
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
    finally:
        _install_auth("admin")

    assert resp.status_code == 403, resp.text


def test_clone_source_missing_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404, resp.text
    assert "Source pipeline not found" in resp.json()["detail"]


def test_clone_name_taken_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}check_pipeline_name_available", new=AsyncMock(return_value=False)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={"name": "Dup"})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 422, resp.text
    assert "already exists" in resp.json()["detail"]


def test_clone_source_disappears_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}check_pipeline_name_available", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}clone_pipeline", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404, resp.text
    assert "disappeared during copy" in resp.json()["detail"]


def test_clone_happy_path_returns_201(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    cloned = _make_pipeline(name="Copy of Test Pipeline")
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}check_pipeline_name_available", new=AsyncMock(return_value=True)),
        patch(f"{_PREFIX}clone_pipeline", new=AsyncMock(return_value=cloned)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "Copy of Test Pipeline"


# ---------------------------------------------------------------------------
# Save as composite
# ---------------------------------------------------------------------------

_COMPOSITE_URL = f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite"


def test_save_as_composite_unknown_pipeline_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(_COMPOSITE_URL, json={"name": "Comp", "selected_node_ids": [str(uuid.uuid4())]})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_save_as_composite_no_valid_nodes_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    pipeline = _make_pipeline(graph_nodes_json=[_agent_node_dict()])
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=pipeline)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(_COMPOSITE_URL, json={"name": "Comp", "selected_node_ids": [str(uuid.uuid4())]})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 422, resp.text
    assert "No valid nodes selected" in resp.json()["detail"]


def test_save_as_composite_happy_path_detects_parameter_ports(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    agent_a_id = uuid.uuid4()
    agent_b_id = uuid.uuid4()
    node_a = _agent_node_dict(agent_a_id)
    node_b = _agent_node_dict(agent_b_id)
    pipeline = _make_pipeline(graph_nodes_json=[node_a, node_b])
    template = MagicMock()
    template.id = uuid.uuid4()
    template.name = "Comp"
    template.version = "0.1.0"

    agent_a = MagicMock()
    agent_a.id = agent_a_id
    agent_a.prompt_template = "Do {{parameter.topic}} then {{parameter.topic}} and {{parameter.mode}}"
    agent_b = MagicMock()
    agent_b.id = agent_b_id
    agent_b.prompt_template = "Plain prompt"

    agents_result = MagicMock()
    agents_result.scalars.return_value.all = MagicMock(return_value=[agent_a, agent_b])
    edges_result = MagicMock()
    edge_row = MagicMock()
    edge_row.id = uuid.uuid4()
    edge_row.source_node_id = uuid.UUID(node_a["id"])
    edge_row.target_node_id = uuid.UUID(node_b["id"])
    edge_row.edge_type = "default"
    edge_row.condition_expression = None
    edge_row.hitl_review_config = None
    edges_result.scalars.return_value.all = MagicMock(return_value=[edge_row])

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        if "FROM agents" in text:
            return agents_result
        if "FROM pipeline_edges" in text or "pipeline_edges" in text:
            return edges_result
        return MagicMock()

    session.execute = AsyncMock(side_effect=_execute)
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch(f"{_PREFIX}create_composite_template", new=AsyncMock(return_value=template)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(
                _COMPOSITE_URL,
                json={"name": "Comp", "selected_node_ids": [node_a["id"], node_b["id"]]},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Comp"
    port_names = [p["name"] for p in body["parameter_ports"]]
    assert port_names == ["topic", "mode"]


def test_save_as_composite_programming_error_maps_501(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(side_effect=_PROG)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(_COMPOSITE_URL, json={"name": "Comp", "selected_node_ids": [str(uuid.uuid4())]})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------

_REPORT = {
    "period": {"start": "2025-01-01", "end": "2025-01-31"},
    "summary": {"pass_rate": 0.9},
    "week_over_week": {"delta": 0.1},
    "trend": [{"week": "1", "pass_rate": 0.9}],
    "eval_breakdown": {"by_eval": {}},
}


def test_quality_report_unknown_pipeline_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/quality-report")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_quality_report_happy_path_with_deliveries(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    endpoint_row = MagicMock()
    endpoint_row.url = "https://hooks.example/qr"
    endpoint_row.events = ["quality_report"]
    endpoints_result = MagicMock()
    endpoints_result.scalars = MagicMock(return_value=[endpoint_row])
    session.execute = AsyncMock(return_value=endpoints_result)
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}generate_quality_report", new=AsyncMock(return_value=dict(_REPORT))),
        patch(
            f"{_PREFIX}deliver_quality_report",
            new=AsyncMock(return_value=[{"url": "https://hooks.example/qr", "status": "delivered"}]),
        ),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/quality-report")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == {"pass_rate": 0.9}
    assert body["deliveries"][0]["status"] == "delivered"


def test_quality_report_programming_error_maps_501(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(side_effect=_PROG)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/quality-report")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

_SNAP_ID = uuid.uuid4()


def _make_snapshot(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.id = _SNAP_ID
    s.pipeline_id = _PIPELINE_ID
    s.snapshot_version = 3
    s.tag = "v3"
    s.notes = None
    s.created_at = _NOW
    s.account_id = _USER_ID
    s.version_kind = "edit"
    s.created_kind = "edit"
    s.draft = False
    s.channel = "none"
    s.graph_json = {"nodes": [], "edges": []}
    s.connector_bindings_json = []
    s.schema_pins_json = []
    s.prompt_pins_json = []
    s.model_backend_pins_json = []
    s.default_autonomy_level = None
    s.run_context_defaults = {}
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def test_list_snapshots_unknown_pipeline_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_list_snapshots_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}list_snapshots", new=AsyncMock(return_value=([_make_snapshot()], 1))),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(_SNAP_ID)


def test_save_edit_snapshot_invalid_channel_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.post(
        f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots",
        json={"channel": "bogus-channel"},
    )

    assert resp.status_code == 422, resp.text
    assert "Invalid release channel" in resp.json()["detail"]


def test_save_edit_snapshot_unknown_pipeline_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots", json={})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_save_edit_snapshot_write_failure_maps_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}create_snapshot_edit", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots", json={"draft": True})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 500, resp.text
    assert "Failed to save edit snapshot" in resp.json()["detail"]


def test_save_edit_snapshot_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_pipeline", new=AsyncMock(return_value=_make_pipeline())),
        patch(f"{_PREFIX}create_snapshot_edit", new=AsyncMock(return_value=_make_snapshot(draft=True))),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots",
                json={"draft": True, "channel": "stable"},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    assert resp.json()["draft"] is True


def test_get_snapshot_detail_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_get_snapshot_detail_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=_make_snapshot())),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    assert resp.json()["graph_json"] == {"nodes": [], "edges": []}


def test_tag_snapshot_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}tag_snapshot", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}", json={"tag": "prod"})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_tag_snapshot_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}tag_snapshot", new=AsyncMock(return_value=_make_snapshot(tag="prod"))),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}",
                json={"tag": "prod", "notes": "release"},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    assert resp.json()["tag"] == "prod"


def test_rollback_snapshot_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}rollback_to_snapshot", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404, resp.text
    assert "Snapshot or pipeline not found" in resp.json()["detail"]


def test_rollback_snapshot_guardrail_strip_denial_maps_403(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(
            f"{_PREFIX}rollback_to_snapshot",
            new=AsyncMock(side_effect=GuardrailBindingStripDenied(stripped_node_ids=["n1"], detail="n1")),
        ),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")

    assert resp.status_code == 403, resp.text


def test_rollback_snapshot_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}rollback_to_snapshot", new=AsyncMock(return_value=_make_snapshot())),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(_SNAP_ID)


def test_delete_snapshot_non_admin_denied(operator_client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = operator_client

    resp = http.delete(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}")

    assert resp.status_code == 403, resp.text
    assert "Only admins can delete snapshots" in resp.json()["detail"]


def test_delete_snapshot_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.delete(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


def test_delete_snapshot_latest_conflict_maps_409(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=_make_snapshot())),
        patch(f"{_PREFIX}delete_snapshot", new=AsyncMock(return_value=False)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.delete(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 409, resp.text
    assert "Cannot delete the latest snapshot" in resp.json()["detail"]


def test_delete_snapshot_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=_make_snapshot())),
        patch(f"{_PREFIX}delete_snapshot", new=AsyncMock(return_value=True)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.delete(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}")
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 204, resp.text


def test_diff_snapshots_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}diff_snapshots", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/diff",
                json={"snapshot_a_id": str(_SNAP_ID), "snapshot_b_id": str(uuid.uuid4())},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404, resp.text


def test_diff_snapshots_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    secret = "raw-secret-that-must-not-leak"
    result = {
        "snapshot_a": {"id": str(_SNAP_ID), "graph": {"nodes": [], "edges": []}},
        "snapshot_b": {"id": str(uuid.uuid4()), "graph": {"nodes": [], "edges": []}},
        "nodes_added": [
            {"id": "n-added", "node_type": "agent", "env_vars": {"OPENAI_API_KEY": secret}},
        ],
        "nodes_removed": [],
        "nodes_modified": [],
        "edges_added": [],
        "edges_removed": [],
        "edges_modified": [],
        "semantic": {},
    }
    with (
        patch(f"{_PREFIX}diff_snapshots", new=AsyncMock(return_value=result)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/diff",
                json={"snapshot_a_id": str(_SNAP_ID), "snapshot_b_id": str(uuid.uuid4())},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot_a"] == {"id": str(_SNAP_ID), "graph": {"nodes": [], "edges": []}}
    # FAR-1181: the diff surface masks credential-bearing node fields.
    assert body["nodes_added"][0]["env_vars"]["OPENAI_API_KEY"] == SENSITIVE_VALUE_MASK
    assert secret not in resp.text


def test_diff_snapshots_non_dict_and_non_list_payloads_pass_through(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """Defensive: only plain dict / list payloads enter the FAR-1181 maskers.

    A mapping that is not a ``dict`` (or a sequence that is not a ``list``) is
    left untouched by the masking step and coerced by the response model — the
    maskers index by key, so they must never receive an arbitrary object.
    """
    from collections import UserDict

    http, _session = client
    result = {
        "snapshot_a": UserDict({"id": str(_SNAP_ID), "graph": {"nodes": [], "edges": []}}),
        "snapshot_b": UserDict({"id": str(uuid.uuid4()), "graph": {"nodes": [], "edges": []}}),
        "nodes_added": (),
        "nodes_removed": (),
        "nodes_modified": [],
        "edges_added": [],
        "edges_removed": [],
        "edges_modified": [],
        "semantic": {},
    }
    with (
        patch(f"{_PREFIX}diff_snapshots", new=AsyncMock(return_value=result)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/diff",
                json={"snapshot_a_id": str(_SNAP_ID), "snapshot_b_id": str(uuid.uuid4())},
            )
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot_a"] == {"id": str(_SNAP_ID), "graph": {"nodes": [], "edges": []}}
    assert not body["nodes_added"]


# ---------------------------------------------------------------------------
# Folder move
# ---------------------------------------------------------------------------


def test_move_pipeline_folder_error_branches(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    moved = _make_pipeline(folder_id=_TEAM_ID)
    with (
        patch(f"{_PREFIX}move_pipeline_to_folder", new=AsyncMock(return_value=moved)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(_TEAM_ID)})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 200, resp.text
    assert resp.json()["folder_id"] == str(_TEAM_ID)

    with (
        patch(f"{_PREFIX}move_pipeline_to_folder", new=AsyncMock(side_effect=ValueError("folder mismatch"))),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(_TEAM_ID)})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 422, resp.text

    with (
        patch(f"{_PREFIX}move_pipeline_to_folder", new=AsyncMock(return_value=None)),
    ):
        _rls_started = _start_rls()
        try:
            resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(_TEAM_ID)})
        finally:
            _stop_all(_rls_started)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Node conversion — convert-to-agent
# ---------------------------------------------------------------------------

_CONVERT_URL = f"/api/v1/pipelines/{_PIPELINE_ID}/nodes"

_CONVERT_BODY_TPL = {
    "agent_id": None,
    "connector_binding": {"type": "github", "instance_id": None},
    "model_backend_id": None,
}


def _convert_body() -> dict:
    body = dict(_CONVERT_BODY_TPL)
    body["agent_id"] = str(uuid.uuid4())
    body["connector_binding"]["instance_id"] = str(uuid.uuid4())
    body["model_backend_id"] = str(uuid.uuid4())
    return body


def _convert_session(agent: object, connector: object, connector_type: str, backend: object) -> AsyncMock:
    session = _make_session()

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        result = MagicMock()
        if "FROM agents" in text:
            result.scalar_one_or_none = MagicMock(return_value=agent)
        elif "FROM connector_instances" in text or "connector_instances" in text:
            if connector is None:
                result.scalar_one_or_none = MagicMock(return_value=None)
            else:
                row = MagicMock()
                row.connector_type_id = connector_type
                result.scalar_one_or_none = MagicMock(return_value=row)
        elif "FROM model_backends" in text or "model_backends" in text:
            result.scalar_one_or_none = MagicMock(return_value=backend)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
            result.scalars.return_value.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


@contextmanager
def _convert_client(session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    _install_auth("admin")
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Node conversion — convert-to-agent
# ---------------------------------------------------------------------------

_CONVERT_URL = f"/api/v1/pipelines/{_PIPELINE_ID}/nodes"


def _convert_body() -> dict:
    return {
        "agent_id": str(uuid.uuid4()),
        "connector_binding": {"type": "github", "instance_id": str(uuid.uuid4())},
        "model_backend_id": str(uuid.uuid4()),
    }


def _convert_session(agent: object, connector: object, connector_type: str, backend: object) -> AsyncMock:
    session = _make_session()

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt)
        result = MagicMock()
        if "FROM agents" in text:
            result.scalar_one_or_none = MagicMock(return_value=agent)
        elif "connector_instances" in text:
            if connector is None:
                result.scalar_one_or_none = MagicMock(return_value=None)
            else:
                row = MagicMock()
                row.connector_type_id = connector_type
                result.scalar_one_or_none = MagicMock(return_value=row)
        elif "model_backends" in text:
            result.scalar_one_or_none = MagicMock(return_value=backend)
        else:
            result.scalar_one_or_none = MagicMock(return_value=None)
            result.scalars.return_value.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _convert_patches(nodes: list[dict], saved: object, *, save_side_effect: object = None) -> list:
    patches = [
        # The in-txn team gate runs FIRST in the handler (its FOR UPDATE row
        # read is what _load_locked_pipeline_graph now consumes); stub it so
        # the strict session double never has to serve it.
        patch(f"{_PREFIX}_reapply_team_gate_inside_mutation_txn", new=AsyncMock(return_value=MagicMock())),
        patch(f"{_PREFIX}_load_locked_pipeline_graph", new=AsyncMock(return_value=(nodes, []))),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ]
    if save_side_effect is not None:
        patches.append(patch(f"{_PREFIX}_save_locked_graph", new=AsyncMock(side_effect=save_side_effect)))
    else:
        patches.append(patch(f"{_PREFIX}_save_locked_graph", new=AsyncMock(return_value=saved)))
    return patches


def test_convert_to_agent_missing_node_returns_404() -> None:
    node_id = uuid.uuid4()
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([], None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 404, resp.text
    assert "Node not found" in resp.json()["detail"]


def test_convert_to_agent_non_manual_node_returns_422() -> None:
    node_id = uuid.uuid4()
    nodes = [_agent_node_dict()]
    nodes[0]["id"] = str(node_id)
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches(nodes, None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 422, resp.text
    assert "Only manual nodes" in resp.json()["detail"]


def test_convert_to_agent_unknown_agent_returns_404() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=None, connector=MagicMock(), connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 404, resp.text
    assert "Agent not found" in resp.json()["detail"]


def test_convert_to_agent_unknown_connector_returns_404() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=MagicMock(), connector=None, connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 404, resp.text
    assert "Connector not found" in resp.json()["detail"]


def test_convert_to_agent_connector_type_mismatch_returns_422() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="gitlab", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 422, resp.text
    assert "Connector type mismatch" in resp.json()["detail"]


def test_convert_to_agent_unknown_model_backend_returns_404() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="github", backend=None)
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 404, resp.text
    assert "Model backend not found" in resp.json()["detail"]


def test_convert_to_agent_happy_path_returns_graph() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    saved_nodes = [_agent_node_dict()]
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], (saved_nodes, [], [])):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"][0]["node_type"] == "agent"


def test_convert_to_agent_surfaces_advisory_validation_issues() -> None:
    """FAR-1277: advisory issues come back in ``validation_issues``, like PATCH /graph.

    ``_save_locked_graph`` logs them AND returns them; the endpoint must pass
    them through to the response instead of dropping them. Blocking codes still
    raise (covered in ``test_node_conversion_save_enforcement.py``) - this is
    only about the non-blocking half the siblings already surface.
    """
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    saved_nodes = [_agent_node_dict()]
    advisory = GraphValidationIssue(
        severity="warning",
        code="guardrail_cap_advisory",
        message="node would exceed the advisory guardrail cap",
        node_id=str(node_id),
    )
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], (saved_nodes, [], [advisory])):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 200, resp.text
    issues = resp.json()["validation_issues"]
    assert issues, "the advisory issue returned by _save_locked_graph must reach the response"
    assert issues[0]["code"] == "guardrail_cap_advisory", issues
    assert issues[0]["severity"] == "warning", issues


def test_convert_to_agent_save_none_maps_404() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Pipeline not found"


def test_convert_to_agent_guardrail_strip_denial_maps_403() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches(
            [manual],
            None,
            save_side_effect=GuardrailBindingStripDenied(stripped_node_ids=[str(node_id)], detail="n"),
        ):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 403, resp.text


def test_convert_to_agent_programming_error_maps_501() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _convert_session(agent=MagicMock(), connector=MagicMock(), connector_type="github", backend=MagicMock())
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _convert_patches([manual], None, save_side_effect=_PROG):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/convert-to-agent", json=_convert_body())

    assert resp.status_code == 501, resp.text


async def test_finalize_locked_graph_save_reraises_unrecognised_error() -> None:
    """A non-denial, non-ProgrammingError exception must propagate unchanged.

    The helper's bare ``raise`` re-raises the *active* exception (the caller's
    ``except`` block), so drive it from within one to match the real contract.
    """
    principal = MagicMock()
    principal.organisation_id = _ORG_ID
    principal.account_id = _USER_ID
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as caught:
        with pytest.raises(RuntimeError, match="kaboom"):
            await _finalize_locked_graph_save(caught, AsyncMock(), principal=principal, pipeline_id=_PIPELINE_ID)


async def test_finalize_locked_graph_save_denies_weakening() -> None:
    """The shared mapping routes a HITL-review-weakening denial to the audit +
    HTTP translation helper (``_deny_hitl_review``) rather than raising raw.

    ``_deny_hitl_review`` is patched here so the helper's own translation
    (tested separately) does not mask this branch. The stub reproduces the real
    helper's contract - it raises UNCONDITIONALLY, so there is no ``return``
    after the ``await`` - and the assertion pins both that the denial path is
    taken and that the mapped HTTP error propagates to the caller rather than
    the helper silently returning.
    """
    principal = MagicMock()
    principal.organisation_id = _ORG_ID
    principal.account_id = _USER_ID
    exc = HitlReviewWeakeningDenied(reason_code="gate-removal")
    mapped = HTTPException(status_code=403, detail="Gate weakening denied.")
    with patch(f"{_PREFIX}_deny_hitl_review", new_callable=AsyncMock) as deny:
        deny.side_effect = mapped
        with pytest.raises(HTTPException) as raised:
            await _finalize_locked_graph_save(exc, AsyncMock(), principal=principal, pipeline_id=_PIPELINE_ID)

    deny.assert_awaited_once()
    assert deny.await_args.kwargs["exc"] is exc
    assert raised.value is mapped


# ---------------------------------------------------------------------------
# Node conversion — revert-to-manual
# ---------------------------------------------------------------------------


def _revert_patches(nodes: list[dict], snapshot: object, saved: object, *, save_side_effect: object = None) -> list:
    patches = [
        # In-txn team gate first, then the graph load (see _convert_patches).
        patch(f"{_PREFIX}_reapply_team_gate_inside_mutation_txn", new=AsyncMock(return_value=MagicMock())),
        patch(f"{_PREFIX}_load_locked_pipeline_graph", new=AsyncMock(return_value=(nodes, []))),
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=snapshot)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
    ]
    if save_side_effect is not None:
        patches.append(patch(f"{_PREFIX}_save_locked_graph", new=AsyncMock(side_effect=save_side_effect)))
    else:
        patches.append(patch(f"{_PREFIX}_save_locked_graph", new=AsyncMock(return_value=saved)))
    return patches


def _revert_snapshot_node(node_id: uuid.UUID, *, output_schema_id: object) -> dict:
    return {
        "id": str(node_id),
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
        "label": "Manual step",
        "output_schema_id": str(output_schema_id),
    }


def _snapshot_with_node(snapshot_node: dict | None) -> MagicMock:
    snapshot = _make_snapshot()
    snapshot.graph_json = {"nodes": [snapshot_node] if snapshot_node else [], "edges": []}
    return snapshot


def test_revert_to_manual_missing_node_returns_404() -> None:
    node_id = uuid.uuid4()
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([], None, None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 404, resp.text
    assert "Node not found" in resp.json()["detail"]


def test_revert_to_manual_non_agent_node_returns_422() -> None:
    node_id = uuid.uuid4()
    manual = _manual_node_dict()
    manual["id"] = str(node_id)
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([manual], None, None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 422, resp.text
    assert "Only agent nodes" in resp.json()["detail"]


def test_revert_to_manual_unknown_snapshot_returns_404() -> None:
    node_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], None, None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 404, resp.text
    assert "Snapshot not found" in resp.json()["detail"]


def test_revert_to_manual_snapshot_node_missing_returns_422() -> None:
    node_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], _snapshot_with_node(None), None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 422, resp.text
    assert "Snapshot does not contain this node" in resp.json()["detail"]


def test_revert_to_manual_snapshot_node_not_manual_returns_422() -> None:
    node_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    snapshot_node = {"id": str(node_id), "node_type": "agent", "position": {"x": 0, "y": 0}}
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], _snapshot_with_node(snapshot_node), None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 422, resp.text
    assert "Snapshot node was not a manual node" in resp.json()["detail"]


def test_revert_to_manual_no_output_schema_returns_422() -> None:
    node_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    snapshot_node = {"id": str(node_id), "node_type": "manual", "position": {"x": 0, "y": 0}}
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], _snapshot_with_node(snapshot_node), None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 422, resp.text
    assert "no output schema" in resp.json()["detail"]


def test_revert_to_manual_happy_path_returns_graph() -> None:
    node_id = uuid.uuid4()
    output_schema_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    snapshot_node = _revert_snapshot_node(node_id, output_schema_id=output_schema_id)
    saved_nodes = [_manual_node_dict()]
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], _snapshot_with_node(snapshot_node), (saved_nodes, [], [])):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 200, resp.text
    assert resp.json()["nodes"][0]["node_type"] == "manual"


def test_revert_to_manual_save_none_maps_404() -> None:
    node_id = uuid.uuid4()
    output_schema_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    snapshot_node = _revert_snapshot_node(node_id, output_schema_id=output_schema_id)
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], _snapshot_with_node(snapshot_node), None):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 404, resp.text


def test_revert_to_manual_programming_error_maps_501() -> None:
    node_id = uuid.uuid4()
    output_schema_id = uuid.uuid4()
    agent_node = _agent_node_dict()
    agent_node["id"] = str(node_id)
    snapshot_node = _revert_snapshot_node(node_id, output_schema_id=output_schema_id)
    session = _make_session()
    with _convert_client(session) as http, ExitStack() as stack:
        for p in _revert_patches([agent_node], _snapshot_with_node(snapshot_node), None, save_side_effect=_PROG):
            stack.enter_context(p)
        for p in _rls():
            stack.enter_context(p)
        resp = http.post(f"{_CONVERT_URL}/{node_id}/revert-to-manual", params={"snapshot_id": str(uuid.uuid4())})

    assert resp.status_code == 501, resp.text


# ---------------------------------------------------------------------------
# Graph reference resolution — capability-scope narrowing check
# ---------------------------------------------------------------------------


async def test_resolve_graph_references_scope_violation_maps_422() -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode, _resolve_graph_references
    from modulo.core.capability_scope import ScopeViolationError

    agent_id = uuid.uuid4()
    node = PipelineGraphNode.model_validate(
        {
            "id": str(uuid.uuid4()),
            "node_type": "agent",
            "agent_id": str(agent_id),
            "position": {"x": 0, "y": 0},
            "capability_scope": {"allowed_connectors": ["github"]},
        }
    )
    agent = MagicMock()
    agent.id = agent_id
    session = _make_session()
    side_effect = ScopeViolationError(node_id="n1", target="github", kind="connector")
    with (
        patch(f"{_PREFIX}_load_agents_by_ids", new=AsyncMock(return_value={agent_id: agent})),
        patch(f"{_PREFIX}validate_allowed_connectors_subset", side_effect=side_effect),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _resolve_graph_references(session, [node], _ORG_ID)

    assert excinfo.value.status_code == 422
    assert "scope.violation" in str(excinfo.value.detail)


async def test_resolve_graph_references_enforces_backend_team_bindings() -> None:
    from modulo.api.routes.pipelines import PipelineGraphNode, _resolve_graph_references

    agent_id = uuid.uuid4()
    node = PipelineGraphNode.model_validate(_agent_node_dict(agent_id))
    agent = MagicMock()
    agent.id = agent_id
    session = _make_session()
    with (
        patch(f"{_PREFIX}_load_agents_by_ids", new=AsyncMock(return_value={agent_id: agent})),
        patch(f"{_PREFIX}_load_existing_schema_ids", new=AsyncMock(return_value=set())),
        patch(
            f"{_PREFIX}_enforce_model_backend_team_bindings",
            new=AsyncMock(side_effect=HTTPException(status_code=409, detail="model_backend_team_mismatch")),
        ) as enforce,
        pytest.raises(HTTPException) as excinfo,
    ):
        await _resolve_graph_references(session, [node], _ORG_ID, pipeline_owner_team_id=_TEAM_ID)

    assert excinfo.value.status_code == 409
    enforce.assert_awaited_once()


def test_update_pipeline_response_carries_rebind_flag(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    current = _make_pipeline()
    updated = _make_pipeline(owner_team_id=_TEAM_ID)
    with (
        patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
        patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"owner_team_id": str(_TEAM_ID)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["connector_rebind_required"] is True
    assert resp.json()["owner_team_id"] == str(_TEAM_ID)
