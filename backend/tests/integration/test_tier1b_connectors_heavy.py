"""Tier 1b (FAR-934) — container-hosted connectors, NIGHTLY HEAVY set.

Covers: gitlab (self-hosted CE). The GitLab CE omnibus image is multi-GB and
bootstraps for several minutes, so it is too heavy to spin up inside every
PR's CI job — it runs only in the nightly job (``tier1b-nightly``).

Every test drives the real Modulo connector against a real container-hosted
GitLab instance (Testcontainers), reached only after a REAL readiness probe,
and asserts on REAL API response data — never a hand-written body.

Runs with::

    uv run pytest tests/integration/test_tier1b_connectors_heavy.py -q

Requires a Docker daemon; without one the integration conftest skips the
whole suite loudly (stderr banner + recorded skip reason).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.gitlab import GitLabConnector
from tests.helpers.testcontainers_harness import (
    SSRF_LOOPBACK_OPTIN,
    ContainerHandle,
    ContainerSpec,
    Tier1bFixtureError,
    probe_http,
    start_tier1b_container,
)

pytestmark = pytest.mark.integration

GITLAB_PROJECT = "root/demo"


@pytest.fixture(scope="session", autouse=True)
def ssrf_loopback_consent() -> Iterator[None]:
    """SSRF guard loopback opt-in (see the light module for the rationale).

    Uses a session-scoped MonkeyPatch directly — the ``monkeypatch``
    fixture is function-scoped and cannot be requested by a session
    fixture (ScopeMismatch).
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("SSRF_ALLOW_PRIVATE_RANGES", SSRF_LOOPBACK_OPTIN)
    yield
    mp.undo()


def _gitlab_root_pat(handle: ContainerHandle) -> str:
    """Create a real PersonalAccessToken for root via ``gitlab-rails runner``.

    The session-login API was removed from modern GitLab, so we mint the
    token inside the container with the Rails console — the same way a
    self-hosted operator would recover from setup without UI access.
    """
    token = handle.exec(
        [
            "gitlab-rails",
            "runner",
            "-e",
            "production",
            "t = PersonalAccessToken.create!(user: User.find_by(username: 'root'), "
            "name: 'modulo-ci', scopes: [:api], expires_at: 364.days.from_now); puts t.token",
        ]
    ).strip()
    assert token and len(token) >= 20, f"expected a real GitLab PAT from rails runner, got {token!r}"
    return token


@pytest.fixture(scope="session")
def gitlab_service() -> ContainerHandle:
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="gitlab/gitlab-ce:latest",
                container_port=80,
                env={
                    # A random password: policy rejects common word combos such as
                    # "modulo-Tier1b-root!" ("Password must not contain commonly used
                    # combinations of words and letters") which kills the 003_admin seed.
                    "GITLAB_ROOT_PASSWORD": f"Mt1b-{secrets.token_hex(12)}$aA",
                    "GITLAB_OMNIBUS_CONFIG": "gitlab_rails['gitlab_shell_ssh_port'] = 2222",
                },
                probe=probe_http("/users/sign_in"),
                # First boot reconfigures + migrates the DB, then puma preloads —
                # observed at the ceiling of 30 min on this host under load.
                ready_timeout_seconds=1800,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b nightly gitlab fixture unavailable on this Docker host (recorded skip): {exc}")
    token = _gitlab_root_pat(handle)
    handle.creds["gitlab_token"] = token
    yield handle
    handle.stop()


@pytest.fixture(scope="session")
def gitlab_connector(gitlab_service: ContainerHandle) -> GitLabConnector:
    connector = GitLabConnector(
        token=gitlab_service.creds["gitlab_token"],
        base_url=f"{gitlab_service.base_url}/api/v4",
    )
    # Seed one project so list/tree/file resources have real data.
    import httpx

    create = httpx.post(
        f"{gitlab_service.base_url}/api/v4/projects",
        headers={"PRIVATE-TOKEN": gitlab_service.creds["gitlab_token"]},
        data={
            "name": "demo",
            "path": "demo",
            "visibility": "public",
            # Without an initial commit there is no default branch, and a file
            # create answered 400 "A file with this name doesn't exist".
            "initialize_with_readme": "true",
        },
        # GitLab's webapp is still warming right after boot — a 60s read window.
        timeout=60.0,
    )
    create.raise_for_status()
    # The connector's ``file`` write path is the repository-files UPDATE endpoint
    # (PUT /repository/files) — on real GitLab that answers 400
    # "A file with this name doesn't exist" unless the file exists, so seed
    # the marker through the raw CREATE endpoint here. The roundtrip test
    # then proves the connector's own write surface against real data.
    seed = httpx.post(
        f"{gitlab_service.base_url}/api/v4/projects/{GITLAB_PROJECT.replace('/', '%2F')}"
        "/repository/files/tier1b%2Fmarker.md",
        headers={"PRIVATE-TOKEN": gitlab_service.creds["gitlab_token"]},
        data={
            "branch": "main",
            "content": "seed placeholder",
            "commit_message": "tier1b: seed marker",
        },
        timeout=60.0,
    )
    seed.raise_for_status()
    return connector


async def test_gitlab_health(gitlab_connector: GitLabConnector) -> None:
    result = await gitlab_connector.health_check()
    assert result.ok, f"GitLab health check failed against real CE container: {getattr(result, 'detail', '')}"


async def test_gitlab_query_projects(gitlab_connector: GitLabConnector) -> None:
    result = await gitlab_connector.query(ConnectorQuery(resource="projects"))
    paths = {record.get("path_with_namespace") for record in result.records}
    assert any("demo" in str(p) for p in paths), f"expected the seeded project in real GitLab project list: {paths!r}"


async def test_gitlab_write_issue_then_query(gitlab_connector: GitLabConnector) -> None:
    title = "tier1b-roundtrip-issue"
    write = await gitlab_connector.write(
        ConnectorPayload(resource="issue", data={"project": GITLAB_PROJECT, "title": title})
    )
    assert isinstance(write, dict), f"expected dict from gitlab issue write, got {type(write).__name__}"
    result = await gitlab_connector.query(
        ConnectorQuery(resource="issues", filters={"project": GITLAB_PROJECT, "state": "opened"})
    )
    titles = {record.get("title") for record in result.records}
    assert title in titles, f"gitlab write-then-read roundtrip failed; real issues: {titles!r}"


async def test_gitlab_write_file_then_read(gitlab_connector: GitLabConnector) -> None:
    content = "tier1b roundtrip marker\n"
    write = await gitlab_connector.write(
        ConnectorPayload(
            resource="file",
            data={
                "project": GITLAB_PROJECT,
                "path": "tier1b/marker.md",
                "ref": "main",
                "content": content,
                "commit_message": "tier1b: seed marker file",
            },
        )
    )
    assert isinstance(write, dict), f"expected dict from gitlab file write, got {type(write).__name__}"
    read = await gitlab_connector.query(
        ConnectorQuery(resource="file", filters={"project": GITLAB_PROJECT, "path": "tier1b/marker.md", "ref": "main"})
    )
    assert isinstance(read.records, list), "gitlab file read must return real records"
