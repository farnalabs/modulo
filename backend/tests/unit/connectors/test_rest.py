"""Unit tests for the generic REST connector (FAR-408)."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.rest import (
    RESTConnectError,
    RestConnector,
    RESTResponseTooLargeError,
    RESTStatusError,
    SecurityGuard,
)
from tests.connectors._conformance import assert_result_shape, assert_write_result_shape


def _default_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def _noop_guard() -> SecurityGuard:
    async def validate_url(url: str) -> None:
        return None

    def filter_strings(values: list[str], resource: str) -> None:
        return None

    return SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)


def _make_connector(
    config: dict[str, Any] | None,
    creds: dict[str, Any] | None,
) -> RestConnector:
    """Build a RestConnector against a stub HTTP transport (no real network)."""
    return RestConnector(
        config,
        creds,
        transport=httpx.MockTransport(_default_handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
    )


# ── Connector type + capabilities ──────────────────────────────────────────


def test_connector_type_is_rest() -> None:
    c = RestConnector({"base_url": "https://api.example.com", "path": "/items"}, {"auth_mode": "bearer", "token": "t"})
    assert c.connector_type is ConnectorType.REST
    assert ConnectorType.REST.capabilities == frozenset({"read", "write"})


# ── Query: records transform ────────────────────────────────────────────────


def test_query_records_path_extracts_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 3
    assert [r["id"] for r in result.records] == [1, 2, 3]
    assert result.total == 3


def test_query_top_level_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"a": 1}, {"a": 2}])

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 2


def test_query_passthrough_raw_body_feeds_shape() -> None:
    """Raw/passthrough content-type still yields a list-of-dicts record shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="a,b\n1,2", headers={"content-type": "text/csv"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 1
    assert result.records[0]["content_type"] == "text/csv"
    assert result.records[0]["body"] == "a,b\n1,2"


def test_query_unknown_resource_raises() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "operations": {"users": {"path": "/items"}}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="Unsupported REST resource"):
        asyncio_run(c.query(ConnectorQuery(resource="nope")))


def test_query_empty_resource_raises() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError):
        asyncio_run(c.query(ConnectorQuery(resource="")))


# ── Query: templating from filters ─────────────────────────────────────────


def test_query_templates_url_and_params_from_filters() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users/{{ user_id }}",
            "params": {"page": "{{ page }}"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default", filters={"user_id": 42, "page": 2})))
    assert str(captured["url"]) == "https://api.example.com/users/42?page=2"
    assert captured["params"] == {"page": "2"}


# ── Auth modes ─────────────────────────────────────────────────────────────


def test_auth_bearer() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["authorization"] == "Bearer sec-token"


def test_auth_api_key_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k123", "in": "header", "header_name": "X-API-Key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["x-api-key"] == "k123"


def test_auth_api_key_query() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k456", "in": "query", "query_param_name": "api_key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"] == {"api_key": "k456"}


def test_api_key_query_secret_not_screened() -> None:
    """A query-mode api_key with filter-triggering chars must never hit the injection filter.

    The credential is injected into params AFTER the screening guard (the same
    invariant as header mode, where apply_auth runs after the guard), so a legit
    secret containing characters the output filter would reject still round-trips.
    """
    screened: list[str] = []

    class _TrackingGuard(SecurityGuard):
        def __init__(self) -> None:
            super().__init__(validate_url=self._noop_validate, filter_strings=self._track_filter)

        @staticmethod
        async def _noop_validate(url: str) -> None:
            return None

        @staticmethod
        def _track_filter(values: list[str], resource: str) -> None:
            screened.extend(values)

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    secret = "k{{java}}ev"
    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": secret, "in": "query", "query_param_name": "api_key"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_TrackingGuard(),
    )
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"] == {"api_key": secret}
    assert secret not in screened


def test_auth_basic() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "basic", "username": "u", "password": "p"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["authorization"].startswith("Basic ")


# ── Injection guard ────────────────────────────────────────────────────────


def test_guard_rejects_auth_header_override() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"Authorization": "evil"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="protected header"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_crlf_in_header_value() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"X-Foo": "ok\r\nX-Evil: 1"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="control characters"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_host_not_in_allowlist() -> None:
    c = _make_connector(
        {
            "base_url": "https://evil.example.com",
            "path": "/items",
            "allowed_hosts": ["api.example.com"],
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="not in allowed_hosts"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_non_http_scheme() -> None:
    c = _make_connector(
        {"base_url": "file:///etc/passwd", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="scheme"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_query_benign_injection_phrase_not_rejected() -> None:
    """A read/query whose term would trip the prompt-injection TEXT classifier must succeed.

    The prompt-injection text classifier is scoped off the READ surface — a
    legitimate agent-supplied search term like ``import os`` or ``eval(`` must not
    throw out of the query path (it would previously raise OutputRejectedError).
    The read surface is protected by the real HTTP controls (control-char
    rejection, protected-header set, SSRF/allowlist) which the dedicated guard
    tests above exercise.
    """

    class _RejectInjectionGuard(SecurityGuard):
        def __init__(self) -> None:
            super().__init__(validate_url=self._noop_validate, filter_strings=self._reject)

        @staticmethod
        async def _noop_validate(url: str) -> None:
            return None

        @staticmethod
        def _reject(values: list[str], resource: str) -> None:
            for value in values:
                for trigger in ("import os", "eval(", "ignore previous instructions"):
                    if trigger in value:
                        raise ValueError(f"rejected injection term: {trigger}")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}]}})

    c = RestConnector(
        {
            "base_url": "https://api.example.com",
            "path": "/search",
            "params": {"q": "{{ q }}"},
            "records_path": "data.items",
        },
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_RejectInjectionGuard(),
    )
    result = asyncio_run(c.query(ConnectorQuery(resource="default", filters={"q": "import os"})))
    assert captured["params"].get("q") == "import os"
    assert len(result.records) == 2
    assert [r["id"] for r in result.records] == [1, 2]


# ── Write ──────────────────────────────────────────────────────────────────


def test_write_posts_json_and_returns_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.read() or request.content
        return httpx.Response(200, json={"method": request.method, "name": json.loads(raw)["name"]})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"name": "{{ name }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert_write_result_shape(result)
    assert result["method"] == "POST"
    assert result["name"] == "Ada"


# ── Health check ───────────────────────────────────────────────────────────


def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True


def test_health_check_api_key_in_query_sends_creds() -> None:
    """health_check must send query-mode api_key creds (not a bare, unauthenticated probe)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "the-secret", "in": "query", "query_param_name": "api_key"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True
    assert captured["params"] == {"api_key": "the-secret"}


# ── ConnectorHub multi-field creds round-trip ──────────────────────────────


def test_hub_ciphertext_round_trips_multi_field_json_dict() -> None:
    """A JSON-dict credentials_ciphertext round-trips as multi-field creds (REST)."""
    from cryptography.fernet import Fernet

    from modulo.core.connector_hub import ConnectorHub
    from modulo.core.secrets_backend import create_secrets_backend

    key = Fernet.generate_key().decode()
    creds = {"auth_mode": "bearer", "token": "secret-token"}
    ciphertext = Fernet(key.encode()).encrypt(json.dumps(creds).encode())

    class _CI:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.connector_type_id = "rest"
            self.config_json = {"base_url": "https://api.example.com", "path": "/items"}
            self.credentials_ciphertext = ciphertext
            self.visibility = "org"
            self.allowed_operations = None

    ci = _CI()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = key
        hub = ConnectorHub(secrets_backend=backend)
        asyncio_run(hub.initialise([ci]))

    connector = hub.get(ci.id)
    assert isinstance(connector._inner, RestConnector)
    assert connector.connector_type is ConnectorType.REST
    assert connector._inner._auth["mode"] == "bearer"
    assert connector._inner._auth["token"] == "secret-token"


def test_hub_teardown_closes_connector_client() -> None:
    """ConnectorHub async teardown must close held connectors' pooled clients (FAR-408)."""
    from cryptography.fernet import Fernet

    from modulo.core.connector_hub import ConnectorHub
    from modulo.core.secrets_backend import create_secrets_backend

    key = Fernet.generate_key().decode()
    creds = {"auth_mode": "bearer", "token": "secret-token"}
    ciphertext = Fernet(key.encode()).encrypt(json.dumps(creds).encode())

    class _CI:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.connector_type_id = "rest"
            self.config_json = {"base_url": "https://api.example.com", "path": "/items"}
            self.credentials_ciphertext = ciphertext
            self.visibility = "org"
            self.allowed_operations = None

    ci = _CI()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = key

        async def scenario() -> None:
            hub = ConnectorHub(secrets_backend=backend)
            await hub.initialise([ci])
            connector = hub.get(ci.id)._inner
            assert isinstance(connector, RestConnector)
            client = connector._client()
            assert client.is_closed is False
            await hub.__aexit__(None, None, None)
            assert client.is_closed is True

        asyncio_run(scenario())


def test_health_check_sends_credentials_and_validates() -> None:
    """health_check must send creds (not a bare probe) and validate the target."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "allowed_hosts": ["api.example.com"],
        },
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True
    assert captured.get("authorization") == "Bearer sec-token"


def test_health_check_reports_bad_status_as_unok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is False


def test_api_key_query_rendered_param_collision_rejected() -> None:
    """A rendered/config param must never shadow the api_key credential param."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "params": {"api_key": "{{ v }}"}},
        {"auth_mode": "api_key", "api_key": "k456", "in": "query", "query_param_name": "api_key"},
    )
    with pytest.raises(ValueError, match="collides with the api_key credential"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_api_key_query_always_overrides_rendered_param() -> None:
    """A rendered param of a DIFFERENT name must never shadow the credential param."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "params": {"api_key": "rendered"}},
        {"auth_mode": "api_key", "api_key": "the-secret", "in": "query", "query_param_name": "auth_token"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"]["api_key"] == "rendered"
    assert captured["params"]["auth_token"] == "the-secret"


def test_three_xx_is_a_distinct_error() -> None:
    """3xx (redirects not followed) must surface as an error, not a passthrough record."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, text="", headers={"location": "https://api.example.com/new"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="location"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_query_honours_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": [{"id": i} for i in range(5)]}})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default", limit=2)))
    assert len(result.records) == 2
    assert result.total == 2


def test_query_rejects_non_none_cursor() -> None:
    """Pagination is response-driven — a direct start cursor is not supported."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="pagination is response-driven"):
        asyncio_run(c.query(ConnectorQuery(resource="default", cursor="abc")))


def test_query_passthrough_forces_wrap_for_json() -> None:
    """passthrough=True forces a single-record wrap even for a JSON body."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [1, 2, 3]}))
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert len(result.records) == 1
    assert '"data"' in result.records[0]["body"]


def test_retry_get_retries_on_429_then_succeeds() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert result.metadata["status_code"] == 200
    assert len(attempts) == 3


def test_write_does_not_retry_non_idempotent() -> None:
    """A POST without an idempotency header is a single attempt, never retried."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"name": "{{ name }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="429"):
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert len(attempts) == 1


def test_retry_delay_honours_retry_after() -> None:
    exc = RESTStatusError("boom", status_code=429, retry_after=2.5)
    assert RestConnector._retry_delay(exc, 0) == 2.5
    assert RestConnector._retry_delay(RESTStatusError("boom", status_code=429, retry_after=None), 1) >= 1.0


def test_retry_delay_caps_huge_retry_after() -> None:
    """An untrusted Retry-After header must be capped so a server cannot make us sleep ~1h."""
    exc = RESTStatusError("boom", status_code=429, retry_after=3600)
    assert RestConnector._retry_delay(exc, 0) == 30.0
    assert RestConnector._retry_delay(exc, 3) == 30.0


def test_dot_index_records_path_rejected() -> None:
    """Legacy dot-index records_path must fail loud instead of being silently coerced."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items.0"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"items": [{"id": 1}]}}))
    with pytest.raises(ValueError, match=r"\[0\]"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_response_size_cap_aborts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 500, headers={"content-type": "text/plain"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "max_response_size": 50},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTResponseTooLargeError, match="too large"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_transport_errors_are_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTConnectError, match="transport error"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
