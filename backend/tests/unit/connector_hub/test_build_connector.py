"""Unit tests for ConnectorHub._build_connector factory coverage.

Every ``match`` branch in ``_build_connector`` is exercised by the
parametrised :func:`test_build_known_connector` case list.  Adding a new
connector type means adding one row to ``BUILD_CASES`` in ``_fixtures.py``
(FAR-1773: shared, non-test fixture module) — the factory is then
regression-tested automatically.
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.ci_runner import GitHubActionsCIRunner
from modulo.connectors.github import GitHubConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.jira import JiraConnector
from modulo.core.connector_hub import _build_connector
from tests.unit.connector_hub._fixtures import BUILD_CASES

# ShellConnector is deprecated (ADR 044) and warns on construction.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.mark.parametrize(
    ("type_id", "config", "creds", "expected_class", "expected_type"),
    BUILD_CASES,
    ids=[case[0] for case in BUILD_CASES],
)
def test_build_known_connector(
    type_id: str,
    config: dict[str, Any],
    creds: dict[str, Any],
    expected_class: type[ConnectorBase],
    expected_type: ConnectorType,
) -> None:
    connector = _build_connector(type_id, config, creds)
    assert type(connector) is expected_class, f"Expected {expected_class.__name__}, got {type(connector).__name__}"
    assert connector.connector_type == expected_type


# ── Config-dependent base URL resolution ───────────────────────────────────


@pytest.mark.parametrize(
    ("config", "expected_base_url"),
    [
        ({"instance": "acme.atlassian.net"}, "https://acme.atlassian.net/rest/api/3"),
        ({"instance": "jira.example.com", "api_version": 2}, "https://jira.example.com/rest/api/2"),
        ({"base_url": "https://jira.example.com"}, "https://jira.example.com/rest/api/3"),
        ({"base_url": "https://jira.example.com/rest/api/2"}, "https://jira.example.com/rest/api/2"),
    ],
    ids=["cloud-instance", "instance-with-api-version", "bare-base-url", "full-base-url"],
)
def test_build_jira_resolves_base_url(config: dict[str, Any], expected_base_url: str) -> None:
    connector = _build_connector("jira", config, {"token": "jira_token"})
    assert isinstance(connector, JiraConnector)
    assert connector._base_url == expected_base_url


def test_build_gitlab_self_hosted_base_url() -> None:
    connector = _build_connector(
        "gitlab",
        {"base_url": "https://gitlab.example.com/api/v4"},
        {"token": "gitlab_token"},
    )
    assert isinstance(connector, GitLabConnector)
    assert connector.connector_type == ConnectorType.GITLAB
    assert connector._base_url == "https://gitlab.example.com/api/v4"


# ── GHES base_url pass-through (FAR-1762) ─────────────────────────────────


def test_build_github_ghes_base_url() -> None:
    """A configured GHES base reaches the connector instead of github.com."""
    connector = _build_connector("github", {"base_url": "https://ghe.example.com/api/v3"}, {"token": "gh_token"})
    assert isinstance(connector, GitHubConnector)
    assert connector._base_url == "https://ghe.example.com/api/v3"


def test_build_github_default_base_url() -> None:
    """No config keeps the github.com default."""
    connector = _build_connector("github", {}, {"token": "gh_token"})
    assert isinstance(connector, GitHubConnector)
    assert connector._base_url == "https://api.github.com"


def test_build_github_blank_base_url_falls_back_to_default() -> None:
    """A present-but-blank ``base_url`` is 'no instance configured' — the
    github.com default applies, it is never passed through as an empty URL."""
    connector = _build_connector("github", {"base_url": "   "}, {"token": "gh_token"})
    assert isinstance(connector, GitHubConnector)
    assert connector._base_url == "https://api.github.com"


@pytest.mark.parametrize("type_id", ["github", "github_actions_ci"])
def test_build_github_family_null_base_url_falls_back_to_default(type_id: str) -> None:
    """An explicit ``base_url: null`` is 'no instance configured' too — the
    github.com default applies (the ``value is None`` arm of
    ``_optional_base_url``), never a ``None`` passed through as the URL."""
    connector = _build_connector(type_id, {"base_url": None}, {"token": "tok"})
    assert connector._base_url == "https://api.github.com"


def test_build_github_non_string_base_url_raises() -> None:
    """A mis-typed ``base_url`` fails loudly at build time, not at request time."""
    with pytest.raises(TypeError, match="base_url' must be a string"):
        _build_connector("github", {"base_url": 123}, {"token": "gh_token"})


def test_build_github_actions_ci_ghes_base_url() -> None:
    """FAR-1762: the CI runner takes the same GHES pass-through."""
    connector = _build_connector(
        "github_actions_ci",
        {"base_url": "https://ghe.example.com/api/v3"},
        {"token": "gha_token"},
    )
    assert isinstance(connector, GitHubActionsCIRunner)
    assert connector._base_url == "https://ghe.example.com/api/v3"


def test_build_github_actions_ci_default_base_url() -> None:
    connector = _build_connector("github_actions_ci", {}, {"token": "gha_token"})
    assert isinstance(connector, GitHubActionsCIRunner)
    assert connector._base_url == "https://api.github.com"


@pytest.mark.parametrize("type_id", ["github", "github_actions_ci"])
def test_github_family_client_targets_the_configured_ghes_base(monkeypatch: pytest.MonkeyPatch, type_id: str) -> None:
    """The configured base is what the pinned transport actually addresses.

    ``_client()`` builds the pinned client from ``self._base_url`` — assert on
    the built client (its ``base_url``), not just the attribute, so a
    re-hardcoded ``api.github.com`` inside ``_client`` fails here. The SSRF
    resolver is stubbed so no real DNS lookup happens.
    """
    from modulo.core import ssrf

    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda host: ["93.184.216.34"])
    connector = _build_connector(type_id, {"base_url": "https://ghe.example.com/api/v3"}, {"token": "tok"})
    assert isinstance(connector, (GitHubConnector, GitHubActionsCIRunner))
    client = connector._client()
    try:
        assert str(client.base_url).rstrip("/") == "https://ghe.example.com/api/v3"
    finally:
        asyncio.run(client.aclose())


# ── ticket-tracker provider dispatch ───────────────────────────────────────


def test_build_ticket_tracker_github_provider() -> None:
    connector = _build_connector("ticket-tracker", {"provider": "github"}, {"token": "tt_token"})
    assert connector.connector_type == ConnectorType.GITHUB


def test_build_ticket_tracker_trello_provider() -> None:
    connector = _build_connector("ticket-tracker", {"provider": "trello"}, {"api_key": "tt_trello", "token": "tt_tok"})
    assert connector.connector_type == ConnectorType.TICKET_TRACKER


def test_build_ticket_tracker_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unknown ticket-tracker provider"):
        _build_connector("ticket-tracker", {"provider": "asana"}, {})


# ── Plugin registry fallback ───────────────────────────────────────────────


def test_build_plugin_registry_fallback() -> None:
    """An unknown type_id is delegated to the plugin registry when registered there."""
    from modulo.core.plugin_registry import PluginManifest, PluginRegistry

    class _PluginConnector(ConnectorBase):
        @property
        def connector_type(self) -> ConnectorType:
            return ConnectorType.CUSTOM

        async def health_check(self) -> HealthResult:
            return HealthResult(ok=True)

        async def query(self, q: ConnectorQuery) -> ConnectorResult:
            return ConnectorResult(records=[{"p": True}])

        async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
            return {"p": True}

    registry = PluginRegistry()
    registry.register_connector_type(
        "my_custom_connector",
        lambda config, creds: _PluginConnector(),
        PluginManifest(PLUGIN_ID="pkg-demo", display_name="Demo", description="", version="1"),
    )
    with patch("modulo.core.connector_hub.get_plugin_registry", return_value=registry):
        connector = _build_connector("my_custom_connector", {}, {})
    assert isinstance(connector, _PluginConnector)
    assert connector.connector_type == ConnectorType.CUSTOM


# ── Failure modes ──────────────────────────────────────────────────────────


def test_build_missing_credential_raises() -> None:
    """Missing credential key raises ValueError naming the key and type."""
    with pytest.raises(ValueError, match="Missing credential key 'token' for connector type 'github'"):
        _build_connector("github", {}, {})


def test_build_missing_required_config_raises() -> None:
    """_require_config raises ValueError naming the missing config key."""
    with pytest.raises(ValueError, match="requires 'base_path' in config_json"):
        _build_connector("filesystem", {}, {})


def test_build_require_config_non_string_raises() -> None:
    """_require_config raises TypeError when the config value is not a string."""
    with pytest.raises(TypeError, match="must be a string"):
        _build_connector("filesystem", {"base_path": 123}, {})


def test_build_jira_missing_instance_and_base_url_raises() -> None:
    with pytest.raises(ValueError, match="instance"):
        _build_connector("jira", {}, {"token": "jira_token"})


def test_build_unknown_type_raises() -> None:
    with pytest.raises(ValueError, match="Unknown connector type"):
        _build_connector("definitely-not-a-connector", {}, {})
