"""FAR-512: outbound connectors + model backends migrated to the pinned-IP transport.

Before FAR-512, a ``base_url``-bearing connector validated its egress target
(:func:`modulo.core.ssrf.validate_outbound_url`) and then built its own
``httpx.AsyncClient`` — a validate-then-connect pattern. A hostname under
attacker DNS control could answer public at validation time and internal
(169.254.169.254) at connect time (DNS rebinding), escaping the gate.

These tests prove the migration actually pins: a connector's ``_client()`` now
builds a :class:`modulo.core.ssrf.PinnedAsyncHTTPTransport` whose pin map is
``{hostname: validated_ip}``, so the connection goes to the VALIDATED address
and never re-resolves the host. The resolver is stubbed to FLIP from the
validated public address to the cloud-metadata address between validation and
connect — the transport must still target the validated IP and refuse the
flipped host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

import modulo.core.ssrf as ssrf
from modulo.connectors.asana import AsanaConnector
from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import ConnectorBase
from modulo.connectors.bitbucket import BitbucketConnector
from modulo.connectors.buildkite import BuildkiteConnector
from modulo.connectors.ci_runner.github_actions import GitHubActionsCIRunner
from modulo.connectors.ci_runner.gitlab_ci import GitLabCIRunner
from modulo.connectors.circleci import CircleCIConnector
from modulo.connectors.codeclimate import CodeClimateConnector
from modulo.connectors.confluence import ConfluenceConnector
from modulo.connectors.datadog import DatadogConnector
from modulo.connectors.discord import DiscordConnector
from modulo.connectors.dropbox_paper import DropboxPaperConnector
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.github import GitHubConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.linear import LinearConnector
from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector
from modulo.connectors.monday import MondayConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.notion import NotionConnector
from modulo.connectors.npm import NpmConnector
from modulo.connectors.onepassword import OnePasswordConnector
from modulo.connectors.opsgenie import OpsgenieConnector
from modulo.connectors.pagerduty import PagerDutyConnector
from modulo.connectors.pypi import PyPIConnector
from modulo.connectors.rest import RestConnector
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.sharepoint import SharePointConnector
from modulo.connectors.shortcut import ShortcutConnector
from modulo.connectors.slack import SlackConnector
from modulo.connectors.snyk import SnykConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.connectors.ticket_tracker.github import GitHubTicketTracker
from modulo.connectors.ticket_tracker.trello import TrelloTicketTracker
from modulo.connectors.trello import TrelloConnector
from modulo.connectors.trivy import TrivyConnector
from modulo.connectors.youtrack import YouTrackConnector

# Resolver flip: first validation resolves PUBLIC (accepted), any later lookup
# answers with the cloud-metadata address (what an attacker's rebinding DNS
# would serve at connect time).
_VALIDATED_PUBLIC = "93.184.216.34"
_REBOUND_METADATA = "169.254.169.254"
TOKEN = "test-token"  # nosec B105 - test fixture value, not a credential


def _flip_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ssrf._resolve_all_sync`` to flip public -> metadata after the first call."""

    def fake(host: str) -> list[str]:
        if host not in _flip_resolver._seen:  # type: ignore[attr-defined]
            _flip_resolver._seen.append(host)  # type: ignore[attr-defined]
            return [_VALIDATED_PUBLIC]
        return [_REBOUND_METADATA]

    _flip_resolver._seen = []  # type: ignore[attr-defined]
    monkeypatch.setattr(ssrf, "_resolve_all_sync", fake)


def _pinned_hosts(client: object) -> dict[str, str]:
    """Return the pin map of the client's pinned transport."""
    transport = client._transport
    backend = transport._pool._network_backend
    return backend._pinned_hosts


def _aclose(client: object) -> None:
    asyncio.run(client.aclose())


def test_gitlab_client_pins_validated_ip_despite_resolver_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``base_url``-bearing connector's ``_client()`` pins the validated IP.

    The resolver flips to the metadata address after the validation lookup. The
    pinned transport must have captured the VALIDATED address in its pin map —
    the transport never re-resolves, so the rebound metadata answer is simply
    irrelevant to the connection.
    """
    _flip_resolver(monkeypatch)
    connector = GitLabConnector(token="test-token", base_url="https://gitlab.example.com/api/v4")

    client = connector._client()
    try:
        assert _pinned_hosts(client) == {"gitlab.example.com": (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


def test_gitlab_client_pins_per_validation_and_fails_closed_on_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each fresh ``_client()`` re-validates + pins; a rebind fails closed.

    ``GitLabConnector._client()`` builds a fresh client per call (not cached).
    The FIRST validation sees a public address and pins it onto the transport
    (so that one client never re-resolves). A SECOND ``_client()`` — now under
    the flipped resolver — must re-validate, see the private address, and fail
    closed rather than connect. This is the per-client pin that closes rebind.
    """
    _flip_resolver(monkeypatch)
    connector = GitLabConnector(token="test-token", base_url="https://gitlab.example.com/api/v4")

    first = connector._client()
    try:
        assert _pinned_hosts(first) == {"gitlab.example.com": (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(first)

    # A second client construction re-validates; the resolver now answers the
    # rebound metadata (private) address, so the gate fails closed.
    with pytest.raises(ValueError, match="private/internal"):
        connector._client()


def test_rest_client_pins_base_url_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REST connector pins its tenant-supplied ``base_url`` host.

    REST is the highest-risk surface (tenant base_url + templated paths), so its
    production ``_client()`` (no injected ``transport`` seam) must build a pinned
    transport for the ``base_url`` host, capturing the validated address.
    """
    _flip_resolver(monkeypatch)
    connector = RestConnector(
        {"base_url": "https://rest-target.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )

    client = connector._client()
    try:
        assert _pinned_hosts(client) == {"rest-target.example.com": (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


def test_rest_client_refuses_unpinned_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host outside the pin map is refused fail-closed.

    A redirected ``follow_redirects`` path or a templated URL that points at a
    different host is NOT silently re-validated at connect time — the pinned
    backend raises ``UnpinnedHostError``. This is what previously let a rebind
    escape: connect re-resolved the host. Now only validated hosts connect.
    """
    _flip_resolver(monkeypatch)
    connector = RestConnector(
        {"base_url": "https://rest-target.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )

    client = connector._client()
    try:
        backend = client._transport._pool._network_backend
        with pytest.raises(ssrf.UnpinnedHostError):
            asyncio.run(backend.connect_tcp("rebound-internal.example", 443))
    finally:
        _aclose(client)


def test_sync_pinned_helpers_wire_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring guard for the sync builders used by synchronous ``_client()``.

    Exercises ``pinned_async_transport_sync`` directly so the connector-level
    tests are unambiguous about where the pin happens. A blocked target still
    fails closed through the sync path.
    """
    _flip_resolver(monkeypatch)
    transport = ssrf.pinned_async_transport_sync("https://pinned-target.example.com/")
    try:
        backend = transport._pool._network_backend
        assert backend._pinned_hosts == {"pinned-target.example.com": (_VALIDATED_PUBLIC,)}
    finally:
        asyncio.run(transport.aclose())


def test_sync_pinned_client_still_blocks_internal_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector whose validator now sees a private address fails closed.

    The pin builder validates synchronously; a target that resolves private must
    not produce a client — the same fail-closed the old ``validate_outbound_url``
    path provided.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["10.0.0.5"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.pinned_async_client_sync("https://blocked.example.com/")


# --- FAR-526B: widen pinning to the last raw-httpx egress sites match -------------


def test_ticket_tracker_github_pins_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """(f) GitHubTicketTracker now builds its client through the pinned transport."""
    _flip_resolver(monkeypatch)
    tracker = GitHubTicketTracker(
        config={"repo": "owner/repo", "base_url": "https://api.github.com"},
        creds={"token": "ghp_fake"},
    )
    client = tracker._client()
    try:
        assert _pinned_hosts(client) == {"api.github.com": (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


def test_ticket_tracker_trello_pins_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """(f) TrelloTicketTracker builds its client through the pinned transport
    (its key/token stay in the query string, redaction preserved)."""
    _flip_resolver(monkeypatch)
    tracker = TrelloTicketTracker(config={"board_id": "board123"}, creds={"api_key": "k", "token": "t"})
    client = tracker._client()
    try:
        assert _pinned_hosts(client) == {"api.trello.com": (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


def test_ci_runner_github_actions_pins_api_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """(f) GitHubActionsCIRunner pins the hardcoded api.github.com address."""
    _flip_resolver(monkeypatch)
    runner = GitHubActionsCIRunner(token="ghp_test")
    client = runner._client()
    try:
        assert _pinned_hosts(client) == {"api.github.com": (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


# --- FAR-526C: pin-map COVERAGE CONTRACT over every egress construction path ------
#
# ``_PIN_ROSTER`` is the documented-COMPLETE set of outbound connectors whose
# ``_client()`` MUST build a client pinned to the validated address. A connector
# added to the egress surface that is not in this list is a visible review
# failure (the parametrised test asserts every entry pins, so the roster is
# mandatory, not optional — leaving a connector out turns the contract red).
#
# The roster spans all three construction paths:
#   * tenant ``base_url`` built in ``_client()`` (gitea, gitlab, jira, youtrack,
#     sonarqube, teamcity, trivy, onepassword, sentry, azure_key_vault, jenkins,
#     n8n, grafana, confluence, gitlab_ci);
#   * a fixed vendor host pinned (azure_repos -> dev.azure.com,
#     azure_pipelines -> dev.azure.com, ci_runner.github_actions -> api.github.com,
#     ticket_tracker.github -> api.github.com, ticket_tracker.trello ->
#     api.trello.com);
#   * an inline ``async with pinned_async_client_sync(...)`` hop (the
#     ci_runner redirect-follow, and pypi's XML-RPC search) — the roster's
#     ``# ok`` counterpart is exercised by the egress-gate suite.
#
# Every connector is built against the SAME flip resolver (PUBLIC on the
# validation lookup, rebound-metadata on any later lookup), so the assertion
# proves the pin captured the VALIDATED address and never the rebound one.
_PIN_ROSTER: dict[str, tuple[Callable[[], ConnectorBase], str]] = {
    # (connector factory -> connector, expected pinned host)
    "gitea": (
        lambda: GiteaConnector(token=TOKEN, base_url="https://gitea-egress.example.com"),
        "gitea-egress.example.com",
    ),
    "gitlab": (
        lambda: GitLabConnector(token=TOKEN, base_url="https://gitlab-egress.example.com/api/v4"),
        "gitlab-egress.example.com",
    ),
    "jira": (
        lambda: JiraConnector(base_url="https://jira-egress.example.com", creds={"token": TOKEN}),
        "jira-egress.example.com",
    ),
    "youtrack": (
        lambda: YouTrackConnector(token=TOKEN, base_url="https://youtrack-egress.example.com"),
        "youtrack-egress.example.com",
    ),
    "sonarqube": (
        lambda: SonarQubeConnector(token=TOKEN, base_url="https://sonarqube-egress.example.com"),
        "sonarqube-egress.example.com",
    ),
    "teamcity": (
        lambda: TeamCityConnector(token=TOKEN, base_url="https://teamcity-egress.example.com"),
        "teamcity-egress.example.com",
    ),
    "trivy": (
        lambda: TrivyConnector(token=TOKEN, base_url="https://trivy-egress.example.com"),
        "trivy-egress.example.com",
    ),
    "onepassword": (
        lambda: OnePasswordConnector(token=TOKEN, base_url="https://onepassword-egress.example.com"),
        "onepassword-egress.example.com",
    ),
    "sentry": (
        lambda: SentryConnector(token=TOKEN, organization="org", base_url="https://sentry-egress.example.com"),
        "sentry-egress.example.com",
    ),
    "azure_key_vault": (
        lambda: AzureKeyVaultConnector(token=TOKEN, vault_url="https://akv-egress.example.com"),
        "akv-egress.example.com",
    ),
    "jenkins": (
        lambda: JenkinsConnector(username="user", token=TOKEN, base_url="https://jenkins-egress.example.com"),
        "jenkins-egress.example.com",
    ),
    "n8n": (
        lambda: N8NConnector(token=TOKEN, base_url="https://n8n-egress.example.com"),
        "n8n-egress.example.com",
    ),
    "grafana": (
        lambda: GrafanaConnector(token=TOKEN, base_url="https://grafana-egress.example.com"),
        "grafana-egress.example.com",
    ),
    "confluence": (
        lambda: ConfluenceConnector(instance="confluence-egress.example.com", creds={"token": TOKEN}),
        "confluence-egress.example.com",
    ),
    "gitlab_ci": (
        lambda: GitLabCIRunner(token=TOKEN, base_url="https://gitlabci-egress.example.com"),
        "gitlabci-egress.example.com",
    ),
    "azure_repos": (
        lambda: AzureReposConnector(token=TOKEN, organization="org"),
        "dev.azure.com",
    ),
    "azure_pipelines": (
        lambda: AzurePipelinesConnector(token=TOKEN, organization="org", project="p"),
        "dev.azure.com",
    ),
    "ticket_tracker.github": (
        lambda: GitHubTicketTracker(
            config={"repo": "owner/repo", "base_url": "https://api.github.com"},
            creds={"token": "ghp_fake"},
        ),
        "api.github.com",
    ),
    "ticket_tracker.trello": (
        lambda: TrelloTicketTracker(config={"board_id": "board123"}, creds={"api_key": "k", "token": "t"}),
        "api.trello.com",
    ),
    "ci_runner.github_actions": (
        lambda: GitHubActionsCIRunner(token=TOKEN),
        "api.github.com",
    ),
    # --- fixed-vendor-host connectors (FAR-526C) -------------------------------
    # These pin a hardcoded vendor host; the resolver stub still proves the pin
    # captured the validated address (never a rebound answer).
    "asana": (
        lambda: AsanaConnector(personal_access_token=TOKEN),
        "app.asana.com",
    ),
    "bitbucket": (
        lambda: BitbucketConnector(token=TOKEN),
        "api.bitbucket.org",
    ),
    "buildkite": (
        lambda: BuildkiteConnector(token=TOKEN),
        "api.buildkite.com",
    ),
    "circleci": (
        lambda: CircleCIConnector(token=TOKEN),
        "circleci.com",
    ),
    "codeclimate": (
        lambda: CodeClimateConnector(token=TOKEN),
        "api.codeclimate.com",
    ),
    "datadog": (
        lambda: DatadogConnector(api_key=TOKEN, app_key=TOKEN, site="us"),
        "api.datadoghq.com",
    ),
    "discord": (
        lambda: DiscordConnector(token=TOKEN),
        "discord.com",
    ),
    "dropbox_paper": (
        lambda: DropboxPaperConnector(token=TOKEN),
        "api.dropboxapi.com",
    ),
    "github": (
        lambda: GitHubConnector(token=TOKEN),
        "api.github.com",
    ),
    "linear": (
        lambda: LinearConnector(token=TOKEN),
        "api.linear.app",
    ),
    "microsoft_teams": (
        lambda: MicrosoftTeamsConnector(token=TOKEN),
        "graph.microsoft.com",
    ),
    "monday": (
        lambda: MondayConnector(api_key=TOKEN),
        "api.monday.com",
    ),
    "notion": (
        lambda: NotionConnector(token=TOKEN),
        "api.notion.com",
    ),
    "npm": (
        lambda: NpmConnector(token=TOKEN),
        "registry.npmjs.org",
    ),
    "opsgenie": (
        lambda: OpsgenieConnector(api_key=TOKEN),
        "api.opsgenie.com",
    ),
    "pagerduty": (
        lambda: PagerDutyConnector(token=TOKEN),
        "api.pagerduty.com",
    ),
    "pypi": (
        lambda: PyPIConnector(token=TOKEN),
        "pypi.org",
    ),
    "sharepoint": (
        lambda: SharePointConnector(token=TOKEN),
        "graph.microsoft.com",
    ),
    "shortcut": (
        lambda: ShortcutConnector(token=TOKEN),
        "api.app.shortcut.com",
    ),
    "slack": (
        lambda: SlackConnector(bot_token=TOKEN),
        "slack.com",
    ),
    "snyk": (
        lambda: SnykConnector(token=TOKEN),
        "api.snyk.io",
    ),
    "trello": (
        lambda: TrelloConnector(api_key=TOKEN, token=TOKEN),
        "api.trello.com",
    ),
}


@pytest.mark.parametrize("name", list(_PIN_ROSTER))
def test_pin_roster_covers_every_egress_connector(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every connector in the egress coverage contract pins its validated host.

    This is the completeness gate for the FAR-526 centralization: a connector
    that silently drops back to a raw ``httpx.AsyncClient`` (or pins the wrong
    host) fails here, so the roster cannot drift without a visible review
    failure.
    """
    factory, expected_host = _PIN_ROSTER[name]
    _flip_resolver(monkeypatch)
    connector = factory()
    client = connector._client()  # type: ignore[attr-defined]
    try:
        assert _pinned_hosts(client) == {expected_host: (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


def test_pin_roster_accounts_for_rest_and_sync_helpers() -> None:
    """The coverage contract must also account for REST and the sync helpers.

    REST pins via ``pinned_async_transport_sync`` + an injected transport (its
    own parametrised tests above), and the sync builders (``_sync`` variants)
    are covered by ``test_sync_pinned_helpers_wire_correctly`` — so they are
    deliberately NOT in ``_PIN_ROSTER`` (they are not ``_client()``-based
    connectors). This test documents that exclusion so a reviewer can see the
    roster is complete-by-design, not missing them.
    """
    excluded = {"rest"}
    assert not excluded & set(_PIN_ROSTER), "rest belongs in the sync/transport tests, not the _client roster"
