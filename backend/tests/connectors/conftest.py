"""Connector conformance fixtures — NOT the project-level conftest."""

import http.server
import json
import socketserver
import threading
import uuid
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest

from modulo.connectors.base import ConnectorBase
from modulo.connectors.rest import RestConnector
from tests.connectors._conformance import get_registered_fixture, get_registered_types, register_conformance_connector
from tests.connectors._noop_guard import make_noop_security_guard

# ── VCR config for pytest-recording ────────────────────────────────────────


@pytest.fixture
def vcr_config() -> dict[str, Any]:
    """Wire the VCR config from the shared helpers module.

    ``pytest-recording`` picks up this fixture automatically when a test is
    marked ``@pytest.mark.vcr``.  ``record_mode`` defaults to ``"none"``
    (replay-only) so the committed suite runs offline; set
    ``VCR_RECORD_MODE=once`` to record new cassettes.
    """
    from tests.helpers.vcr import vcr_config as _vcr_config

    return _vcr_config()


# ── In-process HTTP test server ────────────────────────────────────────────


class _RequestHandler(http.server.BaseHTTPRequestHandler):
    """Routes requests to the owning ``_TestHttpServer`` and records them."""

    def do_request(self) -> None:
        server = self.server.test_http_server  # type: ignore[attr-defined]  # _TestHttpServer instance
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length) if length else b""
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        client_addr = f"{self.client_address[0]}:{self.client_address[1]}"
        server.request_log.append(
            {
                "method": self.command,
                "path": self.path,
                "query": query,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body.decode("utf-8", errors="replace"),
                "client": client_addr,
            }
        )
        status, resp_headers, resp_body = server.route_request(self.command, self.path, dict(self.headers), body)
        self.send_response(status)
        for key, val in resp_headers.items():
            self.send_header(key, val)
        self.end_headers()
        if resp_body:
            self.wfile.write(resp_body)

    do_GET = do_request  # noqa: N815 — http.server method names are mixedCase
    do_POST = do_request  # noqa: N815
    do_PUT = do_request  # noqa: N815
    do_PATCH = do_request  # noqa: N815
    do_DELETE = do_request  # noqa: N815
    do_HEAD = do_request  # noqa: N815
    do_OPTIONS = do_request  # noqa: N815

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress stderr noise


class _TestHttpServer:
    """Real in-process HTTP server on a free port (port 0) for connector tests.

    Routes are registered via ``set_response``.  Every request is recorded in
    ``request_log`` so tests can assert what the connector actually sent over
    the wire (method, path, headers, body, client address).
    """

    def __init__(self) -> None:
        self.request_log: list[dict[str, Any]] = []
        self._routes: dict[tuple[str, str], tuple[int, dict[str, str], bytes]] = {}
        self._default_response: tuple[int, dict[str, str], bytes] = (
            200,
            {"Content-Type": "application/json"},
            json.dumps({"items": [{"id": 1}]}).encode(),
        )
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.host = "127.0.0.1"
        self.port = 0

    def set_response(
        self,
        method: str,
        path: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> None:
        if isinstance(body, (dict, list)):
            body_bytes = json.dumps(body).encode()
            headers = {**(headers or {}), "Content-Type": "application/json"}
        elif isinstance(body, str):
            body_bytes = body.encode()
            headers = {**(headers or {}), "Content-Type": "text/plain"}
        elif isinstance(body, bytes):
            body_bytes = body
        else:
            body_bytes = json.dumps(body).encode() if body is not None else b""
            if body is not None:
                headers = {**(headers or {}), "Content-Type": "application/json"}
        self._routes[(method.upper(), path)] = (status, headers or {}, body_bytes)

    def set_default_response(self, status: int = 200, headers: dict[str, str] | None = None, body: Any = None) -> None:
        if isinstance(body, (dict, list)):
            body_bytes = json.dumps(body).encode()
            headers = {**(headers or {}), "Content-Type": "application/json"}
        elif isinstance(body, str):
            body_bytes = body.encode()
            headers = {**(headers or {}), "Content-Type": "text/plain"}
        elif isinstance(body, bytes):
            body_bytes = body
        else:
            body_bytes = b""
        self._default_response = (status, headers or {}, body_bytes)

    def route_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        key = (method.upper(), path)
        if key in self._routes:
            return self._routes[key]
        return self._default_response

    def start(self) -> None:
        self._server = socketserver.ThreadingTCPServer((self.host, 0), _RequestHandler)
        self._server.daemon_threads = True
        self._server.test_http_server = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def url(self, path: str = "") -> str:
        return f"http://{self.host}:{self.port}{path}"


@pytest.fixture
def rest_test_server(monkeypatch: pytest.MonkeyPatch):
    """Yield a real in-process HTTP server on 127.0.0.1 with an OS-assigned port.

    The server records every request in ``request_log`` so tests can assert on
    what the connector actually sent over the wire.  Use ``set_response`` to
    configure per-route responses; unhandled routes return the default 200 JSON
    response.

    Sets ``SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128`` for the test scope
    so the connector's pinned transport permits loopback — the documented
    operator mechanism for self-hosted deployments reaching localhost backends
    (SonarQube, Ollama, etc.).  This does NOT weaken the SSRF guard;
    link-local / metadata / multicast ranges remain blocked.
    """
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,::1/128")
    server = _TestHttpServer()
    server.start()
    yield server
    server.stop()


# ── Connector fixture definitions ──────────────────────────────────────────


@pytest.fixture
def fs_connector(tmp_path: Path):
    from modulo.connectors.filesystem import FilesystemConnector

    return FilesystemConnector(base_path=str(tmp_path))


register_conformance_connector("filesystem", "fs_connector")


class _FakeRuntimeProvider:
    """Minimal ShellConnector runtime provider satisfying the Protocol."""

    async def execute_command(
        self,
        workspace: Any,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        return {"exit_code": 0, "stdout": "", "stderr": ""}


@pytest.fixture
def shell_connector():
    from modulo.connectors.shell import ShellConnector

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return ShellConnector(
            runtime_provider=_FakeRuntimeProvider(),
            workspace_lease_id=uuid.uuid4(),
            allowed_commands=["echo", "cat"],
        )


register_conformance_connector("shell", "shell_connector")


@pytest.fixture
def rest_connector():
    """A REST connector driven by a stub ``MockTransport`` (no real network).

    The ``operations`` map only declares ``directory`` (read) and ``file``
    (write), so unknown-resource conformance scenarios raise as expected. The
    transport returns a JSON list for GETs (readable via ``records_path``) and a
    JSON object for POSTs (the write result).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}]}})

    return RestConnector(
        {
            "base_url": "https://api.example.com",
            "path": "/health",
            "records_path": "data.items",
            "operations": {
                "directory": {"path": "/directory"},
                "file": {"method": "POST", "path": "/file", "body": {"path": "{{ path }}"}},
            },
        },
        {"auth_mode": "bearer", "token": "test-token"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=make_noop_security_guard(),
    )


register_conformance_connector("rest", "rest_connector")


# ── npm / pypi (VCR-backed, no live credential needed) ────────────────────
# These connectors can be instantiated without credentials but do NOT support
# the standard conformance resources ("directory" read / "file" write) — they
# have their own resource model (package, search, etc.).  They are tested in
# dedicated VCR-backed contract tests (test_npm_contract.py,
# test_pypi_contract.py) and are NOT registered for the shared conformance
# suite.
#
# NOTE — codeclimate is a REST API client that requires a running
# remote service (Code Climate API).  codeclimate requires an API token.
# It is excluded from conformance because it needs live network access.


@pytest.fixture
def npm_connector():
    from modulo.connectors.npm import NpmConnector

    return NpmConnector(token="")


@pytest.fixture
def pypi_connector():
    from modulo.connectors.pypi import PyPIConnector

    return PyPIConnector(token="")


# ── Auto-parametrisation hook ──────────────────────────────────────────────


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "connector_type" in metafunc.fixturenames:
        types = get_registered_types()
        if not types:
            pytest.fail(
                "No connectors registered for conformance testing: call "
                "register_conformance_connector() from a connector test module."
            )
        metafunc.parametrize("connector_type", types, ids=types)


@pytest.fixture
def conformance_connector(connector_type: str, request: pytest.FixtureRequest) -> ConnectorBase:
    fixture_name = get_registered_fixture(connector_type)
    if fixture_name is None:
        pytest.fail(f"No fixture registered for connector type {connector_type!r}")
    if not request.session._fixturemanager.getfixturedefs(fixture_name, request.node):
        pytest.fail(
            f"Fixture {fixture_name!r} registered for connector type {connector_type!r} does not exist: "
            "fix the register_conformance_connector() call in the connector test module"
        )
    return request.getfixturevalue(fixture_name)
