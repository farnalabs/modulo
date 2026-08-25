"""Unit tests for GET /api/v1/runs/{run_id}/nodes/{node_id}/output."""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
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
    node_telemetry_json: dict[str, Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = _RUN_ID
    r.status = status
    r.outputs_json = outputs_json
    r.node_telemetry_json = node_telemetry_json
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def mock_session() -> AsyncMock:
    return _make_mock_session()


@pytest.fixture
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

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestGetNodeOutput:
    def test_returns_node_output(self, client: TestClient) -> None:
        run = _make_run(
            outputs_json={
                "planner": {"plan": "Step 1: analyse", "confidence": 0.9},
                "coder": {"code": "print('hello')", "language": "python"},
            }
        )

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
        run = _make_run(
            outputs_json={
                "writer": {"draft": "Hello world", "word_count": 2},
            }
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/writer/output")

        assert resp.status_code == 200
        assert resp.json()["output"] == {"draft": "Hello world", "word_count": 2}

    def test_node_output_is_valid_json(self, client: TestClient) -> None:
        run = _make_run(
            outputs_json={
                "formatter": {"result": "ok", "errors": []},
            }
        )

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


class TestSplitRowOutput:
    """Per-node endpoint behavior for P1+ (split) runs — pure returns and the
    derived-status fallback for telemetry-only nodes (FAR-126 P2a)."""

    def test_returns_pure_return_for_split_run(self, client: TestClient) -> None:
        pure_return = {"plan": "Step 1: analyse", "confidence": 0.9}
        run = _make_run(
            outputs_json={"planner": pure_return},
            node_telemetry_json={
                "planner": {
                    "status": "completed",
                    "summary": "planned",
                    "agent_stdout": "log",
                    "wall_clock_time_ms": 1200,
                }
            },
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        assert resp.json()["output"] == pure_return

    def test_none_return_with_telemetry_returns_derived_status(self, client: TestClient) -> None:
        # A skipped node has NO outputs entry — only telemetry. The endpoint
        # must NOT 404; it returns a derived {status, summary} object.
        run = _make_run(
            outputs_json={},
            node_telemetry_json={
                "planner": {"status": "skipped", "summary": "Skipped: missing input fields"},
            },
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        assert resp.json()["output"] == {
            "status": "skipped",
            "summary": "Skipped: missing input fields",
        }

    def test_recovered_node_in_telemetry_no_404(self, client: TestClient) -> None:
        run = _make_run(
            outputs_json={},
            node_telemetry_json={"planner": {"recovered": True, "recovery_input": {"review": "LGTM"}}},
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        output = resp.json()["output"]
        # Derived surface only — recovery_input must not leak.
        assert output == {}

    def test_derived_status_never_leaks_telemetry(self, client: TestClient) -> None:
        secret = "sk-leaked-in-stdout"
        run = _make_run(
            outputs_json={},
            node_telemetry_json={
                "planner": {
                    "status": "failed",
                    "summary": "agent errored",
                    "agent_stdout": f"token issued: {secret}",
                    "sandbox_log_tail": secret,
                }
            },
        )

        with (
            patch("modulo.api.routes.runs.get_run", return_value=run),
            patch("modulo.api.routes.runs.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/runs/{_RUN_ID}/nodes/planner/output")

        assert resp.status_code == 200
        body = resp.json()["output"]
        assert body == {"status": "failed", "summary": "agent errored"}
        assert "agent_stdout" not in body
        assert "sandbox_log_tail" not in body
        assert secret not in json.dumps(body)


class TestSensitiveMasking:
    def test_masks_top_level_sensitive_keys(self, client: TestClient) -> None:
        run = _make_run(
            outputs_json={
                "planner": {
                    "api_key": "sk-123",
                    "token": "abc-def",
                    "name": "My Agent",
                    "public_url": "https://example.com",
                },
            }
        )

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
        run = _make_run(
            outputs_json={
                "coder": {
                    "config": {
                        "api_key": "sk-nested",
                        "timeout": 30,
                    },
                    "result": "done",
                },
            }
        )

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
        run = _make_run(
            outputs_json={
                "formatter": {
                    "items": [
                        {"key": "safe-value", "public": "visible"},
                        {"credential": "secret-cred", "public": "also-visible"},
                    ],
                },
            }
        )

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
        run = _make_run(
            outputs_json={
                "planner": {
                    "count": 42,
                    "active": True,
                    "tags": ["a", "b"],
                    "score": 3.14,
                    "nested": None,
                },
            }
        )

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
        assert output["score"] == pytest.approx(3.14)
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

        deep = {"a": {}}
        inner = deep["a"]
        for _ in range(20):
            inner["a"] = {}
            inner = inner["a"]
        inner["api_key"] = "deep"
        result = _mask_output_value(deep)
        # At depth 21+, the value passes through without masking
        deep_ref = result
        for _ in range(21):
            deep_ref = deep_ref.get("a", {}) if isinstance(deep_ref, dict) else {}
        # Check we stopped recursing; the innermost value was NOT masked
        assert deep_ref.get("api_key") == "deep"


class TestMaskOutputValueSecretValues:
    """FAR-392: value-pattern masking catches secrets regardless of key name."""

    def test_masks_secret_value_under_non_sensitive_key(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = {"result": "the key is AKIAIOSFODNN7EXAMPLE and also sk-Zk9f2Lm8QpXr4Tn6WbVc"}
        result = _mask_output_value(value)
        assert SENSITIVE_VALUE_MASK in result["result"]
        assert "AKIAIOSFODNN7EXAMPLE" not in result["result"]
        assert "sk-Zk9f2Lm8QpXr4Tn6WbVc" not in result["result"]

    def test_masks_secret_value_in_free_text(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = "logs show token eyJhbGciOiJIUzI1Ni.eyJzdWIiOiIxMjM0NTY3ODk.wiOWw40dij8"
        result = _mask_output_value(value)
        assert SENSITIVE_VALUE_MASK in result
        assert "eyJhbGciOiJIUzI1Ni" not in result

    def test_masks_private_key_block(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD\n-----END RSA PRIVATE KEY-----"
        value = {"debug": key}
        result = _mask_output_value(value)
        assert SENSITIVE_VALUE_MASK in result["debug"]
        assert "BEGIN RSA PRIVATE KEY" not in result["debug"]

    def test_masks_connection_string_password(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = {"note": "postgres://app_user:S3cr3tP@ss@db.example.com:5432/app"}
        result = _mask_output_value(value)
        # The whole password (including the '@'-separated tail) must be masked.
        assert "S3cr3tP" not in result["note"]
        assert "ss" not in result["note"]
        assert "app_user" in result["note"]
        assert "db.example.com:5432/app" in result["note"]

    def test_masks_connection_string_password_with_at_in_password(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        # Password itself contains '@' — the greedy capture must consume up to
        # the FINAL '@' (host separator), not the first one.
        value = {"note": "postgres://u:pa@ss@host:5432/db"}
        result = _mask_output_value(value)
        assert "pa@ss" not in result["note"]
        assert "host:5432/db" in result["note"]

    def test_masks_connection_string_password_with_colon_slash(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        # Password contains ':' and '/' — must be wholly masked.
        value = {"note": "mysql://root:P@ass:word!@1.2.3.4/app"}
        result = _mask_output_value(value)
        assert "P@ass:word!" not in result["note"]
        assert "1.2.3.4/app" in result["note"]

    def test_masks_bearer_token_in_free_text(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = "Authorization failed: Bearer ya29.freshtokenvalue1234567890"
        result = _mask_output_value(value)
        assert "ya29.freshtokenvalue1234567890" not in result
        assert SENSITIVE_VALUE_MASK in result

    def test_does_not_overmask_legitimate_content(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = {
            "summary": "processed 42 records successfully",
            "url": "https://example.com/page",
            "contact": "reach us at support@example.com",
            "port": "http://localhost:8080/health",
            "score": 3.14,
        }
        result = _mask_output_value(value)
        assert result["summary"] == "processed 42 records successfully"
        assert result["url"] == "https://example.com/page"
        assert result["contact"] == "reach us at support@example.com"
        assert result["port"] == "http://localhost:8080/health"
        assert result["score"] == pytest.approx(3.14)

    def test_does_not_overmask_host_port_followed_by_email(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        # A ``scheme://host:PORT`` with no credentials, followed later in the SAME
        # string by an unrelated ``@`` (an email), must keep the benign span
        # between the port and the email intact. Regression for the greedy
        # ``(.*)`` capture that wiped everything up to the final ``@``.
        value = {"note": "curl http://127.0.0.1:8080 health check ok; contact admin@example.com for help"}
        result = _mask_output_value(value)
        assert result["note"] == value["note"]

        value2 = {"note": "server at http://localhost:9090; engineer joe@corp.io on call; next deploy 18:00"}
        result2 = _mask_output_value(value2)
        assert result2["note"] == value2["note"]

        # A genuine credential-bearing connection string is still masked.
        secret = {"note": "postgres://u:pa@ss@host:5432/db"}
        result3 = _mask_output_value(secret)
        assert "pa@ss" not in result3["note"]

    def test_masks_secret_in_nested_list_item(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = [{"message": "xoxb-1234567890-abcdefghij-klmnopqrstuvwxyz"}]
        result = _mask_output_value(value)
        assert SENSITIVE_VALUE_MASK in result[0]["message"]
        assert "xoxb-1234567890" not in result[0]["message"]

    def test_masks_fine_grained_github_pat(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        token = "github_pat_" + "A" * 50
        value = {"log": f"cloned with {token}"}
        result = _mask_output_value(value)
        assert SENSITIVE_VALUE_MASK in result["log"]
        assert token not in result["log"]

    def test_masks_aws_temp_access_key(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        value = {"log": "temp creds ASIAABCDEFGHIJKLMNOP leaked"}
        result = _mask_output_value(value)
        assert SENSITIVE_VALUE_MASK in result["log"]
        assert "ASIAABCDEFGHIJKLMNOP" not in result["log"]

    def test_does_not_mask_short_github_pat_like_string(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        # github_pat_ with a short (non-secret) suffix must NOT be masked by the
        # value pattern (it requires 50+ chars after the prefix).
        value = {"note": "github_pat_11ABC"}
        result = _mask_output_value(value)
        assert result["note"] == "github_pat_11ABC"

    def test_does_not_truncate_long_non_secret_string(self) -> None:
        from modulo.api.routes.runs import _mask_output_value
        from modulo.core.secret_patterns import mask_secret_values_in_text

        # Regression: a long, benign string must be returned in full — the
        # masking helper must not silently truncate content beyond 5000 chars.
        long_plain = "plain " + "x" * 8000
        assert mask_secret_values_in_text(long_plain) == long_plain
        result = _mask_output_value(long_plain)
        assert result == long_plain
        assert len(result) == len(long_plain)

    def test_masks_secret_in_long_string_without_truncating_tail(self) -> None:
        from modulo.api.routes.runs import _mask_output_value
        from modulo.core.secret_patterns import mask_secret_values_in_text

        # A secret near the start of a long string must be masked while the
        # trailing benign content is preserved in full.
        token = "eyJaaa.eyJbbb.ccc"
        long_value = f"token {token} leaked " + "y" * 8000
        masked = mask_secret_values_in_text(long_value)
        assert token not in masked
        assert masked.endswith("y" * 8000)
        assert len(masked) == len(long_value) - len(token) + len(SENSITIVE_VALUE_MASK)
        assert _mask_output_value(long_value) == masked

    def test_masks_private_key_straddling_slice_boundary(self) -> None:
        from modulo.core.secret_patterns import mask_secret_values_in_text

        # A private key whose block begins in one 5000-char slice and ends in the
        # next must still be masked in full (regression: it was contained by
        # neither non-overlapping slice and leaked unmasked).
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD\n-----END RSA PRIVATE KEY-----"
        straddle = "z" * 4990 + key + "q" * 100
        masked = mask_secret_values_in_text(straddle)
        assert "BEGIN RSA PRIVATE KEY" not in masked
        assert "END RSA PRIVATE KEY" not in masked

    def test_masks_connection_string_straddling_slice_boundary(self) -> None:
        from modulo.core.secret_patterns import mask_secret_values_in_text

        # A connection string beginning near the end of one 5000-char slice must
        # be masked even though its scheme prefix falls in the previous slice.
        conn = "postgresql://admin:super%40secret%2Fpass@db.host:5432/x"
        straddle = "w" * 4995 + conn + "e" * 50
        masked = mask_secret_values_in_text(straddle)
        assert "super%40secret" not in masked
        assert SENSITIVE_VALUE_MASK in masked

    def test_masks_connection_string_password_with_at_in_password_straddling(self) -> None:
        from modulo.core.secret_patterns import mask_secret_values_in_text

        # Regression: a connection-string password that itself contains ``@``
        # (e.g. ``pa@ss``) must be masked in full even when the string is longer
        # than the 5000-char ReDoS cap. The bounded window must span to the FINAL
        # ``@`` (the host separator), not the first — otherwise the password tail
        # after the first ``@`` leaks unmasked.
        conn = "postgres://u:pa@ss@host:5432/db"
        straddle = "w" * 4995 + conn + "e" * 100
        masked = mask_secret_values_in_text(straddle)
        assert "pa@ss" not in masked
        assert "ss@host" not in masked
        assert SENSITIVE_VALUE_MASK in masked

    def test_masks_redis_connection_string_with_empty_username(self) -> None:
        from modulo.core.secret_patterns import mask_secret_values_in_text

        # A broker/Redis URL with an empty username (``redis://:PASSWORD@host``)
        # must still have its password masked — the user segment may be blank.
        masked = mask_secret_values_in_text("redis://:supersecretpass@host:6379")
        assert "supersecretpass" not in masked
        assert SENSITIVE_VALUE_MASK in masked

    def test_masks_private_key_block_longer_than_slice_cap(self) -> None:
        from modulo.core.secret_patterns import (
            SECRET_VALUE_REDACT_CHAR_CAP,
            mask_secret_values_in_text,
        )

        # A private-key block whose body exceeds the 5000-char ReDoS cap must
        # still be masked in full. Previously its close delimiter fell outside
        # the bounded window and the whole block leaked unmasked.
        key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + ("MIIBOgIBAAJBAKj34GkxFhD" * 200)
            + ("X" * 4000)
            + "\n-----END RSA PRIVATE KEY-----"
        )
        assert len(key) > SECRET_VALUE_REDACT_CHAR_CAP
        text = "a" * 1000 + key + "b" * 100
        masked = mask_secret_values_in_text(text)
        assert "BEGIN RSA PRIVATE KEY" not in masked
        assert "END RSA PRIVATE KEY" not in masked
        # Benign surrounding content is preserved in full (no truncation).
        assert masked.startswith("a" * 1000)
        assert masked.endswith("b" * 100)
