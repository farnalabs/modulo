"""Connector conformance fixtures — NOT the project-level conftest."""

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
