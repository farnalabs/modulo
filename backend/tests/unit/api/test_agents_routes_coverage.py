"""Route-level coverage tests for the agent endpoints (FAR-618).

Complements ``test_agents_endpoint.py`` (CRUD happy paths, 404s, generic-agent
criteria) and ``test_agent_prompts.py`` (optimize/apply happy paths) by covering
the per-route DB error-convention matrices (IntegrityError->409/422,
ProgrammingError->501, SQLAlchemyError->503, generic Exception->500), the
``AgentCreate``/``AgentUpdate`` command-field validator 422s, the optimizer
failure surfaces (model-backend 404, credential-decrypt 500,
OptimizationFailedError->500), the ``_llm_call`` content-shape branches, and the
prompt version read/rollback/diff error mappings.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.prompt_optimizer import OptimizationFailedError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_AGENT_ID = uuid.uuid4()
_SCHEMA_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()
_EVAL_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_INTEGRITY = IntegrityError("s", {}, Exception())
_RUNTIME = RuntimeError("kaboom")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=base64_urlsafe(),
        modulo_admin_password="testpass",
    )


def base64_urlsafe() -> str:
    import base64

    return base64.urlsafe_b64encode(b"a" * 32).decode()


def _make_agent() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Prompt Agent"
    a.description = "Test agent"
    a.input_schema_id = _SCHEMA_ID
    a.input_schema_version = "1.0"
    a.output_schema_id = _SCHEMA_ID
    a.output_schema_version = "1.0"
    a.prompt_template = "You are an assistant. Answer: {{query}}"
    a.model_backend_id = _BACKEND_ID
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.max_input_length = None
    a.library_id = None
    a.template_id = None
    a.agent_command = None
    a.agent_commands = None
    a.prompt_always_visible = False
    a.account_id = _USER_ID
    a.required_environment_capabilities = []
    a.created_at = _NOW
    a.updated_at = _NOW
    a.prompt_version_history = []
    return a


_AGENT_BODY = {
    "name": "Test Agent",
    "description": "A test agent for unit tests",
    "input_schema_id": str(_SCHEMA_ID),
    "input_schema_version": "1.0",
    "output_schema_id": str(_SCHEMA_ID),
    "output_schema_version": "1.0",
    "prompt_template": "Hello",
    "model_backend_id": str(_BACKEND_ID),
    "required_environment_capabilities": [],
    "template_id": None,
}

_UPDATE_BODY = {
    "required_environment_capabilities": [],
    "template_id": None,
}

_PREFIX = "modulo.api.routes.agents."


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=default_result)
    session.refresh = AsyncMock(return_value=None)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _rls_patches() -> list:
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
    extra_patches: tuple = (),
) -> None:
    """Run one route against each injected failure and assert the mapped status."""
    for exc, status_code in expected:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}{patch_target}", new=AsyncMock(side_effect=exc)))
            for target, kwargs in extra_patches:
                stack.enter_context(patch(f"{_PREFIX}{target}", **kwargs))
            for p in _rls_patches():
                stack.enter_context(p)
            kwargs = {"json": json_body} if json_body is not None else {}
            resp = getattr(http, method.lower())(url, **kwargs)
        assert resp.status_code == status_code, f"{patch_target} {exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# Request-model validators
# ---------------------------------------------------------------------------


def test_agent_create_rejects_command_and_commands(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    body = {**_AGENT_BODY, "agent_command": "run.sh", "agent_commands": ["a", "b"]}

    resp = http.post("/api/v1/agents", json=body)

    assert resp.status_code == 422, resp.text


def test_agent_update_rejects_command_and_commands(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    body = {**_UPDATE_BODY, "agent_command": "run.sh", "agent_commands": ["a", "b"]}

    resp = http.patch(f"/api/v1/agents/{_AGENT_ID}", json=body)

    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# CRUD error matrices
# ---------------------------------------------------------------------------


def test_list_agents_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url="/api/v1/agents",
        json_body=None,
        patch_target="list_agents",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_create_agent_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="POST",
        url="/api/v1/agents",
        json_body=_AGENT_BODY,
        patch_target="create_agent",
        expected={(_INTEGRITY, 422), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_get_agent_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url=f"/api/v1/agents/{_AGENT_ID}",
        json_body=None,
        patch_target="get_agent",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_update_agent_read_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="PATCH",
        url=f"/api/v1/agents/{_AGENT_ID}",
        json_body=_UPDATE_BODY,
        patch_target="get_agent",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_update_agent_write_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="PATCH",
        url=f"/api/v1/agents/{_AGENT_ID}",
        json_body=_UPDATE_BODY,
        patch_target="update_agent",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_agent", {"new": AsyncMock(return_value=_make_agent())}),),
    )


def test_update_agent_write_returns_none_maps_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_PREFIX}update_agent", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.patch(f"/api/v1/agents/{_AGENT_ID}", json=_UPDATE_BODY)

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Agent not found"


def test_apply_prompt_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="POST",
        url=f"/api/v1/agents/{_AGENT_ID}/prompts/v2/apply",
        json_body={"suggested_prompt": "New prompt"},
        patch_target="add_prompt_version",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_agent", {"new": AsyncMock(return_value=_make_agent())}),),
    )


def test_list_prompt_versions_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url=f"/api/v1/agents/{_AGENT_ID}/prompts",
        json_body=None,
        patch_target="get_agent",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_list_prompt_versions_unknown_agent_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/prompts")

    assert resp.status_code == 404, resp.text


def test_get_prompt_version_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="GET",
        url=f"/api/v1/agents/{_AGENT_ID}/prompts/v1",
        json_body=None,
        patch_target="get_prompt_version",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_agent", {"new": AsyncMock(return_value=_make_agent())}),),
    )


def test_get_prompt_version_unknown_version_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_PREFIX}get_prompt_version", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.get(f"/api/v1/agents/{_AGENT_ID}/prompts/v99")

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Version not found"


def test_rollback_prompt_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="PUT",
        url=f"/api/v1/agents/{_AGENT_ID}/prompts/rollback/v1",
        json_body=None,
        patch_target="rollback_prompt_version",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_agent", {"new": AsyncMock(return_value=_make_agent())}),),
    )


def test_rollback_prompt_unknown_version_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_PREFIX}rollback_prompt_version", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.put(f"/api/v1/agents/{_AGENT_ID}/prompts/rollback/v99")

    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"]


def test_diff_prompt_versions_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="POST",
        url=f"/api/v1/agents/{_AGENT_ID}/prompts/diff",
        json_body={"version_a": "current", "version_b": "v1"},
        patch_target="get_agent",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_diff_prompt_versions_unknown_version_b_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    agent = _make_agent()
    agent.prompt_version_history = []
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=agent)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        resp = http.post(
            f"/api/v1/agents/{_AGENT_ID}/prompts/diff",
            json={"version_a": "current", "version_b": "v99"},
        )

    assert resp.status_code == 404, resp.text
    assert "v99 not found" in resp.json()["detail"]


def test_delete_agent_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="DELETE",
        url=f"/api/v1/agents/{_AGENT_ID}",
        json_body=None,
        patch_target="delete_agent",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_agent", {"new": AsyncMock(return_value=_make_agent())}),),
    )


def test_delete_agent_returns_false_maps_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}get_agent", new=AsyncMock(return_value=_make_agent())),
        patch(f"{_PREFIX}delete_agent", new=AsyncMock(return_value=False)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.delete(f"/api/v1/agents/{_AGENT_ID}")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# optimize_prompt — error blocks and failure surfaces
# ---------------------------------------------------------------------------


_OPTIMIZE_URL = f"/api/v1/agents/{_AGENT_ID}/prompts/current/optimize"

_EVAL_RESULTS = [
    {
        "id": str(uuid.uuid4()),
        "eval_id": str(_EVAL_ID),
        "run_id": str(uuid.uuid4()),
        "passed": False,
        "score": 0.0,
        "detail": "Too brief",
    }
]
_EVAL_DEFS = {str(_EVAL_ID): {"id": str(_EVAL_ID), "name": "Brevity", "eval_type": "regex", "config_json": {}}}


def _optimize_stubs(*, agent: MagicMock | None = None, eval_results: object = None) -> dict[str, AsyncMock]:
    """Default stubs for the optimize happy path up to the model-backend query."""
    agent = agent if agent is not None else _make_agent()
    eval_results = eval_results if eval_results is not None else _EVAL_RESULTS
    return {
        "get_agent": AsyncMock(return_value=agent),
        "get_eval_results_with_defs": AsyncMock(return_value=(eval_results, _EVAL_DEFS)),
    }


def test_optimize_get_agent_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503)]:
        with (
            patch(f"{_PREFIX}get_agent", new=AsyncMock(side_effect=exc)),
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
            patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})
        assert resp.status_code == expected, resp.text


def test_optimize_eval_results_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503)]:
        stubs = _optimize_stubs()
        with (
            patch(f"{_PREFIX}get_agent", stubs["get_agent"]),
            patch(f"{_PREFIX}get_eval_results_with_defs", new=AsyncMock(side_effect=exc)),
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
            patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})
        assert resp.status_code == expected, resp.text


def test_optimize_model_backend_query_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503)]:
        session.execute = AsyncMock(side_effect=exc)
        stubs = _optimize_stubs()
        with (
            patch(f"{_PREFIX}get_agent", stubs["get_agent"]),
            patch(f"{_PREFIX}get_eval_results_with_defs", stubs["get_eval_results_with_defs"]),
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
            patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})
        assert resp.status_code == expected, resp.text
        session.execute = _make_session().execute


def test_optimize_model_backend_not_found_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    """The default session result has scalar_one_or_none -> None: no backend row."""
    http, _session = client
    stubs = _optimize_stubs()
    with (
        patch(f"{_PREFIX}get_agent", stubs["get_agent"]),
        patch(f"{_PREFIX}get_eval_results_with_defs", stubs["get_eval_results_with_defs"]),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Model backend not found"


def test_optimize_credential_decrypt_failure_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb.provider = "openai"
    mb.model_id = "gpt-test"
    mb.default_params = {}
    mb_result = MagicMock()
    mb_result.scalar_one_or_none.return_value = mb
    session.execute = AsyncMock(return_value=mb_result)
    stubs = _optimize_stubs()
    sb = AsyncMock()
    sb.get_secret = AsyncMock(side_effect=KeyError("missing"))
    with (
        patch(f"{_PREFIX}get_agent", stubs["get_agent"]),
        patch(f"{_PREFIX}get_eval_results_with_defs", stubs["get_eval_results_with_defs"]),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        patch(f"{_PREFIX}create_secrets_backend", return_value=sb),
    ):
        resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 500, resp.text
    assert "Failed to decrypt model backend credentials" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("exc", "detail_fragment"),
    [
        (OptimizationFailedError("llm down"), "LLM call failed after retries"),
        (ValueError("bad shape"), "failed unexpectedly"),
    ],
    ids=["optimization-failed", "unexpected"],
)
def test_optimize_optimizer_failure_returns_500(
    client: tuple[TestClient, AsyncMock],
    exc: Exception,
    detail_fragment: str,
) -> None:
    http, session = client
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb_result = MagicMock()
    mb_result.scalar_one_or_none.return_value = mb
    session.execute = AsyncMock(return_value=mb_result)
    stubs = _optimize_stubs()
    sb = AsyncMock()
    sb.get_secret = AsyncMock(return_value=json.dumps({"api_key": "sk"}))
    optimizer = MagicMock()
    optimizer.optimize = AsyncMock(side_effect=exc)
    with (
        patch(f"{_PREFIX}get_agent", stubs["get_agent"]),
        patch(f"{_PREFIX}get_eval_results_with_defs", stubs["get_eval_results_with_defs"]),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        patch(f"{_PREFIX}create_secrets_backend", return_value=sb),
        patch("modulo.core.model_backend_hub._build_backend", return_value=AsyncMock()),
        patch(f"{_PREFIX}PromptOptimizer", return_value=optimizer),
    ):
        resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 500, resp.text
    assert detail_fragment in resp.json()["detail"]


def test_optimize_llm_call_handles_string_and_list_content(client: tuple[TestClient, AsyncMock]) -> None:
    """The ``_llm_call`` closure joins list-part content and stringifies scalars."""
    http, session = client
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb_result = MagicMock()
    mb_result.scalar_one_or_none.return_value = mb
    session.execute = AsyncMock(return_value=mb_result)
    stubs = _optimize_stubs()
    sb = AsyncMock()
    sb.get_secret = AsyncMock(return_value=json.dumps({"api_key": "sk"}))
    optimizer = MagicMock()
    optimizer.optimize = AsyncMock(return_value=MagicMock(suggested_prompt="s", rationale="r", analysis="a"))
    backend = AsyncMock()
    backend.invoke = AsyncMock(return_value=MagicMock(content="plain reply"))
    captured: list = []

    def _capture_llm_call(llm_call: object) -> MagicMock:
        captured.append(llm_call)
        return optimizer

    with (
        patch(f"{_PREFIX}get_agent", stubs["get_agent"]),
        patch(f"{_PREFIX}get_eval_results_with_defs", stubs["get_eval_results_with_defs"]),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
        patch(f"{_PREFIX}create_secrets_backend", return_value=sb),
        patch("modulo.core.model_backend_hub._build_backend", return_value=backend),
        patch(f"{_PREFIX}PromptOptimizer", side_effect=_capture_llm_call),
    ):
        resp = http.post(_OPTIMIZE_URL, json={"eval_result_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 200, resp.text
    llm_call = captured[0]
    plain_message = MagicMock()
    assert asyncio.run(llm_call([plain_message])) == "plain reply"
    backend.invoke.return_value = MagicMock(content=[{"text": "part1"}, "part2"])
    part_message = MagicMock()
    assert asyncio.run(llm_call([part_message])) == "part1part2"
