"""FAR-520: remaining ``base_url`` connectors migrated to the pinned-IP transport.

This file covers the FAR-520 leftovers: trivy, jenkins, grafana, teamcity,
youtrack, gitea, azure_repos, confluence, sentry, onepassword, azure_key_vault,
azure_pipelines and the ``ci_runner`` GitLab CI runner.

Note: the rest/github/gitlab/jira/slack/linear/n8n/sonarqube connectors and the
model backends are NOT covered by this file. Those connectors' pinning status is
tracked separately (see FAR-520) and must not be assumed migrated here — this
test suite only proves the pin for the connectors listed above.

Before FAR-520 each validated its egress target
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
from typing import Self

import httpx
import pytest

import modulo.core.ssrf as ssrf
from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.ci_runner.gitlab_ci import GitLabCIRunner
from modulo.connectors.confluence import ConfluenceConnector
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.onepassword import OnePasswordConnector
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.connectors.trivy import TrivyConnector
from modulo.connectors.youtrack import YouTrackConnector

# Resolver flip: first validation resolves PUBLIC (accepted), any later lookup
# answers with the cloud-metadata address (what an attacker's rebinding DNS
# would serve at connect time).
_VALIDATED_PUBLIC = "93.184.216.34"
_REBOUND_METADATA = "169.254.169.254"

TOKEN = "test-token"  # nosec B105 - test fixture value, not a credential

pytestmark = pytest.mark.real_ssrf_dns


def _flip_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ssrf._resolve_all_sync`` to flip public -> metadata after the first call.

    Every connector under test builds its client synchronously, so the sync
    resolver is the one the pin path consults.
    """

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


# Every migrated connector: the (validated_host, connector) pair. The validated
# host is the original hostname used for validation/SNI; for derived-base_url
# connectors (azure_repos/azure_pipelines) it is a constant.
_MIGRATED = {
    "trivy": ("scanner.example.com", TrivyConnector(token=TOKEN, base_url="https://scanner.example.com")),
    "jenkins": (
        "jenkins.example.com",
        JenkinsConnector(username="user", token=TOKEN, base_url="https://jenkins.example.com"),
    ),
    "grafana": ("grafana.example.com", GrafanaConnector(token=TOKEN, base_url="https://grafana.example.com")),
    "teamcity": ("teamcity.example.com", TeamCityConnector(token=TOKEN, base_url="https://teamcity.example.com")),
    "youtrack": ("youtrack.example.com", YouTrackConnector(token=TOKEN, base_url="https://youtrack.example.com")),
    "gitea": ("gitea.example.com", GiteaConnector(token=TOKEN, base_url="https://gitea.example.com")),
    "confluence": (
        "confluence.example.com",
        ConfluenceConnector(instance="confluence.example.com", creds={"token": TOKEN}),
    ),
    "sentry": (
        "sentry.example.com",
        SentryConnector(token=TOKEN, organization="org", base_url="https://sentry.example.com"),
    ),
    "onepassword": ("op.example.com", OnePasswordConnector(token=TOKEN, base_url="https://op.example.com")),
    "azure_key_vault": ("kv.example.com", AzureKeyVaultConnector(token=TOKEN, vault_url="https://kv.example.com")),
    "azure_repos": ("dev.azure.com", AzureReposConnector(token=TOKEN, organization="org")),
    "azure_pipelines": ("dev.azure.com", AzurePipelinesConnector(token=TOKEN, organization="org", project="p")),
    "gitlab_ci": ("gitlab.example.com", GitLabCIRunner(token=TOKEN, base_url="https://gitlab.example.com")),
}


@pytest.mark.parametrize("name", sorted(_MIGRATED))
def test_connector_pins_validated_ip_despite_resolver_flip(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migrated connector's ``_client()`` pins the validated IP.

    The resolver flips to the metadata address after the validation lookup. The
    pinned transport must have captured the VALIDATED address in its pin map —
    the transport never re-resolves, so the rebound metadata answer is simply
    irrelevant to the connection.
    """
    _flip_resolver(monkeypatch)
    host, connector = _MIGRATED[name]

    client = connector._client()
    try:
        assert _pinned_hosts(client) == {host: (_VALIDATED_PUBLIC,)}
    finally:
        _aclose(client)


def test_connector_refuses_unpinned_rebound_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host outside the pin map is refused fail-closed.

    A redirected ``follow_redirects`` path or a URL that points at a different
    host is NOT silently re-validated at connect time — the pinned backend
    raises ``UnpinnedHostError``. This is the property that closes a rebind: the
    connection only ever targets the validated address.
    """
    _flip_resolver(monkeypatch)
    connector = TrivyConnector(token=TOKEN, base_url="https://scanner.example.com")
    client = connector._client()
    try:
        backend = client._transport._pool._network_backend
        with pytest.raises(ssrf.UnpinnedHostError):
            asyncio.run(backend.connect_tcp("rebound-internal.example", 443))
    finally:
        _aclose(client)


def test_migrated_connector_still_fails_closed_on_blocked_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The migrated pin path preserves the SSRF fail-closed gate.

    A target that resolves private must not produce a client — the sync pin
    builder resolves + validates synchronously and raises, matching the old
    ``validate_outbound_url`` behaviour the egress-gate suite asserts.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["10.0.0.5"])
    connector = GiteaConnector(token=TOKEN, base_url="https://blocked.example.com")
    with pytest.raises(ValueError, match="private/internal"):
        connector._client()


def test_sync_pinned_client_still_blocks_internal_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring guard for the sync builders used by the migrated ``_client()``s."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["10.0.0.5"])
    with pytest.raises(ValueError, match="private/internal"):
        ssrf.pinned_async_client_sync("https://blocked.example.com/")


def test_azure_repos_health_check_egress_is_pinned_to_profile_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-520: azure_repos ``health_check`` pins its ACTUAL connect host.

    ``health_check`` connects to the Microsoft profile host
    (``app.vssps.visualstudio.com``), NOT the repo ``base_url``
    (``dev.azure.com/<org>``). It must build its client through
    :func:`modulo.core.ssrf.pinned_async_client_sync` targeting that host — not
    a plain unpinned ``AsyncClient`` and not a stale
    ``validate_outbound_url(base_url)`` that guarded a host this connection
    never uses.
    """
    captured: dict[str, str] = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"displayName": "tester"}

    class _ProfileClient:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def get(self, url: str, **_kw: object) -> _Resp:
            captured["url"] = url
            return _Resp()

    def fake_pinned(url: str, **_kw: object) -> _ProfileClient:
        captured["pinned_url"] = url
        return _ProfileClient()

    monkeypatch.setattr("modulo.connectors.azure_repos.pinned_async_client_sync", fake_pinned)

    connector = AzureReposConnector(token=TOKEN, organization="org")
    result = asyncio.run(connector.health_check())

    assert result.ok is True
    assert result.detail == "tester"
    assert captured["pinned_url"] == "https://app.vssps.visualstudio.com"
    assert captured["url"] == "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"


def test_pinned_transport_defaults_to_bounded_httpx_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAJOR regression (FAR-520 review): a pinned transport built WITHOUT a
    caller-supplied ``limits`` must keep httpx's DEFAULT connection cap (100/20).

    ``limits or httpx.Limits()`` previously evaluated to an empty ``httpx.Limits()``
    which httpcore substitutes with ``sys.maxsize`` — an unbounded pool — silently
    lifting the per-client connection ceiling for every migrated connector (and the
    OIDC discovery/JWKS/token fetches). The pin must not also remove the cap.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: [_VALIDATED_PUBLIC])
    transport = ssrf.pinned_async_transport_sync("https://scanner.example.com/")
    assert transport._pool._max_connections == 100
    assert transport._pool._max_keepalive_connections == 20


def test_pinned_transport_respects_caller_supplied_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-supplied ``limits`` flows into the httpcore pool intact."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: [_VALIDATED_PUBLIC])
    custom = httpx.Limits(max_connections=5, max_keepalive_connections=2)
    transport = ssrf.pinned_async_transport_sync("https://scanner.example.com/", limits=custom)
    assert transport._pool._max_connections == 5
    assert transport._pool._max_keepalive_connections == 2


def test_pinned_client_sync_rejects_caller_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """MINOR regression (FAR-520 review): a ``transport`` in client_kwargs must be
    rejected so it cannot silently bypass the pinned transport.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: [_VALIDATED_PUBLIC])
    with pytest.raises(ValueError, match="transport"):
        ssrf.pinned_async_client_sync(
            "https://scanner.example.com/", transport=httpx.MockTransport(lambda _req: httpx.Response(200))
        )
