"""Unit tests for DeterminationScanner — uses mocked connectors."""

import contextlib
import json
import uuid
from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from modulo.core.connector_hub import ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.determination.scanner import run_scan

pytestmark = pytest.mark.asyncio(loop_scope="module")

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


def _fake_ci(
    connector_type_id: str,
    creds: dict | None = None,
    config: dict | None = None,
) -> SimpleNamespace:
    """Minimal fake ConnectorInstance."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        connector_type_id=connector_type_id,
        config_json=config or {},
        credentials_ciphertext=_encrypt(creds or {}),
        visibility="org",
        allowed_operations=None,
    )


@pytest.fixture
def hub_factory() -> Generator[Callable[..., ConnectorHub], None, None]:
    """Return a factory that builds ConnectorHubs with a mocked secrets backend.

    The mocked ``get_secret`` decrypts each connector's stored ciphertext, keyed
    by connector id, so multiple connectors can be initialised with distinct
    credentials.  The patch is stopped after each test.
    """

    patchers: list[Any] = []

    def _make(*instances: object) -> ConnectorHub:
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
        hub = ConnectorHub(secrets_backend=backend)
        creds_by_id: dict[str, str] = {}
        for ci in instances:
            payload = getattr(ci, "credentials_ciphertext", None)
            if payload:
                with contextlib.suppress(ValueError, TypeError):
                    creds_by_id[str(ci.id)] = Fernet(_KEY.encode()).decrypt(payload).decode()

        def _get_secret(connector_id: str) -> str:
            return creds_by_id.get(connector_id, "{}")

        patcher = patch.object(backend, "get_secret", side_effect=_get_secret)
        patcher.start()
        patchers.append(patcher)
        return hub

    yield _make
    for patcher in patchers:
        patcher.stop()


_GITHUB_API = "https://api.github.com"
_GITLAB_API = "https://gitlab.com/api/v4"
_JIRA_BASE = "https://test-domain.atlassian.net/rest/api/3"
_LINEAR_API = "https://api.linear.app/graphql"


@respx.mock
async def test_github_scan(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
    respx.get(f"{_GITHUB_API}/user/repos").mock(
        httpx.Response(200, json=[{"full_name": "owner/repo1"}, {"full_name": "owner/repo2"}])
    )
    respx.get(f"{_GITHUB_API}/repos/owner/repo1/pulls").mock(
        httpx.Response(200, json=[{"number": 1, "created_at": "2026-06-20T00:00:00Z"}])
    )
    respx.get(f"{_GITHUB_API}/repos/owner/repo2/pulls").mock(httpx.Response(200, json=[]))

    samples = await run_scan(hub)
    assert len(samples) >= 2
    resources = {s.resource for s in samples}
    assert "repos" in resources
    assert "pulls" in resources
    assert all(s.error is None for s in samples)


@respx.mock
async def test_gitlab_scan(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("gitlab", creds={"token": "glpat_test"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_GITLAB_API}/user").mock(httpx.Response(200, json={"username": "testuser"}))
    respx.get(f"{_GITLAB_API}/projects").mock(httpx.Response(200, json=[{"id": 1, "name": "proj1"}]))
    respx.get(path__regex=r".*merge_requests.*").mock(httpx.Response(200, json=[{"id": 42, "title": "MR 1"}]))

    samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "projects" in resources
    assert "mrs" in resources


@respx.mock
async def test_jira_scan(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci(
        "jira",
        creds={"email": "user@test.com", "api_token": "token"},
        config={"instance": "test-domain.atlassian.net"},
    )
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_JIRA_BASE}/myself").mock(httpx.Response(200, json={"displayName": "Test User"}))
    respx.post(f"{_JIRA_BASE}/search").mock(
        httpx.Response(
            200,
            json={"issues": [{"id": "1", "key": "PROJ-1"}], "total": 1},
        )
    )

    samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "issues" in resources


@respx.mock
async def test_linear_scan(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("linear", creds={"api_key": "lin_key"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.post(f"{_LINEAR_API}/graphql").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "viewer": {"id": "u1", "name": "User", "email": "u@test.com"},
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "searchIssues": {
                            "nodes": [
                                {
                                    "id": "i1",
                                    "identifier": "PROJ-1",
                                    "title": "Bug",
                                    "state": {"name": "Todo"},
                                }
                            ]
                        }
                    }
                },
            ),
        ]
    )

    samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "issues" in resources


@respx.mock
async def test_filesystem_connector_skipped(hub_factory: Callable[..., ConnectorHub], tmp_path) -> None:
    ci = _fake_ci("filesystem", config={"base_path": str(tmp_path)})
    hub = hub_factory(ci)
    await hub.initialise([ci])
    samples = await run_scan(hub)
    assert len(samples) == 0


@respx.mock
async def test_health_check_failure_still_attempts_queries(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("github", creds={"token": "bad_token"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(401, text="Unauthorized"))

    samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert len(resources) > 0


@respx.mock
async def test_empty_hub_returns_no_samples(hub_factory: Callable[..., ConnectorHub]) -> None:
    hub = hub_factory()
    samples = await run_scan(hub)
    assert len(samples) == 0


@respx.mock
async def test_connector_query_error_returns_error_in_sample(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
    # Repos endpoint fails; pulls won't be queried since repos list is empty
    respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(500, text="Server Error"))

    samples = await run_scan(hub)
    errored = [s for s in samples if s.error]
    assert len(errored) == 1
    assert "500" in errored[0].error


@respx.mock
async def test_scan_yields_repos_sample_when_repo_list_empty(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
    respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(200, json=[]))

    samples = await run_scan(hub)
    assert len(samples) >= 1
    resources = {s.resource for s in samples}
    assert "repos" in resources


@respx.mock
async def test_multiple_connectors_scanned_independently(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci1 = _fake_ci("github", creds={"token": "ghp_one"})
    ci2 = _fake_ci("github", creds={"token": "ghp_two"})
    hub = hub_factory(ci1, ci2)
    await hub.initialise([ci1, ci2])

    respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
    respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(200, json=[{"full_name": "owner/repo1"}]))
    respx.get(f"{_GITHUB_API}/repos/owner/repo1/pulls").mock(httpx.Response(200, json=[]))

    samples = await run_scan(hub)
    assert len(samples) >= 1
    assert all(s.error is None for s in samples)


@respx.mock
async def test_query_failures_are_reported_as_error_samples(hub_factory: Callable[..., ConnectorHub]) -> None:
    ci = _fake_ci("github", creds={"token": "bad_token"})
    hub = hub_factory(ci)
    await hub.initialise([ci])

    respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(401, text="Unauthorized"))
    respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(401, text="Unauthorized"))

    samples = await run_scan(hub)
    assert len(samples) > 0
    assert any(s.error for s in samples)
