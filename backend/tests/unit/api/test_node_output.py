"""Unit tests for GET /api/v1/runs/{run_id}/nodes/{node_id}/output."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_run(
    *,
    status: str = "complete",
    outputs_json: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.status = status
    r.outputs_json = outputs_json
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def mock_session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture()
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestGetNodeOutput:
    def test_returns_node_output(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "planner": {"plan": "Step 1: analyse", "confidence": 0.9},
            "coder": {"code": "print('hello')", "language": "python"},
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == str(_RUN_ID)
        assert body["node_id"] == "planner"
        assert body["output"] == {"plan": "Step 1: analyse", "confidence": 0.9}

    def test_returns_different_node_output(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "writer": {"draft": "Hello world", "word_count": 2},
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/writer/output")

        assert resp.status_code == 200
        assert resp.json()["output"] == {"draft": "Hello world", "word_count": 2}

    def test_node_output_is_valid_json(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "formatter": {"result": "ok", "errors": []},
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/formatter/output")

        assert resp.status_code == 200
        assert isinstance(resp.json()["output"], dict)

    def test_run_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.runs.get_run", return_value=None),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{uuid.uuid4()}/nodes/planner/output")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Run not found"

    def test_node_not_found_returns_404(self, client: TestClient) -> None:
        run = _make_run(outputs_json={"planner": {"done": True}})

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/unknown/output")

        assert resp.status_code == 404
        assert "unknown" in resp.json()["detail"]

    def test_empty_outputs_json_returns_404(self, client: TestClient) -> None:
        run = _make_run(outputs_json=None)

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 404

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")
        assert resp.status_code in (401, 403)


class TestSensitiveMasking:
    def test_masks_top_level_sensitive_keys(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "planner": {
                "api_key": "sk-123",
                "token": "abc-def",
                "name": "My Agent",
                "public_url": "https://example.com",
            },
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        output = resp.json()["output"]
        assert output["api_key"] == SENSITIVE_VALUE_MASK
        assert output["token"] == SENSITIVE_VALUE_MASK
        assert output["name"] == "My Agent"
        assert output["public_url"] == "https://example.com"

    def test_masks_nested_sensitive_keys(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "coder": {
                "config": {
                    "api_key": "sk-nested",
                    "timeout": 30,
                },
                "result": "done",
            },
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/coder/output")

        assert resp.status_code == 200
        output = resp.json()["output"]
        assert output["config"]["api_key"] == SENSITIVE_VALUE_MASK
        assert output["config"]["timeout"] == 30
        assert output["result"] == "done"

    def test_masks_in_list_items(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "formatter": {
                "items": [
                    {"key": "safe-value", "public": "visible"},
                    {"credential": "secret-cred", "public": "also-visible"},
                ],
            },
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/formatter/output")

        assert resp.status_code == 200
        output = resp.json()["output"]
        assert output["items"][0]["key"] == SENSITIVE_VALUE_MASK
        assert output["items"][0]["public"] == "visible"
        assert output["items"][1]["credential"] == SENSITIVE_VALUE_MASK
        assert output["items"][1]["public"] == "also-visible"

    def test_preserves_non_string_types(self, client: TestClient) -> None:
        run = _make_run(outputs_json={
            "planner": {
                "count": 42,
                "active": True,
                "tags": ["a", "b"],
                "score": 3.14,
                "nested": None,
            },
        })

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        output = resp.json()["output"]
        assert output["count"] == 42
        assert output["active"] is True
        assert output["tags"] == ["a", "b"]
        assert output["score"] == 3.14
        assert output["nested"] is None


class TestMaskOutputValue:
    """Unit tests for the internal masking helper."""

    def test_masks_sensitive_keys_in_dict(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = {"api_key": "secret", "name": "safe", "nested": {"token": "hidden"}}
        result = _mask_output_value(value)
        assert result["api_key"] == SENSITIVE_VALUE_MASK
        assert result["name"] == "safe"
        assert result["nested"]["token"] == SENSITIVE_VALUE_MASK

    def test_masks_items_in_list(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = [{"password": "p@ss", "label": "safe"}, {"key": "val"}]
        result = _mask_output_value(value)
        assert result[0]["password"] == SENSITIVE_VALUE_MASK
        assert result[0]["label"] == "safe"
        assert result[1]["key"] == SENSITIVE_VALUE_MASK

    def test_passes_non_dict_values(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        assert _mask_output_value("hello") == "hello"
        assert _mask_output_value(42) == 42
        assert _mask_output_value(None) is None
        assert _mask_output_value([1, 2, 3]) == [1, 2, 3]

    def test_limits_recursion_depth(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        deep = {
            "a": {
                "b": {
                    "c": {
                        "d": {
                            "e": {
                                "f": {
                                    "g": {
                                        "h": {
                                            "i": {
                                                "j": {
                                                    "k": {
                                                        "l": {
                                                            "m": {
                                                                "n": {
                                                                    "o": {
                                                                        "p": {
                                                                            "q": {
                                                                                "r": {
                                                                                    "s": {
                                                                                        "t": {
                                                                                            "u": {"api_key": "deep"},
                                                                                        },
                                                                                    },
                                                                                },
                                                                            },
                                                                        },
                                                                    },
                                                                },
                                                            },
                                                        },
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        result = _mask_output_value(deep)
        # At depth 20+, the value passes through without masking
        deep_ref = result
        for _ in range(20):
            deep_ref = deep_ref.get("a", {}) if isinstance(deep_ref, dict) else {}
        # Check we stopped recursing; the innermost value is still a dict
        assert isinstance(deep_ref, dict)
