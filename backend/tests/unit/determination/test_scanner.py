"""Unit tests for DeterminationScanner — uses mocked connectors."""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from modulo.connectors.base import ConnectorQuery, ConnectorType
from modulo.core.connector_hub import ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend
from modulo.determination.scanner import _repo_name, run_scan

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
        respx.get("https://gitlab.com/oauth/token/info").mock(
            httpx.Response(200, json={"scope": ["read_api", "write_repository", "api"]})
        )

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
async def test_filesystem_connector_skipped() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        ci = _fake_ci("filesystem", config={"base_path": tmpdir})
        async with _hub(ci) as hub:
            await hub.initialise([ci])
            samples = await run_scan(hub)
    assert len(samples) == 0  # filesystem is not sampled


async def test_unsupported_connector_type_produces_no_samples() -> None:
    """A connector whose type is not sampled must be health-checked but yield no samples."""
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    cid = uuid.uuid4()

    class _SlackConnector:
        connector_type = ConnectorType.SLACK

        async def health_check(self) -> None:
            return None

        async def query(self, query: ConnectorQuery) -> SimpleNamespace:  # pragma: no cover - never reached
            raise AssertionError("query should not be reached for an unsampled connector type")

    hub._connectors = {cid: _SlackConnector()}
    hub._initialised = True

    samples = await run_scan(hub)
    assert samples == []


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
    assert not samples


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
    assert not errored[0].records
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
    assert not by_resource["pulls"].records
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
async def test_nameless_project_skips_mr_query() -> None:
    """A GitLab project record with no name must not trigger a per-project MR query."""
    ci = _fake_ci("gitlab", creds={"token": "glpat_test"})
    async with _hub(ci) as hub:
        await hub.initialise([ci])

        respx.get(f"{_GITLAB_API}/user").mock(httpx.Response(200, json={"username": "testuser"}))
        respx.get(f"{_GITLAB_API}/projects").mock(httpx.Response(200, json=[{"id": 1, "name": "proj1"}, {"id": 2}]))
        respx.get(path__regex=r".*merge_requests.*").mock(httpx.Response(200, json=[{"id": 42, "title": "MR 1"}]))
        respx.get("https://gitlab.com/oauth/token/info").mock(
            httpx.Response(200, json={"scope": ["read_api", "write_repository", "api"]})
        )

        samples = await run_scan(hub)
    resources = {s.resource for s in samples}
    assert "projects" in resources
    assert "mrs" in resources
    projects_sample = next(s for s in samples if s.resource == "projects")
    assert len(projects_sample.records) == 2
    # Only the named project triggers an MR query; the nameless one is skipped.
    mrs_samples = [s for s in samples if s.resource == "mrs"]
    assert len(mrs_samples) == 1
    assert mrs_samples[0].error is None


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


async def test_connector_query_cancellation_propagates() -> None:
    """A CancelledError from a connector query must propagate out of run_scan."""
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    cid = uuid.uuid4()

    class _CancellingConnector:
        connector_type = ConnectorType.GITHUB

        async def health_check(self) -> None:
            return None

        async def query(self, query: ConnectorQuery) -> None:  # pragma: no cover - always cancelled
            raise asyncio.CancelledError

    hub._connectors = {cid: _CancellingConnector()}
    hub._initialised = True

    with pytest.raises(asyncio.CancelledError):
        await run_scan(hub)


async def test_connector_health_check_cancellation_propagates() -> None:
    """A CancelledError from health_check must propagate out of run_scan."""
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    cid = uuid.uuid4()

    class _CancellingHealthCheck:
        connector_type = ConnectorType.GITHUB

        async def health_check(self) -> None:
            raise asyncio.CancelledError

        async def query(self, query: ConnectorQuery) -> None:  # pragma: no cover - never reached
            raise AssertionError("query should not be reached")

    hub._connectors = {cid: _CancellingHealthCheck()}
    hub._initialised = True

    with pytest.raises(asyncio.CancelledError):
        await run_scan(hub)


async def test_hub_get_key_error_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """A connector_id missing from the hub must be logged and skipped, not abort the scan."""
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    hub._connectors = {uuid.uuid4(): object()}
    hub._initialised = True

    with patch.object(hub, "get", side_effect=KeyError("missing")):
        samples = await run_scan(hub)

    assert samples == []
    assert "Connector " in caplog.text
    assert "not found in hub during scan" in caplog.text


async def test_hub_get_unexpected_error_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """An unexpected hub.get() error must be logged and skipped, not abort the scan."""
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    hub._connectors = {uuid.uuid4(): object()}
    hub._initialised = True

    with patch.object(hub, "get", side_effect=RuntimeError("retrieval exploded")):
        samples = await run_scan(hub)

    assert samples == []
    assert "retrieval failed" in caplog.text
    assert "retrieval exploded" in caplog.text


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


# ---------------------------------------------------------------------------
# _repo_name
# ---------------------------------------------------------------------------


def test_repo_name_prefers_full_name_over_name() -> None:
    assert _repo_name({"full_name": "owner/repo", "name": "repo"}) == "owner/repo"


def test_repo_name_gitlab_path_with_namespace() -> None:
    assert _repo_name({"path_with_namespace": "group/proj", "name": "proj"}) == "group/proj"


def test_repo_name_falls_back_to_plain_name() -> None:
    assert _repo_name({"name": "repo-a"}) == "repo-a"


def test_repo_name_ignores_non_string_values() -> None:
    assert not _repo_name({"name": 123})
    assert not _repo_name({})


# ---------------------------------------------------------------------------
# Query timeout / limit edge cases
# ---------------------------------------------------------------------------


@respx.mock
async def test_connector_query_timeout_produces_error_sample() -> None:
    """A connector whose query hangs must be cut off and surfaced as an error sample."""
    hub = ConnectorHub(secrets_backend=create_secrets_backend(fernet_key=_KEY))
    cid = uuid.uuid4()

    class _SlowConnector:
        connector_type = ConnectorType.GITHUB

        async def health_check(self) -> None:
            return None

        async def query(self, query: ConnectorQuery) -> SimpleNamespace:  # pragma: no cover - never returns
            await asyncio.sleep(60)
            raise AssertionError("query should be interrupted by the timeout")

    hub._connectors = {cid: _SlowConnector()}
    hub._initialised = True

    with patch("modulo.determination.scanner._QUERY_TIMEOUT", 0.05):
        samples = await run_scan(hub)

    assert len(samples) == 1
    assert samples[0].connector_id == cid
    assert samples[0].resource == "repos"
    assert not samples[0].records
    assert samples[0].error is not None
    assert "timed out" in samples[0].error
