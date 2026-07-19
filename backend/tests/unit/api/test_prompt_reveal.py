"""Unit tests for POST /api/v1/runs/{run_id}/nodes/{node_id}/prompt/reveal."""

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_AGENT_ID = uuid.uuid4()
_THREAD_ID = f"{_ORG_ID}:{_RUN_ID}"
_NODE_ID = "node-a"
_VALID_32 = "a" * 32
_FERNET_KEY = base64.urlsafe_b64encode(b"\x00" * 32).decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_run(
    status: str = "complete",
    *,
    input_payload: dict[str, Any] | None = None,
    outputs_json: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.status = status
    r.snapshot_id = _SNAPSHOT_ID
    r.langgraph_thread_id = _THREAD_ID
    r.input_payload = input_payload or {"query": "test input"}
    r.outputs_json = outputs_json
    return r


def _make_snapshot(
    *,
    agent_id: uuid.UUID | None = _AGENT_ID,
) -> MagicMock:
    s = MagicMock()
    s.id = _SNAPSHOT_ID
    nodes = [
        {"id": _NODE_ID, "agent_id": str(agent_id) if agent_id else None, "role": None},
        {"id": "node-b", "agent_id": str(uuid.uuid4()) if agent_id else None, "role": None},
    ]
    if agent_id is None:
        nodes[0].pop("agent_id", None)
        nodes[1].pop("agent_id", None)
    s.graph_json = {"nodes": nodes, "edges": []}
    return s


def _make_agent(*, prompt_template: str = "You are a helpful assistant.") -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.prompt_template = prompt_template
    a.template_id = None
    a.agent_command = None
    return a


_session_holder: list[AsyncMock] = []


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    _session_holder.clear()
    _session_holder.append(mock_session)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    mock_engine = MagicMock()

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: mock_engine
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _setup_session_execute(return_value: MagicMock | None) -> None:
    session = _session_holder[0]
    execute_result = AsyncMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=return_value)
    execute_result.fetchone = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=execute_result)


# ---------------------------------------------------------------------------
# Helpers: unit tests
# ---------------------------------------------------------------------------


class TestMaskPromptText:
    def test_masks_api_key_value(self) -> None:
        from modulo.api.routes.runs import _mask_prompt_text

        result = _mask_prompt_text('api_key: "sk-123456"')
        assert SENSITIVE_VALUE_MASK in result
        assert "sk-123456" not in result

    def test_masks_secret_value(self) -> None:
        from modulo.api.routes.runs import _mask_prompt_text

        result = _mask_prompt_text('secret: "my-secret"')
        assert SENSITIVE_VALUE_MASK in result
        assert "my-secret" not in result

    def test_leaves_non_sensitive_untouched(self) -> None:
        from modulo.api.routes.runs import _mask_prompt_text

        text = "Hello world, this is a normal message."
        assert _mask_prompt_text(text) == text

    def test_masks_multiple_sensitive_values(self) -> None:
        from modulo.api.routes.runs import _mask_prompt_text

        text = 'api_key: "sk-123", token: "abc-456"'
        result = _mask_prompt_text(text)
        assert SENSITIVE_VALUE_MASK in result
        assert "sk-123" not in result
        assert "abc-456" not in result


class TestEstimateTokens:
    def test_returns_positive_for_empty(self) -> None:
        from modulo.api.routes.runs import _estimate_tokens

        assert _estimate_tokens("") == 1

    def test_approximates_tokens(self) -> None:
        from modulo.api.routes.runs import _estimate_tokens

        text = "Hello world, this is a test."
        expected = max(1, len(text) // 4)
        assert _estimate_tokens(text) == expected


class TestLookupAgentForNode:
    def test_returns_agent_id_when_found(self) -> None:
        from modulo.api.routes.runs import _lookup_agent_for_node

        graph = {
            "nodes": [
                {"id": "node-a", "agent_id": str(_AGENT_ID)},
                {"id": "node-b", "agent_id": str(uuid.uuid4())},
            ]
        }
        result = _lookup_agent_for_node(graph, "node-a")
        assert result == _AGENT_ID

    def test_returns_none_when_node_has_no_agent(self) -> None:
        from modulo.api.routes.runs import _lookup_agent_for_node

        graph = {"nodes": [{"id": "manual-node"}]}
        result = _lookup_agent_for_node(graph, "manual-node")
        assert result is None

    def test_returns_none_when_node_not_found(self) -> None:
        from modulo.api.routes.runs import _lookup_agent_for_node

        graph = {"nodes": [{"id": "node-a", "agent_id": str(_AGENT_ID)}]}
        result = _lookup_agent_for_node(graph, "nonexistent")
        assert result is None


class TestBuildMessagesFromAgentAndState:
    def test_includes_system_message_from_agent(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        agent = _make_agent(prompt_template="You are a bot.")
        messages = _build_messages_from_agent_and_state(agent, {"q": "hi"}, None, None, "node-a")
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert any("You are a bot." in m["content"] for m in messages)

    def test_includes_user_message_from_input_payload(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        agent = _make_agent()
        messages = _build_messages_from_agent_and_state(agent, {"query": "hello"}, None, None, "node-a")
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert "hello" in user_msgs[0]["content"]

    def test_no_system_message_when_no_agent(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        messages = _build_messages_from_agent_and_state(None, {"q": "hi"}, None, None, "node-a")
        roles = [m["role"] for m in messages]
        assert "system" not in roles

    def test_includes_assistant_messages_from_outputs(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        agent = _make_agent()
        outputs = {"node-b": "previous result"}
        messages = _build_messages_from_agent_and_state(agent, {"q": "hi"}, outputs, None, "node-a")
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "previous result" in assistant_msgs[0]["content"]

    def test_skips_own_node_output(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        agent = _make_agent()
        outputs = {"node-a": "self output", "node-b": "other output"}
        messages = _build_messages_from_agent_and_state(agent, {"q": "hi"}, outputs, None, "node-a")
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert all("self output" not in m["content"] for m in assistant_msgs)
        assert any("other output" in m["content"] for m in assistant_msgs)

    def test_prefers_checkpoint_state_over_input_payload(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        agent = _make_agent()
        checkpoint_state = {"run_context": {"input": {"query": "from checkpoint"}}}
        messages = _build_messages_from_agent_and_state(
            agent, {"query": "from payload"}, None, checkpoint_state, "node-a"
        )
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert "from checkpoint" in user_msgs[0]["content"]

    def test_returns_empty_when_no_data(self) -> None:
        from modulo.api.routes.runs import _build_messages_from_agent_and_state

        messages = _build_messages_from_agent_and_state(None, None, None, None, "node-a")
        assert messages == []


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


class TestRevealNodePrompt:
    def test_reveal_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
        )
        assert resp.status_code in (401, 403)

    def test_reveal_run_not_found_returns_404(self, client: TestClient) -> None:
        _setup_session_execute(None)

        with patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{uuid.uuid4()}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 404

    def test_reveal_node_not_found_returns_404(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run()
        snapshot = _make_snapshot()
        snapshot.graph_json = {"nodes": [{"id": "other-node", "agent_id": str(_AGENT_ID)}], "edges": []}

        def _mock_execute(stmt, *args, **kwargs):
            stmt_str = str(stmt)
            if "PipelineSnapshot" in stmt_str:
                result = AsyncMock()
                result.scalar_one_or_none = MagicMock(return_value=snapshot)
                return result
            if "checkpoints" in stmt_str.lower():
                result = AsyncMock()
                result.fetchone = MagicMock(return_value=None)
                return result
            result = AsyncMock()
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result

        session.execute = AsyncMock(side_effect=_mock_execute)

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/nonexistent-node/prompt/reveal",
            )

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_reveal_snapshot_not_found_returns_404(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run()

        session.execute = AsyncMock(side_effect=self._make_mock_execute(run, None))

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 404
        assert "Snapshot" in resp.json()["detail"]

    def test_reveal_agent_not_found_returns_404(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run()
        snapshot = _make_snapshot()
        snapshot.graph_json = {
            "nodes": [
                {"id": _NODE_ID, "agent_id": str(_AGENT_ID)},
            ],
            "edges": [],
        }

        session.execute = AsyncMock(side_effect=self._make_mock_execute(run, snapshot, agent=None))

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 404
        assert "Agent" in resp.json()["detail"]

    def _make_mock_execute(self, run, snapshot, agent=None, checkpoint_row=None):
        """Helper to build the common mock_execute pattern."""

        def _mock_execute(stmt, *args, **kwargs):
            stmt_str = str(stmt)
            if "pipeline_snapshots" in stmt_str:
                result = AsyncMock()
                result.scalar_one_or_none = MagicMock(return_value=snapshot)
                return result
            if "agents" in stmt_str and agent is not None:
                result = AsyncMock()
                result.scalar_one_or_none = MagicMock(return_value=agent)
                return result
            if "checkpoints" in stmt_str and checkpoint_row is not None:
                result = AsyncMock()
                result.fetchone = MagicMock(return_value=checkpoint_row)
                return result
            if "checkpoints" in stmt_str:
                result = AsyncMock()
                result.fetchone = MagicMock(return_value=None)
                return result
            result = AsyncMock()
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result

        return _mock_execute

    def test_reveal_returns_prompt_with_system_message(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run()
        snapshot = _make_snapshot()
        agent = _make_agent(prompt_template="You are a helpful coding assistant.")

        session.execute = AsyncMock(side_effect=self._make_mock_execute(run, snapshot, agent=agent))

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "prompt" in body
        assert "messages" in body
        assert "token_count" in body
        assert isinstance(body["token_count"], int)
        assert body["token_count"] >= 1

        messages = body["messages"]
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles
        assert any("coding assistant" in m["content"] for m in messages)
        assert any("test input" in m["content"] for m in messages)

    def test_reveal_masks_sensitive_values(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run(input_payload={"api_key": "sk-real-key", "query": "hello"})
        snapshot = _make_snapshot()
        agent = _make_agent(prompt_template="Process the input.")

        session.execute = AsyncMock(side_effect=self._make_mock_execute(run, snapshot, agent=agent))

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 200
        body = resp.json()
        prompt_text = body["prompt"]
        # The actual input value might be masked in the prompt text
        assert "sk-real-key" not in prompt_text or SENSITIVE_VALUE_MASK in prompt_text
        assert "hello" in prompt_text

    def test_reveal_with_checkpoint_state(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run()
        snapshot = _make_snapshot()
        agent = _make_agent()

        checkpoint_data = json.dumps(
            {
                "channel_values": {
                    "run_context": {"input": {"query": "from checkpoint state"}},
                }
            }
        )
        checkpoint_row = (checkpoint_data, "ckp-001")

        session.execute = AsyncMock(
            side_effect=self._make_mock_execute(run, snapshot, agent=agent, checkpoint_row=checkpoint_row)
        )

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 200
        body = resp.json()
        messages = body["messages"]
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert any("from checkpoint state" in m["content"] for m in user_msgs)

    def test_reveal_non_agent_node_returns_without_system(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run()
        snapshot = _make_snapshot(agent_id=None)

        session.execute = AsyncMock(side_effect=self._make_mock_execute(run, snapshot))

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 200
        body = resp.json()
        messages = body["messages"]
        roles = [m["role"] for m in messages]
        assert "system" not in roles
        assert "user" in roles

    def test_reveal_count_tokens_consistently(self, client: TestClient) -> None:
        session = _session_holder[0]
        run = _make_run(input_payload={"query": "short"})
        snapshot = _make_snapshot()
        agent = _make_agent(prompt_template="Short.")

        session.execute = AsyncMock(side_effect=self._make_mock_execute(run, snapshot, agent=agent))

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp1 = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )
            resp2 = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["token_count"] == resp2.json()["token_count"]
        assert resp1.json()["prompt"] == resp2.json()["prompt"]

    def test_reveal_with_encrypted_checkpoint(self, client: TestClient) -> None:
        from cryptography.fernet import Fernet

        session = _session_holder[0]
        run = _make_run()
        snapshot = _make_snapshot()
        agent = _make_agent()

        f = Fernet(_FERNET_KEY.encode())
        checkpoint_body = json.dumps(
            {
                "channel_values": {
                    "run_context": {"input": {"query": "from encrypted checkpoint"}},
                }
            }
        )
        encrypted = f.encrypt(checkpoint_body.encode())
        checkpoint_row = (json.dumps({"__encrypted__": True, "data": encrypted.decode()}), "ckp-002")

        session.execute = AsyncMock(
            side_effect=self._make_mock_execute(run, snapshot, agent=agent, checkpoint_row=checkpoint_row)
        )

        with patch("modulo.api.routes.runs.get_run", return_value=run), patch("modulo.api.routes.runs.set_rls_org"):
            resp = client.post(
                f"/api/v1/runs/{_RUN_ID}/nodes/{_NODE_ID}/prompt/reveal",
            )

        assert resp.status_code == 200
        messages = resp.json()["messages"]
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert any("from encrypted checkpoint" in m["content"] for m in user_msgs)
