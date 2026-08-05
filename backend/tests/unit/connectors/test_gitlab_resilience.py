"""Resilience unit tests for GitLabConnector — error wrapping for pipeline safety."""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.gitlab import GitLabConnector

TOKEN = "glpat_test_token"
_API = "https://gitlab.com/api/v4"
_TOKEN_INFO = "https://gitlab.com/oauth/token/info"
_SELF_TOKEN_INFO = "https://gitlab.example.com/oauth/token/info"
_FULL_SCOPES = {"scope": ["read_api", "write_repository", "api"]}


@pytest.fixture()
def connector():
    return GitLabConnector(token=TOKEN)


@respx.mock
async def test_query_429_rate_limit_returns_value_error(connector):
    """HTTP 429 should be wrapped as ValueError, not raw HTTPStatusError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded"),
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_query_500_error_returns_value_error(connector):
    """HTTP 500 should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(500, text="Internal Server Error"),
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 500"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_write_429_rate_limit_returns_value_error(connector):
    """HTTP 429 on write should be wrapped as ValueError."""
    respx.post(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(429, text="Rate limit exceeded"),
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429"):
        await connector.write(
            ConnectorPayload(
                resource="issue",
                data={"project": "group/project", "title": "Test"},
            )
        )


@respx.mock
async def test_query_connection_error_returns_value_error(connector):
    """Connection error should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    with pytest.raises(ValueError, match="GitLab API connection error"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_query_timeout_returns_value_error(connector):
    """Timeout should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        side_effect=httpx.TimeoutException("Request timed out"),
    )
    with pytest.raises(ValueError, match="GitLab API timeout"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_query_invalid_json_returns_value_error(connector):
    """Malformed JSON response should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    with pytest.raises(ValueError, match="GitLab API invalid response"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_health_check_invalid_json(connector):
    """Health check should handle invalid JSON gracefully."""
    respx.get(f"{_API}/user").mock(
        return_value=httpx.Response(200, text="not-json"),
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid JSON" in result.detail


@respx.mock
async def test_query_429_retry_then_success(connector):
    """HTTP 429 should be retried and succeed on second attempt."""
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit exceeded"),
            httpx.Response(200, json=[{"id": 1}]),
        ],
    )
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_query_502_retry_then_success(connector):
    """HTTP 502 should be retried and succeed on second attempt."""
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(200, json=[{"id": 1}]),
        ],
    )
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_query_429_retry_exhausted_returns_value_error(connector):
    """HTTP 429 retries exhausted should still return ValueError."""
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit"),
            httpx.Response(429, text="Rate limit"),
            httpx.Response(429, text="Rate limit"),
            httpx.Response(429, text="Rate limit"),
        ],
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert route.call_count == 4


@respx.mock
async def test_query_304_returns_value_error(connector):
    """HTTP 304 Not Modified should be wrapped as ValueError."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(304),
    )
    with pytest.raises(ValueError, match="304 Not Modified"):
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))


@respx.mock
async def test_write_429_retry_then_success(connector):
    """Write HTTP 429 should be retried and succeed."""
    route = respx.post(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(429, text="Rate limit"),
            httpx.Response(200, json={"id": 1}),
        ],
    )
    result = await connector.write(
        ConnectorPayload(
            resource="issue",
            data={"project": "group/project", "title": "Test"},
        ),
    )
    assert result["id"] == 1
    assert route.call_count == 2


@respx.mock
async def test_health_check_uses_single_client_session(connector):
    """Health check should use one client for both /user and /projects calls."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_parse_json_narrowed_to_jsondecodeerror(connector):
    """_parse_json should catch json.JSONDecodeError, not bare Exception."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(200, text="[1, 2]"),
    )
    # list responses should work (they're valid JSON) — if _parse_json was dict-only this would fail
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert len(result.records) == 2


@respx.mock
async def test_query_429_uses_rate_limit_reset_time(connector, monkeypatch):
    """RateLimit-ResetTime header should drive the retry delay on 429."""

    # Freeze time so the reset window cannot expire mid-request (the connector's
    # own processing time would otherwise push reset_epoch - time.time() <= 0 and
    # fall back to exponential backoff, making the test timing-flaky).
    fake_now = 1_000_000.0
    monkeypatch.setattr("modulo.connectors.gitlab.time.time", lambda: fake_now)
    reset_epoch = fake_now + 5.0  # 5s reset window, comfortably in the future
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"RateLimit-Remaining": "0", "RateLimit-ResetTime": str(reset_epoch)},
                text="Rate limit exceeded",
            ),
            httpx.Response(200, json=[{"id": 1}]),
        ],
    )
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_query_429_uses_rate_limit_reset_fallback(connector, monkeypatch):
    """RateLimit-Reset (epoch) should drive the retry delay when ResetTime is absent."""

    fake_now = 1_000_000.0
    monkeypatch.setattr("modulo.connectors.gitlab.time.time", lambda: fake_now)
    reset_epoch = fake_now + 5.0
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"RateLimit-Remaining": "0", "RateLimit-Reset": str(reset_epoch)},
                text="Rate limit exceeded",
            ),
            httpx.Response(200, json=[{"id": 1}]),
        ],
    )
    result = await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert len(result.records) == 1
    assert route.call_count == 2


@respx.mock
async def test_query_429_rate_limit_headers_in_error(connector):
    """Rate-limit quota headers should surface in the final 429 error detail."""
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                headers={
                    "RateLimit-Limit": "600",
                    "RateLimit-Remaining": "0",
                    "RateLimit-Reset": "1754000000",
                },
                text="Rate limit",
            )
            for _ in range(4)
        ],
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429") as excinfo:
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert route.call_count == 4
    assert "RateLimit-Reset=1754000000" in str(excinfo.value)


@respx.mock
async def test_health_check_detects_missing_scope_on_projects(connector):
    """A 403 on /projects (insufficient read_api/api scope) should fail health."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(403, json={"error": "insufficient_scope"}))
    result = await connector.health_check()
    assert result.ok is False
    assert "read_api" in result.detail or "api" in result.detail


@respx.mock
async def test_health_check_detects_expired_token(connector):
    """A 401 on /user should be reported as an invalid/expired token, not a scope issue."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(401, json={"error": "invalid_token"}))
    result = await connector.health_check()
    assert result.ok is False
    assert "Invalid or expired" in result.detail


@respx.mock
async def test_health_check_ok_with_rate_limit_headers(connector):
    """Healthy quota headers should not trip the health check."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1}],
            headers={"RateLimit-Limit": "600", "RateLimit-Remaining": "599"},
        )
    )
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_no_rate_limit_headers_ok(connector):
    """No RateLimit-* headers (unrestricted api scope) should pass health."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await connector.health_check()
    assert result.ok is True


def test_retry_delay_429_reset_window_not_capped():
    """A quota reset window longer than _MAX_DELAY must not be capped."""
    import time

    reset_epoch = time.time() + 45.0
    response = httpx.Response(429, headers={"RateLimit-ResetTime": str(reset_epoch)})
    delay = GitLabConnector._retry_delay(response, attempt=0)
    assert 44.0 < delay <= 45.0


def test_retry_delay_retry_after_and_backoff_capped():
    """Retry-After and exponential backoff remain capped at _MAX_DELAY."""
    long_retry_after = httpx.Response(429, headers={"Retry-After": "120"})
    assert GitLabConnector._retry_delay(long_retry_after, attempt=0) == 30.0
    far_reset = httpx.Response(503)
    assert GitLabConnector._retry_delay(far_reset, attempt=5) == 30.0


def test_has_server_delay_gated_to_429_for_reset_headers():
    """RateLimit-Reset headers must only count as a server delay on 429.

    GitLab sends these headers on all responses while rate limiting is active,
    so on 502/503/504 they must not switch the backoff to tight jitter.
    """
    import time

    reset = str(int(time.time()) + 60)
    for status in (502, 503, 504):
        response = httpx.Response(status, headers={"RateLimit-Reset": reset})
        assert GitLabConnector._has_server_delay(response) is False
    response = httpx.Response(429, headers={"RateLimit-Reset": reset})
    assert GitLabConnector._has_server_delay(response) is True


def test_has_server_delay_retry_after_any_status():
    """Retry-After is an explicit server wait and counts on any status."""
    response = httpx.Response(503, headers={"Retry-After": "5"})
    assert GitLabConnector._has_server_delay(response) is True


@respx.mock
async def test_query_error_surfaces_request_id(connector):
    """API errors should surface GitLab's X-Request-Id header for support debugging."""
    respx.get(f"{_API}/projects/group%2Fproject/issues").mock(
        return_value=httpx.Response(
            500,
            text="Internal Server Error",
            headers={"X-Request-Id": "req_abc123"},
        )
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 500") as excinfo:
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert "req_abc123" in str(excinfo.value)


@respx.mock
async def test_query_429_exhausted_surfaces_request_id(connector):
    """The final exhausted-429 error should carry the request id for escalation."""
    route = respx.get(f"{_API}/projects/group%2Fproject/issues")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                text="Rate limit",
                headers={"X-Request-Id": "req_rate_1"},
            )
            for _ in range(4)
        ],
    )
    with pytest.raises(ValueError, match="GitLab API HTTP 429") as excinfo:
        await connector.query(ConnectorQuery(resource="issues", filters={"project": "group/project"}))
    assert route.call_count == 4
    assert "req_rate_1" in str(excinfo.value)


@respx.mock
async def test_health_check_401_surfaces_request_id(connector):
    """Health-check failures should include the request id when GitLab reports one."""
    respx.get(f"{_API}/user").mock(
        return_value=httpx.Response(401, text="unauthorized", headers={"X-Request-Id": "req_401"})
    )
    result = await connector.health_check()
    assert result.ok is False
    assert "req_401" in result.detail


@respx.mock
async def test_self_hosted_health_check_reports_version(connector):
    """Self-hosted health checks report the instance version for diagnostics."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "myuser"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(
        return_value=httpx.Response(200, json={"version": "16.9.2-ee", "revision": "deadbeef"})
    )
    respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "myuser (GitLab 16.9.2-ee)"


@respx.mock
async def test_self_hosted_health_check_version_probe_failure_non_fatal(connector):
    """A failing /version probe must not fail the health check (diagnostic only)."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "myuser"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_self_hosted_health_check_version_probe_non_object_body_non_fatal(connector):
    """A 2xx /version probe with a non-object JSON body must not fail the health check."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "myuser"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(
        return_value=httpx.Response(200, json=["unexpected", "array"])
    )
    respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_self_hosted_health_check_version_probe_network_error_non_fatal(connector):
    """A network error on the version probe must not fail the health check."""
    custom = GitLabConnector(token=TOKEN, base_url="https://gitlab.example.com/api/v4")
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "myuser"})
    )
    respx.get("https://gitlab.example.com/api/v4/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get("https://gitlab.example.com/api/v4/version").mock(side_effect=httpx.ConnectError("Connection refused"))
    respx.get(_SELF_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await custom.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


@respx.mock
async def test_hosted_health_check_does_not_probe_version(connector):
    """The hosted gitlab.com endpoint is not probed for /version."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json=_FULL_SCOPES))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


def test_validate_path_helpers():
    """Module-level path validation rejects traversal and absolute paths."""
    from modulo.connectors.gitlab import _validate_path

    _validate_path("src/main.py", "file")
    _validate_path("a/b/c.txt", "file")
    for bad in ("../x", "a/../x", "..", "../../etc/passwd", "a\\..\\b", "/abs/path", "\\abs"):
        try:
            _validate_path(bad, "file")
        except ValueError:
            continue
        raise AssertionError(f"path {bad!r} was not rejected")
    with pytest.raises(ValueError):
        _validate_path("", "file")


@respx.mock
async def test_health_check_token_info_deprecated_scopes_alias(connector):
    """The deprecated plural ``scopes`` field is honoured as an alias for ``scope``."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scopes": ["read_api"]}))
    result = await connector.health_check()
    assert result.ok is False
    assert "write_repository" in result.detail


@respx.mock
async def test_health_check_token_info_string_scopes(connector):
    """Some self-hosted deployments return the scope as a space-separated string."""
    respx.get(f"{_API}/user").mock(return_value=httpx.Response(200, json={"username": "myuser"}))
    respx.get(f"{_API}/projects").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    respx.get(_TOKEN_INFO).mock(return_value=httpx.Response(200, json={"scope": "api read_api"}))
    result = await connector.health_check()
    assert result.ok is True
    assert result.detail == "myuser"


def test_instance_root_derivation():
    """_instance_root strips a versioned API path and keeps reverse-proxy mounts."""
    from modulo.connectors.gitlab import _instance_root

    assert _instance_root("https://gitlab.com/api/v4") == "https://gitlab.com"
    assert _instance_root("https://gitlab.com/api/v4/") == "https://gitlab.com"
    assert _instance_root("https://gitlab.example.com/api/v3") == "https://gitlab.example.com"
    assert _instance_root("https://gitlab.example.com/gitlab/api/v4") == "https://gitlab.example.com/gitlab"
    assert _instance_root("https://gitlab.example.com") == "https://gitlab.example.com"


def test_effective_scopes_api_superset():
    """api scope expands to cover read_api and write_repository."""
    from modulo.connectors.gitlab import REQUIRED_SCOPES, _effective_scopes

    assert REQUIRED_SCOPES - _effective_scopes(frozenset({"api"})) == frozenset()
    assert REQUIRED_SCOPES - _effective_scopes(frozenset({"read_api"})) == frozenset({"api", "write_repository"})
    assert REQUIRED_SCOPES - _effective_scopes(frozenset({"read_api", "write_repository"})) == frozenset({"api"})
    assert REQUIRED_SCOPES - _effective_scopes(frozenset({"read_api", "api"})) == frozenset()
