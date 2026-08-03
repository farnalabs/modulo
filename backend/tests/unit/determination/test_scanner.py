"""Unit tests for DeterminationScanner — uses mocked connectors."""

import contextlib
import json
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import respx
from cryptography.fernet import Fernet

from modulo.connectors.base import ConnectorQuery, ConnectorType
from modulo.core.connector_hub import ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.determination.scanner import run_scan

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


def _fake_ci(
    connector_type_id: str,
    creds: dict | None = None,
    config: dict | None = None,
) -> object:
    """Minimal fake ConnectorInstance."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        connector_type_id=connector_type_id,
        config_json=config or {},
        credentials_ciphertext=_encrypt(creds or {}),
        visibility="org",
        allowed_operations=None,
    )


@contextlib.asynccontextmanager
async def _hub(ci: object) -> AsyncGenerator[ConnectorHub, None]:
    """Create a ConnectorHub with a mocked secrets backend."""
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend)
    # The initialise method calls backend.get_secret; mock it to return
    # the credentials that _fake_ci stored. We don't have a DB session so
    # we bypass the real backend lookup.
    ci_creds_json = "{}"
    payload = getattr(ci, "credentials_ciphertext", None)
    if payload:
        with contextlib.suppress(ValueError, TypeError):
            ci_creds_json = Fernet(_KEY.encode()).decrypt(payload).decode()
    with patch.object(backend, "get_secret", return_value=ci_creds_json):
        yield hub


_GITHUB_API = "https://api.github.com"
_GITLAB_API = "https://gitlab.com/api/v4"
_JIRA_BASE = "https://test-domain.atlassian.net/rest/api/3"
_LINEAR_API = "https://api.linear.app/graphql"


@respx.mock
async def test_github_scan() -> None:
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    async with _hub(ci) as hub:
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
    assert len(samples) >= 2  # repos + pulls
    resources = {s.resource for s in samples}
    assert "repos" in resources
    assert "pulls" in resources
    assert all(s.error is None for s in samples)


@respx.mock
async def test_gitlab_scan() -> None:
    ci = _fake_ci("gitlab", creds={"token": "glpat_test"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITLAB_API}/user").mock(httpx.Response(200, json={"username": "testuser"}))
        respx.get(f"{_GITLAB_API}/projects").mock(httpx.Response(200, json=[{"id": 1, "name": "proj1"}]))
        respx.get(path__regex=r".*merge_requests.*").mock(httpx.Response(200, json=[{"id": 42, "title": "MR 1"}]))

        samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "projects" in resources
    assert "mrs" in resources


@respx.mock
async def test_jira_scan() -> None:
    ci = _fake_ci(
        "jira",
        creds={"email": "user@test.com", "api_token": "token"},
        config={"instance": "test-domain.atlassian.net"},
    )
    async with _hub(ci) as hub:
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
async def test_linear_scan() -> None:
    ci = _fake_ci("linear", creds={"api_key": "lin_key"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.post(f"{_LINEAR_API}/graphql").side_effect = [
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

        samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "issues" in resources


@respx.mock
async def test_filesystem_connector_skipped() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        ci = _fake_ci("filesystem", config={"base_path": tmpdir})
        async with _hub(ci) as hub:
            await hub.initialise([ci])
            samples = await run_scan(hub)
    assert len(samples) == 0  # filesystem is not sampled


@respx.mock
async def test_health_check_failure_still_attempts_queries() -> None:
    ci = _fake_ci("github", creds={"token": "bad_token"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        # Health check returns 401 but doesn't raise; scanner proceeds to attempt queries
        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(401, text="Unauthorized"))

        samples = await run_scan(hub)
    # Scanner attempts queries despite health check failure — individual query errors
    # are captured as error samples, not as an unhandled exception
    resources = {s.resource for s in samples}
    assert len(resources) > 0


@respx.mock
async def test_empty_hub_returns_no_samples() -> None:
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    samples = await run_scan(hub)
    assert len(samples) == 0


@respx.mock
async def test_connector_query_error_returns_error_in_sample() -> None:
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
        # Repos endpoint fails; pulls won't be queried since repos list is empty
        respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(500, text="Server Error"))

        samples = await run_scan(hub)
    errored = [s for s in samples if s.error]
    assert len(errored) == 1
    assert errored[0].resource == "repos"
    assert errored[0].records == []
    assert errored[0].sample_count == 0
    assert "500" in errored[0].error


@respx.mock
async def test_connector_with_no_repos_produces_repos_sample() -> None:
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
        respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(200, json=[]))

        samples = await run_scan(hub)
    assert len(samples) >= 1
    resources = {s.resource for s in samples}
    assert "repos" in resources


@respx.mock
async def test_pull_query_failure_produces_error_sample() -> None:
    """A failed per-repo pulls query must surface as an error sample, not crash the scan."""
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
        respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(200, json=[{"full_name": "owner/repo1"}]))
        respx.get(f"{_GITHUB_API}/repos/owner/repo1/pulls").mock(httpx.Response(500, text="Server Error"))

        samples = await run_scan(hub)
    by_resource = {s.resource: s for s in samples}
    assert "repos" in by_resource
    assert by_resource["repos"].error is None
    assert "pulls" in by_resource
    assert by_resource["pulls"].error is not None
    assert by_resource["pulls"].records == []
    assert by_resource["pulls"].sample_count == 0
    assert "500" in by_resource["pulls"].error


@respx.mock
async def test_nameless_repo_skips_pull_query() -> None:
    """A repo record with no name must not trigger a per-repo pulls query."""
    ci = _fake_ci("github", creds={"token": "ghp_test"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
        respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(200, json=[{"id": 1}]))

        samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "repos" in resources
    assert "pulls" not in resources
    repos_sample = next(s for s in samples if s.resource == "repos")
    assert repos_sample.error is None


@respx.mock
async def test_multiple_connectors_scanned_independently() -> None:
    ci1 = _fake_ci("github", creds={"token": "ghp_one"})
    ci2 = _fake_ci("github", creds={"token": "ghp_two"})
    async with _hub(ci1) as hub:
        await hub.initialise([ci1, ci2])

        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(200, json={"login": "octocat"}))
        respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(200, json=[{"full_name": "owner/repo1"}]))
        respx.get(f"{_GITHUB_API}/repos/owner/repo1/pulls").mock(httpx.Response(200, json=[]))

        samples = await run_scan(hub)
    assert len(samples) >= 1
    assert all(s.error is None for s in samples)


@respx.mock
async def test_all_health_checks_fail_returns_error_samples() -> None:
    ci = _fake_ci("github", creds={"token": "bad_token"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITHUB_API}/user").mock(httpx.Response(401, text="Unauthorized"))
        respx.get(f"{_GITHUB_API}/user/repos").mock(httpx.Response(401, text="Unauthorized"))

        samples = await run_scan(hub)
    assert len(samples) > 0
    assert any(s.error for s in samples)


@respx.mock
async def test_connector_whose_health_check_raises_is_skipped() -> None:
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    broken_id = uuid.uuid4()
    healthy_id = uuid.uuid4()

    class _ExplodingConnector:
        connector_type = ConnectorType.GITHUB

        async def health_check(self) -> None:
            raise RuntimeError("health check exploded")

        async def query(self, query: ConnectorQuery) -> None:  # pragma: no cover - never reached
            raise AssertionError("query should not be called on a broken connector")

    class _HealthyConnector:
        connector_type = ConnectorType.GITHUB

        async def health_check(self) -> None:
            return None

        async def query(self, query: ConnectorQuery) -> SimpleNamespace:
            return SimpleNamespace(records=[])

    hub._connectors = {broken_id: _ExplodingConnector(), healthy_id: _HealthyConnector()}
    hub._initialised = True

    samples = await run_scan(hub)
    # The broken connector must not crash the scan or produce samples;
    # the healthy connector's repos sample must still be collected.
    assert len(samples) == 1
    assert samples[0].connector_id == healthy_id
    assert samples[0].resource == "repos"
    assert samples[0].error is None
