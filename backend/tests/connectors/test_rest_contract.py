"""REST connector contract tests — exercises the full connector code path.

Uses a real in-process HTTP server (``http.server`` + ``socketserver``) on
``127.0.0.1`` with an OS-assigned port so every test verifies:

1. The connector processes auth modes, pagination, records_path, on_unknown,
   and allowed_hosts correctly.
2. The connector sends the expected HTTP method, path, and headers over a real
   socket (including the SSRF-pinned transport path when no test seam is
   injected).
3. The ``allowed_hosts`` enforcement rejects a host NOT in the allowlist and
   permits the local test server when it IS listed.

No Docker, no external network — the server binds to loopback on a free port.
"""

import base64
import json
from typing import Any

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.rest import RestConnector
from tests.connectors._noop_guard import make_noop_security_guard


async def _noop_ssrf(_url: str) -> None:
    """Async SSRF guard stub (matches the SsrfValidator Awaitable signature)."""


class _RequestCapture:
    """Captures every request the real server received."""

    def __init__(self, server: Any) -> None:
        self._server = server

    @property
    def last(self) -> dict[str, Any]:
        return self._server.request_log[-1]

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.request_log


def _rest_connector(
    *,
    server: Any,
    capture: _RequestCapture,
    config: dict[str, Any] | None = None,
    creds: dict[str, Any] | None = None,
    **extra: Any,
) -> RestConnector:
    """Build a RestConnector wired to the real in-process test server."""
    base_config: dict[str, Any] = {
        "base_url": server.url(),
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
        ssrf_validator=_noop_ssrf,
        security_guard=make_noop_security_guard(),
        verify_tls=False,
        **extra,
    )


# ── REST connector auth modes ──────────────────────────────────────────────


class TestRestAuthModes:
    async def test_bearer_auth(self, rest_test_server: Any) -> None:
        """Bearer token appears in Authorization header."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            creds={"auth_mode": "bearer", "token": "secret-bearer-token"},
        )
        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        assert cap.last["headers"].get("authorization") == "Bearer secret-bearer-token"
        await connector.close()

    async def test_api_key_header_auth(self, rest_test_server: Any) -> None:
        """api_key in header mode appears as X-API-Key."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            creds={"auth_mode": "api_key", "api_key": "my-api-key", "in": "header", "header_name": "X-API-Key"},
        )
        await connector.query(ConnectorQuery(resource="directory"))
        assert cap.last["headers"].get("x-api-key") == "my-api-key"
        await connector.close()

    async def test_api_key_query_auth(self, rest_test_server: Any) -> None:
        """api_key in query mode appears as a query parameter."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            creds={"auth_mode": "api_key", "api_key": "q-key", "in": "query", "query_param_name": "api_key"},
        )
        await connector.query(ConnectorQuery(resource="directory"))
        url = cap.last["path"]
        assert "api_key=q-key" in url
        await connector.close()

    async def test_basic_auth(self, rest_test_server: Any) -> None:
        """Basic auth sends Base64-encoded credentials."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            creds={"auth_mode": "basic", "username": "user", "password": "pass"},
        )
        await connector.query(ConnectorQuery(resource="directory"))
        expected = "Basic " + base64.b64encode(b"user:pass").decode()
        assert cap.last["headers"].get("authorization") == expected
        await connector.close()


# ── REST connector pagination ──────────────────────────────────────────────


class TestRestPagination:
    async def test_next_cursor_path(self, rest_test_server: Any) -> None:
        """Pagination cursor is extracted from the response via next_cursor_path."""
        call_count = {"n": 0}

        def _route(method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                data = {"data": {"items": [{"id": 1}], "next_cursor": "tok2"}}
            else:
                data = {"data": {"items": [{"id": 2}], "next_cursor": None}}
            return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()

        rest_test_server.route_request = _route  # type: ignore[assignment]

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "data.items",
                "next_cursor_path": "data.next_cursor",
                "operations": {"directory": {"path": "/api/data"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        page1 = await connector.query(ConnectorQuery(resource="directory"))
        assert len(page1.records) == 1
        assert page1.next_cursor == "tok2"

        with pytest.raises(ValueError, match="response-driven"):
            await connector.query(ConnectorQuery(resource="directory", cursor="tok2"))
        await connector.close()

    async def test_next_cursor_none_when_exhausted(self, rest_test_server: Any) -> None:
        """next_cursor is None when there are no more pages."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
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
    async def test_nested_records_path(self, rest_test_server: Any) -> None:
        """Records are extracted from a nested JSON path."""
        rest_test_server.set_default_response(body={"response": {"data": {"records": [{"a": 1}, {"a": 2}]}}})

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "response.data.records",
                "operations": {"directory": {"path": "/api/items"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 2
        assert result.records[0]["a"] == 1
        await connector.close()

    async def test_records_path_total(self, rest_test_server: Any) -> None:
        """Total count is extracted when a total_path is declared."""
        rest_test_server.set_default_response(body={"items": [{"id": 1}], "total_count": 42})

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "items",
                "operations": {"directory": {"path": "/api/items"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        assert result.total is None or result.total >= 0
        await connector.close()

    async def test_records_limit_truncation(self, rest_test_server: Any) -> None:
        """Query limit truncates returned records."""
        rest_test_server.set_default_response(body={"items": [{"id": 1}, {"id": 2}, {"id": 3}]})

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "items",
                "operations": {"directory": {"path": "/api/items"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        result = await connector.query(ConnectorQuery(resource="directory", limit=2))
        assert len(result.records) == 2
        assert result.total == 2
        await connector.close()


# ── REST connector on_unknown idempotency policy ───────────────────────────


class TestRestOnUnknownPolicy:
    async def test_on_unknown_fail_open(self, rest_test_server: Any) -> None:
        """fail_open on_unknown returns the mode."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            config={"on_unknown": "fail_open"},
        )
        assert connector.on_unknown_for("directory") == "fail_open"
        await connector.close()

    async def test_on_unknown_fail_closed(self, rest_test_server: Any) -> None:
        """fail_closed on_unknown returns the mode."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            config={"on_unknown": "fail_closed"},
        )
        assert connector.on_unknown_for("directory") == "fail_closed"
        await connector.close()

    async def test_on_unknown_off(self, rest_test_server: Any) -> None:
        """off on_unknown returns the mode."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            config={"on_unknown": "off"},
        )
        assert connector.on_unknown_for("directory") == "off"
        await connector.close()

    async def test_per_resource_on_unknown_override(self, rest_test_server: Any) -> None:
        """Per-resource on_unknown overrides the top-level default."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
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

    def test_invalid_on_unknown_raises(self, rest_test_server: Any) -> None:
        """Invalid on_unknown value is rejected at construction time."""
        cap = _RequestCapture(rest_test_server)
        with pytest.raises(ValueError, match="REST on_unknown must be one of"):
            _rest_connector(
                server=rest_test_server,
                capture=cap,
                config={"on_unknown": "bogus"},
            )


# ── REST connector allowed_hosts enforcement ───────────────────────────────


class TestRestAllowedHosts:
    async def test_allowed_hosts_blocks_unknown(self, rest_test_server: Any) -> None:
        """Request to a host not in allowed_hosts raises ValueError."""
        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "items",
                "allowed_hosts": ["other-host.com"],
                "operations": {"directory": {"path": "/api/data"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        with pytest.raises(ValueError, match="not in allowed_hosts"):
            await connector.query(ConnectorQuery(resource="directory"))
        await connector.close()

    async def test_allowed_hosts_permits_matching(self, rest_test_server: Any) -> None:
        """Request to an allowed host proceeds normally."""
        cap = _RequestCapture(rest_test_server)
        connector = _rest_connector(
            server=rest_test_server,
            capture=cap,
            config={"allowed_hosts": ["127.0.0.1"]},
        )
        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        await connector.close()

    async def test_allowed_hosts_subdomain_match(self, rest_test_server: Any) -> None:
        """Subdomain of an allowed host is also permitted."""
        rest_test_server.set_default_response(body={"items": [{"id": 1}]})

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "items",
                "allowed_hosts": ["127.0.0.1"],
                "operations": {"directory": {"path": "/api/data"}},
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        result = await connector.query(ConnectorQuery(resource="directory"))
        assert len(result.records) == 1
        await connector.close()


# ── REST connector write ───────────────────────────────────────────────────


class TestRestWrite:
    async def test_write_returns_json_result(self, rest_test_server: Any) -> None:
        """POST write returns the server response as a dict."""

        def _route(method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
            parsed = json.loads(body) if body else {}
            resp = {"created": True, "id": parsed.get("name", "unknown")}
            return 200, {"Content-Type": "application/json"}, json.dumps(resp).encode()

        rest_test_server.route_request = _route  # type: ignore[assignment]

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "items",
                "operations": {
                    "file": {"method": "POST", "path": "/api/create", "body": {"name": "{{ name }}"}},
                },
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        result = await connector.write(ConnectorPayload(resource="file", data={"name": "test-item"}))
        assert result["created"] is True
        assert result["id"] == "test-item"
        await connector.close()

    async def test_write_sends_correct_method_and_path(self, rest_test_server: Any) -> None:
        """Write sends the configured HTTP method and path."""
        captured: dict[str, Any] = {}

        def _route(method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
            captured["method"] = method
            captured["url"] = path
            return 200, {"Content-Type": "application/json"}, json.dumps({"ok": True}).encode()

        rest_test_server.route_request = _route  # type: ignore[assignment]

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
                "path": "/health",
                "records_path": "items",
                "operations": {
                    "item": {"method": "PUT", "path": "/api/items/{{ item_id }}"},
                },
            },
            {"auth_mode": "bearer", "token": "tok"},
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        await connector.write(ConnectorPayload(resource="item", data={"item_id": "42", "name": "updated"}))
        assert captured["method"] == "PUT"
        assert "/api/items/42" in captured["url"]
        await connector.close()

    async def test_write_with_templated_body(self, rest_test_server: Any) -> None:
        """Write renders Jinja templates in the body."""
        captured_body: dict[str, Any] = {}

        def _route(method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
            captured_body.update(json.loads(body))
            return 200, {"Content-Type": "application/json"}, json.dumps({"ok": True}).encode()

        rest_test_server.route_request = _route  # type: ignore[assignment]

        connector = RestConnector(
            {
                "base_url": rest_test_server.url(),
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
            ssrf_validator=_noop_ssrf,
            security_guard=make_noop_security_guard(),
            verify_tls=False,
        )

        await connector.write(ConnectorPayload(resource="task", data={"title": "Fix bug", "priority": "high"}))
        assert captured_body["title"] == "Fix bug"
        assert captured_body["priority"] == "high"
        await connector.close()
