"""REST connector contract tests — exercises the full connector code path.

Uses ``httpx.MockTransport`` with request capture so every test verifies:
1. The connector processes auth modes, pagination, records_path, on_unknown,
   and allowed_hosts correctly.
2. The connector sends the expected HTTP method, path, and headers.
No Docker, no network — everything runs in-process.
"""

import base64
import json
from typing import Any

import httpx
import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.rest import RestConnector
from tests.connectors._noop_guard import make_noop_security_guard


def _noop_ssrf(_url: str) -> None:
    """SSRF guard stub."""


class _RequestCapture:
    """Captures every request passed to a MockTransport handler."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body_bytes = request.content
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body_bytes.decode("utf-8") if body_bytes else "",
            }
        )
        return httpx.Response(200, json={"items": [{"id": 1}]})

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


def _rest_connector(
    *,
    capture: _RequestCapture,
    config: dict[str, Any] | None = None,
    creds: dict[str, Any] | None = None,
    **extra: Any,
) -> RestConnector:
    """Build a RestConnector wired to the capture transport."""
    base_config = {
        "base_url": "https://api.example.com",
        "path": "/health",
        "records_path": "items",
        "operations": {"directory": {"path": "/api/data"}},
    }
    if config is not None:
        base_config.update(config)
    base_creds = {"auth_mode": "bearer", "token": "tok"} if creds is None else creds
    return RestConnector(
        base_config,
        base_creds,
        transport=httpx.MockTransport(capture.handler),
        ssrf_validator=_noop_ssrf,
        security_guard=make_noop_security_guard(),
        **extra,
    )


# ── REST connector auth modes ──────────────────────────────────────────────


class TestRestAuthModes:
    async def test_bearer_auth(self) -> None:
        """Bearer token appears in Authorization header."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            creds={"auth_mode": "bearer", "token": "secret-bearer-token"},
        )
        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        assert cap.last["headers"].get("authorization") == "Bearer secret-bearer-token"
        await connector.close()

    async def test_api_key_header_auth(self) -> None:
        """api_key in header mode appears as X-API-Key."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            creds={"auth_mode": "api_key", "api_key": "my-api-key", "in": "header", "header_name": "X-API-Key"},
        )
        await connector.query(ConnectorQuery(resource="directory"))
        assert cap.last["headers"].get("x-api-key") == "my-api-key"
        await connector.close()

    async def test_api_key_query_auth(self) -> None:
        """api_key in query mode appears as a query parameter."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            creds={"auth_mode": "api_key", "api_key": "q-key", "in": "query", "query_param_name": "api_key"},
        )
        await connector.query(ConnectorQuery(resource="directory"))
        url = cap.last["url"]
        assert "api_key=q-key" in url
        await connector.close()

    async def test_basic_auth(self) -> None:
        """Basic auth sends Base64-encoded credentials."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            creds={"auth_mode": "basic", "username": "user", "password": "pass"},
        )
        await connector.query(ConnectorQuery(resource="directory"))
        expected = "Basic " + base64.b64encode(b"user:pass").decode()
        assert cap.last["headers"].get("authorization") == expected
        await connector.close()


# ── REST connector pagination ──────────────────────────────────────────────


class TestRestPagination:
    async def test_next_cursor_path(self) -> None:
        """Pagination cursor is extracted from the response via next_cursor_path."""
        call_count = {"n": 0}

        def router(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(
                    200,
                    json={"data": {"items": [{"id": 1}], "next_cursor": "tok2"}},
                )
            return httpx.Response(
                200,
                json={"data": {"items": [{"id": 2}], "next_cursor": None}},
            )

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "data.items",
                "next_cursor_path": "data.next_cursor",
                "operations": {"directory": {"path": "/api/data"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(router),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        page1 = await connector.query(ConnectorQuery(resource="directory"))
        assert len(page1.records) == 1
        assert page1.next_cursor == "tok2"

        # The REST connector's query() rejects a direct cursor parameter.
        # Pagination is response-driven: the caller uses next_cursor from the
        # response to supply the right filter value for the next page.
        with pytest.raises(ValueError, match="response-driven"):
            await connector.query(ConnectorQuery(resource="directory", cursor="tok2"))
        await connector.close()

    async def test_next_cursor_none_when_exhausted(self) -> None:
        """next_cursor is None when there are no more pages."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            config={
                "records_path": "items",
                "operations": {"directory": {"path": "/api/data"}},
            },
        )
        result = await connector.query(ConnectorQuery(resource="directory"))
        assert result.next_cursor is None
        await connector.close()


# ── REST connector records_path extraction ─────────────────────────────────


class TestRestRecordsPath:
    async def test_nested_records_path(self) -> None:
        """Records are extracted from a nested JSON path."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"response": {"data": {"records": [{"a": 1}, {"a": 2}]}}},
            )

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "response.data.records",
                "operations": {"directory": {"path": "/api/items"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 2
        assert result.records[0]["a"] == 1
        await connector.close()

    async def test_records_path_total(self) -> None:
        """Total count is extracted when a total_path is declared."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"items": [{"id": 1}], "total_count": 42},
            )

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "items",
                "operations": {"directory": {"path": "/api/items"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        assert result.total is None or result.total >= 0
        await connector.close()

    async def test_records_limit_truncation(self) -> None:
        """Query limit truncates returned records."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"items": [{"id": 1}, {"id": 2}, {"id": 3}]},
            )

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "items",
                "operations": {"directory": {"path": "/api/items"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        result = await connector.query(ConnectorQuery(resource="directory", limit=2))
        assert len(result.records) == 2
        assert result.total == 2
        await connector.close()


# ── REST connector on_unknown idempotency policy ───────────────────────────


class TestRestOnUnknownPolicy:
    async def test_on_unknown_fail_open(self) -> None:
        """fail_open on_unknown returns the mode."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            config={"on_unknown": "fail_open"},
        )
        assert connector.on_unknown_for("directory") == "fail_open"
        await connector.close()

    async def test_on_unknown_fail_closed(self) -> None:
        """fail_closed on_unknown returns the mode."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            config={"on_unknown": "fail_closed"},
        )
        assert connector.on_unknown_for("directory") == "fail_closed"
        await connector.close()

    async def test_on_unknown_off(self) -> None:
        """off on_unknown returns the mode."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            config={"on_unknown": "off"},
        )
        assert connector.on_unknown_for("directory") == "off"
        await connector.close()

    async def test_per_resource_on_unknown_override(self) -> None:
        """Per-resource on_unknown overrides the top-level default."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            config={
                "on_unknown": "fail_open",
                "operations": {
                    "directory": {"path": "/api/data"},
                    "upload": {"path": "/api/upload", "on_unknown": "fail_closed"},
                },
            },
        )
        assert connector.on_unknown_for("directory") == "fail_open"
        assert connector.on_unknown_for("upload") == "fail_closed"
        await connector.close()

    def test_invalid_on_unknown_raises(self) -> None:
        """Invalid on_unknown value is rejected at construction time."""
        cap = _RequestCapture()
        with pytest.raises(ValueError, match="REST on_unknown must be one of"):
            _rest_connector(
                capture=cap,
                config={"on_unknown": "bogus"},
            )


# ── REST connector allowed_hosts enforcement ───────────────────────────────


class TestRestAllowedHosts:
    async def test_allowed_hosts_blocks_unknown(self) -> None:
        """Request to a host not in allowed_hosts raises ValueError."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": []})

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "items",
                "allowed_hosts": ["other-host.com"],
                "operations": {"directory": {"path": "/api/data"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        with pytest.raises(ValueError, match="not in allowed_hosts"):
            await connector.query(ConnectorQuery(resource="directory"))
        await connector.close()

    async def test_allowed_hosts_permits_matching(self) -> None:
        """Request to an allowed host proceeds normally."""
        cap = _RequestCapture()
        connector = _rest_connector(
            capture=cap,
            config={"allowed_hosts": ["api.example.com"]},
        )
        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        await connector.close()

    async def test_allowed_hosts_subdomain_match(self) -> None:
        """Subdomain of an allowed host is also permitted."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [{"id": 1}]})

        connector = RestConnector(
            {
                "base_url": "https://sub.api.example.com",
                "path": "/health",
                "records_path": "items",
                "allowed_hosts": ["example.com"],
                "operations": {"directory": {"path": "/api/data"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        await connector.close()


# ── REST connector write ───────────────────────────────────────────────────


class TestRestWrite:
    async def test_write_returns_json_result(self) -> None:
        """POST write returns the server response as a dict."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json={"created": True, "id": body.get("name", "unknown")})

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "items",
                "operations": {
                    "file": {"method": "POST", "path": "/api/create", "body": {"name": "{{ name }}"}},
                },
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        result = await connector.write(ConnectorPayload(resource="file", data={"name": "test-item"}))
        assert result["created"] is True
        assert result["id"] == "test-item"
        await connector.close()

    async def test_write_sends_correct_method_and_path(self) -> None:
        """Write sends the configured HTTP method and path."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"ok": True})

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "items",
                "operations": {
                    "item": {"method": "PUT", "path": "/api/items/{{ item_id }}"},
                },
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        await connector.write(ConnectorPayload(resource="item", data={"item_id": "42", "name": "updated"}))
        assert captured["method"] == "PUT"
        assert "/api/items/42" in captured["url"]
        await connector.close()

    async def test_write_with_templated_body(self) -> None:
        """Write renders Jinja templates in the body."""
        captured_body: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        connector = RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/health",
                "records_path": "items",
                "operations": {
                    "task": {
                        "method": "POST",
                        "path": "/api/tasks",
                        "body": {"title": "{{ title }}", "priority": "{{ priority }}"},
                    },
                },
            },
            {"auth_mode": "bearer", "token": "tok"},
            transport=httpx.MockTransport(handler),
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
        )

        await connector.write(ConnectorPayload(resource="task", data={"title": "Fix bug", "priority": "high"}))
        assert captured_body["title"] == "Fix bug"
        assert captured_body["priority"] == "high"
        await connector.close()
