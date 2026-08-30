"""Prove the outbound SSRF gate on every ``base_url``-bearing connector.

``CONNECTOR_NAMES`` is the coverage contract for the claim in
``docs/configuration-reference.md`` that *all* ``base_url``-bearing connectors are
gated, so every connector accepting a caller-supplied egress target belongs in
that list — including the ``gitlab_ci`` CI runner, whose ``base_url`` the
connector hub passes straight through from tenant config.

Each connector validates its egress target in ``_client()`` (and, where the
health check builds its own client, in ``health_check``). These tests assert the
gate FAILS CLOSED: a private or loopback ``base_url`` must never produce a usable
client, and a health check must report it as unhealthy instead of raising.

The "report, never raise" half matters as much as the block itself: a connector
whose ``health_check`` lets the guard's ``ValueError`` escape turns
``GET /connectors/{id}/health`` into a 502 instead of an actionable ``ok:false``
plus remediation detail.

Two properties make these tests meaningful rather than tautological:

* ``pytestmark = pytest.mark.real_ssrf_dns`` opts out of the autouse DNS shim in
  ``tests/conftest.py`` (which resolves every hostname to a public address for
  the rest of the suite). Without the opt-out these assertions would pass for the
  wrong reason.
* The blocked URLs are literal IPs, so no DNS resolution happens at all and the
  result is deterministic offline. The hostname-resolution path is covered
  separately by patching the resolver to return a private answer.

Remove any ``validate_outbound_url`` call from the connector under test and the
corresponding case here fails — that is the prove-the-fix contract.
"""

from typing import Any
from urllib.parse import urlparse

import pytest

import modulo.core.ssrf as ssrf
from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import ConnectorPayload, ConnectorQuery
from modulo.connectors.ci_runner.gitlab_ci import GitLabCIRunner
from modulo.connectors.confluence import ConfluenceConnector
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.onepassword import OnePasswordConnector
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.connectors.trivy import TrivyConnector
from modulo.connectors.youtrack import YouTrackConnector

pytestmark = pytest.mark.real_ssrf_dns

TOKEN = "test-token"  # nosec B105 - test fixture value, not a credential

# Literal blocked targets: loopback, RFC1918, and the cloud metadata address.
# Literals skip DNS entirely, so these cases are hermetic.
LOOPBACK = "http://127.0.0.1:8080"
PRIVATE = "http://10.1.2.3:8080"
METADATA = "http://169.254.169.254"


def _build(name: str, base_url: str) -> Any:
    """Construct the named connector pointed at ``base_url``."""
    builders = {
        "gitea": lambda: GiteaConnector(token=TOKEN, base_url=base_url),
        "gitlab": lambda: GitLabConnector(token=TOKEN, base_url=base_url),
        "jira": lambda: JiraConnector(base_url=base_url, creds={"token": TOKEN}),
        "youtrack": lambda: YouTrackConnector(token=TOKEN, base_url=base_url),
        "sonarqube": lambda: SonarQubeConnector(token=TOKEN, base_url=base_url),
        "teamcity": lambda: TeamCityConnector(token=TOKEN, base_url=base_url),
        "trivy": lambda: TrivyConnector(token=TOKEN, base_url=base_url),
        "onepassword": lambda: OnePasswordConnector(token=TOKEN, base_url=base_url),
        "sentry": lambda: SentryConnector(token=TOKEN, organization="org", base_url=base_url),
        "azure_key_vault": lambda: AzureKeyVaultConnector(token=TOKEN, vault_url=base_url),
        "jenkins": lambda: JenkinsConnector(username="user", token=TOKEN, base_url=base_url),
        "n8n": lambda: N8NConnector(token=TOKEN, base_url=base_url),
        "grafana": lambda: GrafanaConnector(token=TOKEN, base_url=base_url),
        # Confluence derives base_url as https://<instance>, so the blocked host
        # goes in as the instance rather than a full URL.
        "confluence": lambda: ConfluenceConnector(
            instance=urlparse(base_url).netloc,
            creds={"token": TOKEN},
        ),
        # The gitlab_ci CI runner is on the same trust boundary: connector_hub
        # passes config['base_url'] straight through from tenant config.
        "gitlab_ci": lambda: GitLabCIRunner(token=TOKEN, base_url=base_url),
    }
    return builders[name]()


CONNECTOR_NAMES = [
    "gitea",
    "gitlab",
    "jira",
    "youtrack",
    "sonarqube",
    "teamcity",
    "trivy",
    "onepassword",
    "sentry",
    "azure_key_vault",
    # Gated in PR #2116. These had NO prove-the-fix coverage, which is how a
    # raising-health-check regression reached green CI; they are listed here
    # rather than in a parallel structure so every case above applies to them.
    "jenkins",
    "n8n",
    "grafana",
    "confluence",
    "gitlab_ci",
]


@pytest.mark.parametrize("name", CONNECTOR_NAMES)
@pytest.mark.parametrize("blocked_url", [LOOPBACK, PRIVATE, METADATA])
def test_client_refuses_blocked_base_url(name: str, blocked_url: str) -> None:
    """``_client()`` must raise rather than hand back a client aimed at an internal host."""
    connector = _build(name, blocked_url)
    with pytest.raises(ValueError, match="private/internal"):
        connector._client()


@pytest.mark.parametrize("name", CONNECTOR_NAMES)
async def test_health_check_reports_blocked_base_url(name: str) -> None:
    """A blocked base_url is an unhealthy result, never an exception."""
    connector = _build(name, LOOPBACK)
    result = await connector.health_check()
    assert result.ok is False
    assert "127.0.0.1" in result.detail


LOCALHOST_DEFAULT_BUILDERS = {
    "trivy": lambda: TrivyConnector(token=TOKEN),
    "sonarqube": lambda: SonarQubeConnector(token=TOKEN),
    "teamcity": lambda: TeamCityConnector(token=TOKEN),
    "onepassword": lambda: OnePasswordConnector(token=TOKEN),
    "jenkins": lambda: JenkinsConnector(username="user", token=TOKEN),
    "n8n": lambda: N8NConnector(token=TOKEN),
    "grafana": lambda: GrafanaConnector(token=TOKEN),
}

LOCALHOST_DEFAULT_NAMES = list(LOCALHOST_DEFAULT_BUILDERS)


@pytest.mark.parametrize("name", LOCALHOST_DEFAULT_NAMES)
async def test_localhost_default_base_url_is_blocked_by_default(name: str, monkeypatch) -> None:
    """The localhost-default connectors fail closed with actionable guidance.

    Trivy/SonarQube/TeamCity/1Password/Jenkins/n8n/Grafana ship a loopback default
    ``base_url``. With loopback blocked by default they must not connect, and the
    error must name the variable AND the both-families requirement, because
    ``localhost`` resolves to ``127.0.0.1`` and ``::1`` on a dual-stack host.

    The health check must REPORT the rejection, never raise it. A connector whose
    DEFAULT target is blocked is the most likely way a tenant meets this guard —
    exactly the Jenkins case — so ``GET /connectors/{id}/health`` must answer
    ``ok:false`` with the remediation text instead of returning a 502.
    """
    monkeypatch.delenv("SSRF_ALLOW_PRIVATE_RANGES", raising=False)
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["127.0.0.1", "::1"])

    connector = LOCALHOST_DEFAULT_BUILDERS[name]()

    with pytest.raises(ValueError, match="private/internal") as exc_info:
        connector._client()
    message = str(exc_info.value)
    assert "SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128" in message
    assert "BOTH" in message

    result = await connector.health_check()
    assert result.ok is False


@pytest.mark.parametrize("name", LOCALHOST_DEFAULT_NAMES)
def test_localhost_default_works_once_both_loopback_families_allowed(name: str, monkeypatch) -> None:
    """The documented remediation must actually work on a dual-stack host.

    ``localhost`` resolving to both families means an IPv4-only allowlist still
    fails closed; the documented ``127.0.0.0/8,::1/128`` pair is what unblocks it.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["127.0.0.1", "::1"])
    connector = LOCALHOST_DEFAULT_BUILDERS[name]()

    # IPv4-only allowlist: ::1 is still blocked, so the guard still fails closed.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8")
    with pytest.raises(ValueError, match="private/internal"):
        connector._client()

    # Both families allowlisted: the target becomes reachable.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,::1/128")
    client = connector._client()
    assert client is not None


async def test_hostname_resolving_to_private_address_is_blocked(monkeypatch) -> None:
    """The DNS path is gated too, not just literal IPs."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["192.168.7.7"])
    connector = TrivyConnector(token=TOKEN, base_url="http://scanner.internal.example:8080")

    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        connector._client()

    result = await connector.health_check()
    assert result.ok is False
    assert "192.168.7.7" in result.detail


async def test_query_and_write_refuse_blocked_base_url() -> None:
    """``query``/``write`` must not reach an internal host either.

    They surface the guard's ValueError (with its remediation text) rather than
    silently proceeding — the same failure mode as any other invalid connector
    configuration.
    """
    connector = TrivyConnector(token=TOKEN, base_url=METADATA)

    with pytest.raises(ValueError, match="private/internal"):
        await connector.query(ConnectorQuery(resource="reports"))

    with pytest.raises(ValueError, match="private/internal"):
        await connector.write(ConnectorPayload(resource="scan", data={"image": "alpine:3"}))


def test_public_base_url_still_builds_a_client(monkeypatch) -> None:
    """Control case: the gate must not block a legitimate public target."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["93.184.216.34"])
    connector = TrivyConnector(token=TOKEN, base_url="https://scanner.example.com")
    assert connector._client() is not None


@pytest.mark.parametrize(
    "connector_factory",
    [
        lambda: AzurePipelinesConnector(token=TOKEN, organization="org", project="p"),
        lambda: AzureReposConnector(token=TOKEN, organization="org"),
    ],
)
async def test_azure_connectors_refuse_blocked_egress(connector_factory, monkeypatch) -> None:
    """Azure DevOps connectors build their client against ``dev.azure.com``.

    They are gated in code (validate_outbound_url in ``_client``), but the host is
    a constant, so the literal-IP cases above cannot exercise it. Force the
    resolver to answer with a private address and confirm the gate fails closed:
    ``_client()`` raises and ``health_check`` reports unhealthy.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["10.1.2.3"])

    connector = connector_factory()
    with pytest.raises(ValueError, match="private/internal"):
        connector._client()

    result = await connector.health_check()
    assert result.ok is False
    assert "10.1.2.3" in result.detail
