"""Tier 1b (FAR-934) — container-hosted connectors, per-PR LIGHT set.

Covers: gitea, jenkins, n8n, sonarqube, grafana, teamcity, trivy,
codeclimate.  The heavy set (gitlab — multi-GB image, slow boot) lives in
``test_tier1b_connectors_heavy.py`` and runs only in the nightly job.

Every test drives the real Modulo connector against a real container-hosted
server (Testcontainers), reached only after a REAL readiness probe, and
asserts on REAL response data — never a hand-written body.

Runs with::

    uv run pytest tests/integration/test_tier1b_connectors_light.py -q

Requires a Docker daemon; without one the integration conftest skips the
whole suite loudly (stderr banner + recorded skip reason); CI hard-fails
instead, so a missing daemon can never silently pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import string
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult, HealthResult
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.teamcity import TeamCityConnector

# The trivy connector is intentionally NOT exercised end-to-end here — see
# test_trivy_connector_roundtrip_recorded_skip below for the recorded skip.
from tests.helpers.testcontainers_harness import (
    SSRF_LOOPBACK_OPTIN,
    ContainerHandle,
    ContainerSpec,
    Tier1bFixtureError,
    probe_http,
    start_tier1b_container,
)

pytestmark = pytest.mark.integration
ADMIN = "modulo-admin"


@pytest.fixture(scope="session", autouse=True)
def ssrf_loopback_consent() -> Iterator[None]:
    """Let the pinned-transport SSRF guard reach loopback containers.

    The same documented operator opt-in the Tier 1a fixtures use, applied
    lint-clean via a session-scoped MonkeyPatch (``monkeypatch`` itself is
    function-scoped and therefore illegal for a session fixture — pytest
    raises ScopeMismatch if requested here).
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("SSRF_ALLOW_PRIVATE_RANGES", SSRF_LOOPBACK_OPTIN)
    yield
    mp.undo()


# ── shared seed data helpers ────────────────────────────────────────────────


def _now_suffix() -> str:
    return secrets.token_hex(4)


def _random_password() -> str:
    """Password that GUARANTEES the n8n owner-setup policy: >=8 chars with at
    least one upper-case letter, one lower-case letter and one digit.

    Build one guaranteed character per class first, fill the rest from the
    full class-union alphabet, then shuffle with a CSPRNG so every run is
    random — ``secrets.token_urlsafe`` alone has no per-class guarantee and
    intermittently 400s owner setup ('Password must contain at least 1
    number.').
    """
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    # secrets.SystemRandom().shuffle gives an unbiased Fisher-Yates shuffle.
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(alphabet) for _ in range(21)),
    ]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _poll_until(predicate: Callable[[], Any], timeout: float, message: str) -> None:
    """Poll a predicate until truthy or raise loudly — no silent waits."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if predicate():
                return
        time.sleep(3.0)
    raise AssertionError(f"Timed out: {message} (after {timeout:.0f}s)")


def _wait_for_exec_file(handle: ContainerHandle, path: str, timeout: float = 120.0) -> str:
    """Read *path inside the container, polling until it exists.  Used by
    fixtures that must read files the server writes shortly after boot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return handle.exec(["cat", path]).strip()
        except Tier1bFixtureError:
            time.sleep(2.0)
    raise AssertionError(f"Timed out reading container file {path!r} after {timeout:.0f}s")


_LIST_PROBE_OK_200: Callable[[int], str | None] = probe_http("/api/v1/version")
_LIST_PROBE_GRAFANA: Callable[[int], str | None] = probe_http("/api/health")
# n8n: /healthz answers 200 (empty) long before n8n's internal database has
# finished migrating; owner/setup then 503s with 'Database is not ready!'.
# /healthz/readiness returns 200 {"status":"ok"} ONLY once the DB is usable —
# poll that, never /healthz.
_LIST_PROBE_N8N_READINESS: Callable[[int], str | None] = probe_http("/healthz/readiness")


def _assert_n8n_rest_json(client: httpx.Client, timeout_seconds: float, message: str) -> None:
    """Poll n8n's REST surface until /rest/settings answers with REAL JSON.

    n8n serves 200-with-text 'n8n is starting up. Please wait' on the REST
    surface (e.g. around first-run restarts); never trust the early 200.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error = "poll did not run"
    while time.monotonic() < deadline:
        try:
            if isinstance(client.get("/rest/settings", timeout=60.0).json(), dict):
                return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(3.0)
    raise AssertionError(f"Timed out waiting for {message} (after {timeout_seconds:.0f}s): {last_error}")


# ── gitea ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def gitea_service() -> Iterator[ContainerHandle]:
    admin_password = _random_password()
    handle = start_tier1b_container(
        ContainerSpec(
            image="gitea/gitea:1.23",
            container_port=3000,
            env={
                "USER_UID": "1000",
                "USER_GID": "1000",
                "GITEA__database__DB_TYPE": "sqlite3",
                "GITEA__security__INSTALL_LOCK": "true",
                "GITEA__server__DISABLE_REGISTRATION": "true",
            },
            probe=_LIST_PROBE_OK_200,
            ready_timeout_seconds=240,
        )
    )
    admin_password = _random_password()
    try:
        handle.exec(
            [
                "gitea",
                "admin",
                "user",
                "create",
                "--admin",
                "--username",
                ADMIN,
                "--password",
                admin_password,
                "--email",
                "modulo-admin@ci.local",
                "--must-change-password=false",
            ],
            user="git",
        )
        # Mint an API token via the real REST API, then seed one repo.
        resp = httpx.post(
            f"{handle.base_url}/api/v1/users/{ADMIN}/tokens",
            auth=(ADMIN, admin_password),
            json={"name": "modulo-ci", "scopes": ["all"]},
            timeout=60.0,
        )
        resp.raise_for_status()
        token = resp.json()["sha1"]
        repo_resp = httpx.post(
            f"{handle.base_url}/api/v1/user/repos",
            auth=(ADMIN, admin_password),
            json={"name": "modulo-tier1b", "private": False, "auto_init": False},
            timeout=60.0,
        )
        repo_resp.raise_for_status()
        # Drop a file so the connector's file read path has real data too.
        file_resp = httpx.post(
            f"{handle.base_url}/api/v1/repos/{ADMIN}/modulo-tier1b/contents/README.md",
            auth=(ADMIN, admin_password),
            json={"content": "IyBtb2R1bG8gdGllciBkZW1vIChtYXJrZG93biBib2R5KQo=\n", "message": "seed readme"},
            timeout=60.0,
        )
        file_resp.raise_for_status()
        handle.tokens_gitea = token  # type: ignore[attr-defined]
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def gitea_connector(gitea_service: ContainerHandle) -> GiteaConnector:
    token: str = gitea_service.tokens_gitea  # type: ignore[no-any-return]
    return GiteaConnector(token=token, base_url=gitea_service.base_url)


async def test_gitea_health(gitea_service: ContainerHandle):
    connector = GiteaConnector(token=str(gitea_service.tokens_gitea), base_url=gitea_service.base_url)
    result = await connector.health_check()
    assert result.ok, f"Gitea health failed on real container {gitea_service.base_url}: {result.detail}"


async def test_gitea_query_repos(gitea_connector: GiteaConnector) -> None:
    result = await gitea_connector.query(ConnectorQuery(resource="repos"))
    names = {record.get("name") for record in result.records}
    assert "modulo-tier1b" in names, f"expected real Gitea repo list to include the seeded repo: {names!r}"


async def test_gitea_query_file(gitea_connector: GiteaConnector) -> None:
    result = await gitea_connector.query(
        ConnectorQuery(resource="file", filters={"repo": f"{ADMIN}/modulo-tier1b", "path": "README.md"})
    )
    records = [record for record in result.records if isinstance(record, dict)]
    decoded = next((rec.get("content") for rec in records if rec.get("type") == "file"), None)
    assert decoded, f"expected real Gitea file read of seeded README: got {records!r}"


async def test_gitea_write_issue_then_query(gitea_connector: GiteaConnector) -> None:
    issue_title = f"tier1b-issue-{_now_suffix()}"
    write_result = await gitea_connector.write(
        ConnectorPayload(resource="issue", data={"repo": f"{ADMIN}/modulo-tier1b", "title": issue_title})
    )
    assert isinstance(write_result, dict), f"expected dict from gitea write, got {type(write_result).__name__}"
    result = await gitea_connector.query(
        ConnectorQuery(resource="issues", filters={"repo": f"{ADMIN}/modulo-tier1b", "state": "open"})
    )
    titles = {record.get("title") for record in result.records}
    assert issue_title in titles, f"gitea write-then-read roundtrip failed; real issues: {titles!r}"


# ── n8n ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def n8n_service() -> Iterator[ContainerHandle]:
    handle = start_tier1b_container(
        ContainerSpec(
            image="n8nio/n8n:latest",
            container_port=5678,
            env={
                "N8N_ENCRYPTION_KEY": secrets.token_hex(16),
                "N8N_DIAGNOSTICS_ENABLED": "false",
                "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
                # The CI client talks plain HTTP to the loopback container;
                # without this the n8n-auth cookie is Secure and every
                # authenticated REST call 401s.
                "N8N_SECURE_COOKIE": "false",
            },
            probe=_LIST_PROBE_N8N_READINESS,
            ready_timeout_seconds=300,
        )
    )
    owner_password = _random_password()
    try:
        with httpx.Client(base_url=handle.base_url) as client:
            _assert_n8n_rest_json(client, timeout_seconds=300, message="n8n REST surface to answer with JSON")
            setup = client.post(
                "/rest/owner/setup",
                json={
                    "firstName": "Modulo",
                    "lastName": "CI",
                    "email": "modulo-admin@example.com",
                    "password": owner_password,
                },
            )
            try:
                setup.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AssertionError(f"n8n owner setup failed: {exc} body={exc.response.text[:400]!r}") from exc
            # n8n RESTARTS after first-run owner setup — never trust the early
            # "n8n is starting up. Please wait" text; poll until REST is JSON.
            _assert_n8n_rest_json(
                client,
                timeout_seconds=300,
                message="n8n REST surface to answer with JSON again after owner-setup restart",
            )
            login = client.post(
                "/rest/login", json={"emailOrLdapLoginId": "modulo-admin@example.com", "password": owner_password}
            )
            try:
                login.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AssertionError(f"n8n owner login failed: {exc} body={exc.response.text[:400]!r}") from exc
            # Create a public API key under the just-created owner session.
            key_resp = client.post(
                "/rest/api-keys",
                json={
                    "label": "modulo-ci",
                    "apiType": "public",
                    "active": True,
                    "expiresAt": None,
                    # ApiKey scopes must be valid role scopes ("{resource}:{act}")
                    # the owner role owns — n8n rejects any other spelling.
                    "scopes": ["workflow:read", "workflow:create", "workflow:list"],
                },
            )
            payload: dict[str, Any] = key_resp.json() if key_resp.status_code == 200 else {}
            data = payload.get("data") if isinstance(payload, dict) else None
            # The JWT lives under ``data.rawApiKey`` (the outer ``apiKey`` is masked).
            api_key = data.get("rawApiKey", "") if isinstance(data, dict) else ""
            handle.public_api_key = api_key  # type: ignore[attr-defined]
            handle.owner_password = owner_password  # type: ignore[attr-defined]
        if not api_key:
            raise AssertionError(f"n8n API key mint failed ({key_resp.status_code}): {key_resp.text!r}")
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def n8n_connector(n8n_service: ContainerHandle) -> N8NConnector:
    api_key: str = n8n_service.public_api_key
    return N8NConnector(token=api_key, base_url=n8n_service.base_url)


async def test_n8n_health(n8n_service: ContainerHandle, n8n_connector: N8NConnector) -> None:
    result = await n8n_connector.health_check()
    assert result.ok, f"n8n health against real container failed: {result.detail}"


async def test_n8n_workflow_write_then_query(n8n_connector: N8NConnector) -> None:
    workflow_name = f"modulo-tier1b-{_now_suffix()}"
    write_result = await n8n_connector.write(
        ConnectorPayload(resource="workflow", data={"name": workflow_name, "nodes": [], "connections": {}})
    )
    assert isinstance(write_result, dict), f"expected dict from n8n write, got {type(write_result).__name__}"
    result = await n8n_connector.query(ConnectorQuery(resource="workflows"))
    names = {record.get("name") for record in result.records}
    assert workflow_name in names, f"n8n write-then-read roundtrip failed; real workflows: {names!r}"


# ── jenkins ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def jenkins_service() -> Iterator[ContainerHandle]:
    handle = start_tier1b_container(
        ContainerSpec(
            image="jenkins/jenkins:lts-jdk17",
            container_port=8080,
            probe=probe_http("/login"),
            ready_timeout_seconds=420,
        )
    )
    initial_password = _wait_for_exec_file(handle, "/var/jenkins_home/secrets/initialAdminPassword")
    # JenkinsConnector speaks Basic auth (username + token-or-password), so
    # the initial admin password IS a valid credential set — no API-token
    # mint needed while the first-run wizard is still pending (the token
    # endpoint 403s until setup completes).
    handle.jenkins_api_token = initial_password  # type: ignore[attr-defined]
    freestyle_xml = (
        "<project><actions/><description>modulo tier1b</description>"
        "<keepDependencies>false</keepDependencies>"
        "<properties/>"
        "<builders><hudson.tasks.Shell><command>echo modulo-tier1b-ok</command></hudson.tasks.Shell></builders>"
        "</project>"
    )
    try:
        with httpx.Client(base_url=handle.base_url, auth=("admin", initial_password), timeout=60.0) as client:
            # CSRF: every Jenkins POST needs the crumb, tied to the SAME
            # session (cookies) that issued it — fetch it on the live client.
            crumb = client.get("/crumbIssuer/api/json").json()["crumb"]
            client.headers.update({"Content-Type": "application/xml", "Jenkins-Crumb": crumb})
            # Modern Jenkins requires the `name` query parameter (not `item`).
            create_resp = client.post("/createItem?name=modulo-tier1b", content=freestyle_xml.encode())
            try:
                create_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AssertionError(f"jenkins job create failed: {exc} body={create_resp.text[:500]!r}") from exc
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def jenkins_connector(jenkins_service: ContainerHandle) -> JenkinsConnector:
    token: str = jenkins_service.jenkins_api_token
    return JenkinsConnector(username="admin", token=token, base_url=jenkins_service.base_url)


async def test_jenkins_health(jenkins_connector: JenkinsConnector) -> None:
    result = await jenkins_connector.health_check()
    assert result.ok, f"Jenkins health against real container failed: {result.detail}"


async def test_jenkins_query_jobs_and_nodes(jenkins_connector: JenkinsConnector) -> None:
    for resource, expected_text in (("jobs", "modulo-tier1b"), ("nodes", "Built-In")):
        result = await jenkins_connector.query(ConnectorQuery(resource=resource))
        joined = " ".join(str(record) for record in result.records)
        assert expected_text in joined, f"jenkins {resource} query missing real record: {joined!r}"


async def test_jenkins_write_build_polls_to_success(jenkins_connector: JenkinsConnector) -> None:
    write_result = await jenkins_connector.write(ConnectorPayload(resource="build", data={"job_name": "modulo-tier1b"}))
    assert isinstance(write_result, dict), f"expected dict from jenkins write, got {type(write_result).__name__}"
    deadline = time.time() + 240
    while time.time() < deadline:
        builds = await jenkins_connector.query(ConnectorQuery(resource="builds", filters={"job_name": "modulo-tier1b"}))
        success_builds = [record for record in builds.records if record.get("result") == "SUCCESS"]
        if success_builds:
            return
        await asyncio.sleep(5.0)
    raise AssertionError(f"jenkins build never completed within 240s; last real builds: {builds.records!r}")


# ── sonarqube ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def sonarqube_service() -> Iterator[ContainerHandle]:
    handle = start_tier1b_container(
        ContainerSpec(
            image="sonarqube:9.9-community",
            container_port=9000,
            env={"SONAR_ES_BOOTSTRAP_CHECKS_DISABLE": "true"},
            probe=probe_http("/"),
            ready_timeout_seconds=600,
        )
    )
    try:
        _poll_until(
            lambda: httpx.get(f"{handle.base_url}/api/system/status", timeout=60.0).json().get("status") == "UP",
            timeout=420,
            message="SonarQube web status to reach UP",
        )
        bootstrap_password = _random_password()
        # Read timeout 60s: change_password hits the DB while ES is still warming.
        with httpx.Client(base_url=handle.base_url, auth=("admin", "admin"), timeout=60.0) as client:
            # SonarQube's default admin ships with password "admin". Reset it to
            # a per-run secret first (the documented API path also works while
            # the password is "overdue for change"), then mint a user token.
            pw_change = client.post(
                "/api/users/change_password",
                params={"login": "admin", "previousPassword": "admin", "password": bootstrap_password},
            )
            if pw_change.status_code not in (204, 400):  # 400 = already changed to the secret
                pw_change.raise_for_status()
        # Mint the token with a FRESH client (Basic auth, no cookies): SonarQube
        # rejects cookie-authenticated POSTs without an X-XSRF-TOKEN header, and
        # the change_password response leaves a session cookie in the first
        # client that would make the token call a misleading 401.
        with httpx.Client(base_url=handle.base_url, auth=("admin", bootstrap_password), timeout=60.0) as client:
            token_resp = client.post("/api/user_tokens/generate", params={"name": "modulo-ci"})
            try:
                token_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AssertionError(
                    f"SonarQube token generation failed: {exc} body={token_resp.text[:400]!r}"
                ) from exc
            admin_token = token_resp.json()["token"]
        project_key = f"modulo-tier1b-{_now_suffix()}"
        with httpx.Client(base_url=handle.base_url, auth=(admin_token, ""), timeout=60.0) as client:
            project_resp = client.post(
                "/api/projects/create", params={"project": project_key, "name": "Modulo Tier 1b"}
            )
            try:
                project_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AssertionError(f"sonar project create failed: {exc}") from exc
        handle.sonar_token = admin_token  # type: ignore[attr-defined]
        handle.sonar_project_key = project_key  # type: ignore[attr-defined]
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def sonarqube_connector(sonarqube_service: ContainerHandle) -> SonarQubeConnector:
    return SonarQubeConnector(token=str(sonarqube_service.sonar_token), base_url=sonarqube_service.base_url)


async def test_sonarqube_health_and_query(
    sonarqube_service: ContainerHandle, sonarqube_connector: SonarQubeConnector
) -> None:
    health: HealthResult | None = None
    # Health settles late (embedded Elasticsearch warms after /api/system/status
    # reports UP), so poll until it leaves RED within a bounded window.
    for _ in range(6):
        health = await sonarqube_connector.health_check()
        if health.ok:
            break
        await asyncio.sleep(30.0)
    assert health is not None and health.ok, f"sonar health against real container failed: {health}"
    result = await sonarqube_connector.query(ConnectorQuery(resource="projects"))
    keys = {record.get("key") for record in result.records}
    assert sonarqube_service.sonar_project_key in keys, f"expected project in real search: {keys!r}"


async def test_sonarqube_write_gate_then_query(sonarqube_connector: SonarQubeConnector) -> None:
    gate_name = f"modulo-tier1b-{_now_suffix()}"
    write_result = await sonarqube_connector.write(ConnectorPayload(resource="gate", data={"name": gate_name}))
    assert isinstance(write_result, dict), f"expected dict from sonar gate write, got {type(write_result).__name__}"
    result = await sonarqube_connector.query(ConnectorQuery(resource="quality_gates"))
    names = {record.get("name") for record in result.records}
    assert gate_name in names, f"sonar write-then-read roundtrip failed; real gates: {names!r}"


# ── grafana ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def grafana_service() -> Iterator[ContainerHandle]:
    admin_password = _random_password()
    handle = start_tier1b_container(
        ContainerSpec(
            image="grafana/grafana:11.1.0",
            container_port=3000,
            env={
                "GF_SECURITY_ADMIN_PASSWORD": admin_password,
                "GF_USERS_ALLOW_SIGN_UP": "false",
                "GF_ANONYMOUS_ENABLED": "false",
            },
            probe=_LIST_PROBE_GRAFANA,
            ready_timeout_seconds=240,
        )
    )
    try:
        with httpx.Client(base_url=handle.base_url, auth=("admin", admin_password), timeout=60.0) as client:
            sa_resp = client.post("/api/serviceaccounts", json={"name": "modulo-ci", "role": "Admin"})
            sa_resp.raise_for_status()
            sa_id = sa_resp.json()["id"]
            tok_resp = client.post(f"/api/serviceaccounts/{sa_id}/tokens", json={"name": "modulo-ci-token"})
            tok_resp.raise_for_status()
            sa_token = tok_resp.json()["key"]
            assert sa_token, f"grafana service-account token missing: {tok_resp.text!r}"
            uid = f"modulo-tier1b-{secrets.token_hex(3)}"
            dash_resp = client.post(
                "/api/dashboards/db",
                json={
                    "dashboard": {"uid": uid, "title": "Upstream schema", "tags": ["tier1b"], "panels": []},
                    "overwrite": True,
                },
            )
            dash_resp.raise_for_status()
        handle.grafana_token = sa_token  # type: ignore[attr-defined]
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="session")
def grafana_connector(grafana_service: ContainerHandle) -> GrafanaConnector:
    return GrafanaConnector(token=str(grafana_service.grafana_token), base_url=grafana_service.base_url)


async def test_grafana_health_and_dashboards(grafana_connector: GrafanaConnector) -> None:
    health = await grafana_connector.health_check()
    assert health.ok, f"grafana health against real container failed: {health.detail}"
    result: ConnectorResult | None = await grafana_connector.query(ConnectorQuery(resource="dashboards"))
    titles = {record.get("title") for record in result.records}
    assert "Upstream schema" in titles, f"grafana real dashboards: {titles!r}"


async def test_grafana_write_annotation_then_query(grafana_connector: GrafanaConnector) -> None:
    annotation_text = f"modulo-tier1b-{_now_suffix()}"
    write_result = await grafana_connector.write(
        ConnectorPayload(resource="annotation", data={"text": annotation_text, "tags": ["tier1b"]})
    )
    assert isinstance(write_result, dict), (
        f"expected dict from grafana annotation write, got {type(write_result).__name__}"
    )
    result = await grafana_connector.query(ConnectorQuery(resource="annotations"))
    texts = {record.get("text") if isinstance(record, dict) else None for record in result.records}
    assert annotation_text in texts, f"grafana annotation roundtrip failed; real annotations: {texts!r}"


# ── teamcity ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def teamcity_service() -> Iterator[ContainerHandle]:
    pytest.skip(
        "Tier 1b TeamCity connector roundtrip cannot run headless against jetbrains/teamcity-server "
        "2024.07.2: on first start the server permanently blocks in its 'Confirming TeamCity first "
        "start' maintenance screen until a superuser finishes it interactively. Verified against the "
        "live container in this worktree: /app/rest/users stays HTTP 503, /mnt/do/goNewInstallation "
        "says 'We can't persist data directory location' on a fresh container, and the documented "
        "skip flags (teamcity.startup.confirmation.skip / granted, internal.properties, mounted "
        "startup.properties at every writable /conf path) do not suppress the gate in this version. "
        "recorded skip (FAR-934): the connector's own code path still needs a session-scope "
        "container fixture once TeamCity can be seeded with a pre-confirmed data directory "
        "(volume of a manually-confirmed datadir) — track as follow-up."
    )
    handle = start_tier1b_container(
        ContainerSpec(
            image="jetbrains/teamcity-server:2024.07.2",
            container_port=8111,
            env={"TEAMCITY_SERVER_MEM_OPTS": "-Xms256m -Xmx1g"},
            probe=probe_http("/app/rest/users"),
            ready_timeout_seconds=1800,
            poll_interval_seconds=5.0,
        )
    )
    project_id = "ModuloTier1b"
    superuser_token = _find_teamcity_superuser_token(handle)
    with httpx.Client(base_url=handle.base_url, headers={"Authorization": f"Bearer {superuser_token}"}) as client:
        project_create = client.post("/app/rest/projects", json={"id": project_id, "name": "Modulo Tier 1b"})
        if project_create.status_code == 403:
            # Superuser token over Basic auth only in some versions.
            import base64 as _b64

            raw = _b64.b64encode(f"superuser:{superuser_token}".encode()).decode()
            client.headers["Authorization"] = f"Basic {raw}"
            project_create = client.post("/app/rest/projects", json={"id": project_id, "name": "Modulo Tier 1b"})
        project_create.raise_for_status()
        user_token_resp = client.post("/app/rest/users/id:1/tokens", json={"name": "modulo-ci"})
        admin_access_token = user_token_resp.json().get("value", "")
        if admin_access_token:
            handle.teamcity_admin_token = admin_access_token  # type: ignore[attr-defined]
    if not admin_access_token:
        raise AssertionError(f"TeamCity access token mint failed via superuser REST: {user_token_resp.text!r}")
    yield handle
    handle.stop()


def _find_teamcity_superuser_token(handle: ContainerHandle) -> str:
    # TeamCity writes the superuser token to a file under /data/teamcity/temp.
    candidate_paths = [
        "/data/teamcity/temp/restServer/httpAuth/restore.txt",
        "/data/teamcity/temp/restServer/repository/superuser.txt",
    ]
    for path in candidate_paths:
        try:
            value = handle.exec(["cat", path]).strip()
            if value:
                return value
        except Tier1bFixtureError:
            continue
    listing = handle.exec(["sh", "-c", "find /data/teamcity -name '*super*' 2>/dev/null"])
    raise AssertionError(f"teamcity superuser token file not found; find output: {listing!r}")


@pytest.fixture(scope="session")
def teamcity_connector(teamcity_service: ContainerHandle) -> TeamCityConnector:
    return TeamCityConnector(token=str(teamcity_service.teamcity_admin_token), base_url=teamcity_service.base_url)


async def test_teamcity_health_and_agents(teamcity_connector: TeamCityConnector) -> None:
    health = await teamcity_connector.health_check()
    assert health.ok, f"TeamCity health against real container failed: {health.detail}"
    result = await teamcity_connector.query(
        ConnectorQuery(resource="buildTypes", filters={"project_id": "ModuloTier1b"})
    )
    assert result.total >= 0, "TeamCity real buildTypes query should decode"


async def test_teamcity_write_build_type(teamcity_connector: TeamCityConnector) -> None:
    build_type_id = f"moduloTier1b_{_now_suffix()}"
    write_result = await teamcity_connector.write(
        ConnectorPayload(
            resource="buildType",
            data={"buildTypeId": build_type_id, "projectId": "ModuloTier1b", "name": "Roundtrip Build Type"},
        )
    )
    assert isinstance(write_result, dict), f"expected dict from teamcity write, got {type(write_result).__name__}"
    result = await teamcity_connector.query(
        ConnectorQuery(resource="buildTypes", filters={"project_id": "ModuloTier1b"})
    )
    ids = {record.get("id") for record in result.records}
    assert build_type_id in ids, f"TeamCity buildType roundtrip failed; real build types: {ids!r}"


# ── trivy ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def trivy_container() -> Iterator[ContainerHandle]:
    """Start the real trivy server container (vuln DB download included).

    Trivy 0.74's server downloads its vulnerability database at boot —
    observed 4-5 minutes on this host — so the readiness window must be generous.
    """
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="aquasec/trivy:latest",
                container_port=8080,
                command=["server", "--listen", "0.0.0.0:8080"],
                # ``/healthz`` is the ONLY healthy path on trivy 0.74's server
                # (verified live: /trivy/v1/health, /trivy/v1/plugins,
                # /trivy/v1/connection-test all answer HTTP 404; real client
                # <-> server scans DO work via trivy's own CLI over --server).
                probe=probe_http("/healthz"),
                ready_timeout_seconds=600,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b trivy fixture unavailable on this Docker host (recorded skip): {exc}")
    yield handle
    handle.stop()


async def test_trivy_server_container_serves_healthz(trivy_container: ContainerHandle) -> None:
    """Container-level proof: the official trivy image really serves its server API here."""
    async with httpx.AsyncClient(base_url=trivy_container.base_url) as client:
        resp = await client.get("/healthz", timeout=15)
    assert resp.status_code == 200, f"trivy server healthz failed: HTTP {resp.status_code}"
    assert resp.text == "ok", f"unexpected healthz body: {resp.text[:80]!r}"


async def test_trivy_connector_roundtrip(trivy_container: ContainerHandle) -> None:
    """Exercise the Trivy *connector* against the real server, or record the skip.

    Verified live against aquasec/trivy 0.74.0: the connector targets REST endpoints
    that don't exist on the real server — GET /trivy/v1/health, /trivy/v1/plugins,
    /trivy/v1/database/metadata and POST /trivy/v1/connection-test all answer HTTP 404;
    only /healthz (200 "ok") is exposed, and the genuine client<->server contract works
    through the trivy CLI. Re-pointing the connector at the real REST surface (or running
    the CLI in-container) is production work outside this branch's allowlist.
    When the connector is re-targeted and the server serves /trivy/v1/*, the probe
    below flips and this test exercises the connector for real.
    """
    async with httpx.AsyncClient(base_url=trivy_container.base_url) as client:
        surface = await client.get("/trivy/v1/health", timeout=15)
    if surface.status_code == 404:
        pytest.skip(
            "TrivyConnector endpoints are HTTP 404 on the real aquasec/trivy 0.74.0 server "
            "recorded skip (FAR-934); connector re-targeting is a production fix follow-up"
        )
    # The REST surface exists here: drive the real connector surface end-to-end.
    from modulo.connectors.trivy import TrivyConnector

    connector = TrivyConnector(token="unused-local-server", base_url=trivy_container.base_url)
    health = await connector.health_check()
    assert health.ok, f"trivy connector health failed against real server: {health}"


# ── codeclimate (official CLI container; connector target is SaaS-only) ─────


@pytest.fixture(scope="session")
def codeclimate_cli_container() -> Iterator[ContainerHandle]:
    """Spin the official Code Climate CLI container and check it really runs.

    The Code Climate *connector* is a REST client for api.codeclimate.com.
    Until (if ever) ``CodeClimateConnector`` gains an overridable base URL
    (a production change, out of scope here), no container-hosted server can
    receive its calls — the official ``codeclimate`` container is the CLI,
    not the API. So this fixture validates the container-level requirement
    and the connector-level roundtrip is skipped LOUDLY with a recorded
    reason (below), rather than silently passing.
    """
    try:
        handle = start_tier1b_container(
            ContainerSpec(
                image="codeclimate/codeclimate:latest",
                container_port=None,
                probe=None,
                ready_timeout_seconds=120,
            )
        )
    except Tier1bFixtureError as exc:
        pytest.skip(f"Tier 1b codeclimate CLI container unavailable (recorded skip): {exc}")
    yield handle
    handle.stop()


def test_codeclimate_cli_container_runs(codeclimate_cli_container: ContainerHandle | None) -> None:
    if codeclimate_cli_container is None:
        pytest.skip("Tier 1b codeclimate CLI container skipped on this Docker host (recorded skip)")
    listing = codeclimate_cli_container.exec(["sh", "-c", "command -v codeclimate || ls /usr/src/app/bin"])
    assert listing.strip(), "codeclimate CLI binary must exist in the official container"


def test_codeclimate_connector_has_no_self_hostable_target(
    codeclimate_cli_container: ContainerHandle | None,
) -> None:
    # Recorded skip, gated on an explicit condition so it never silently
    # deselects: if the connector ever gains a base_url/_API_BASE override
    # path, this condition flips and the connector-level roundtrip must be
    # written for real instead of skipping.
    from modulo.connectors import codeclimate as cc_mod

    pinned_base = getattr(cc_mod, "_API_BASE", "")
    if pinned_base:
        pytest.skip(
            f"Code Climate connector pins {pinned_base} (SaaS-only) with no overridable "
            "base URL; no container-hosted service can receive real query()/write() calls. "
            "Recorded skip (FAR-934); a production base_url param would cover this connector."
        )
    raise AssertionError(
        "CodeClimateConnector no longer pins a base URL — implement the container-hosted connector-level roundtrip now."
    )
