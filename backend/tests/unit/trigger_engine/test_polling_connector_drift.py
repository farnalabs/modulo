"""Validate that _build_polling_connector stays in sync with connector_hub._build_connector.

If new connector types are added to the hub but not to polling, this test
will fail and force an explicit decision.
"""

import ast
import inspect

from modulo.connectors.base import ConnectorBase
from modulo.core.trigger_engine.polling import _build_polling_connector


def _get_hub_connector_types() -> set[str]:
    """Parse _build_connector's match/case arms to extract type strings."""
    from modulo.core.connector_hub import _build_connector

    source = inspect.getsource(_build_connector)
    tree = ast.parse(source)

    hub_types: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.match_case)
            and isinstance(node.pattern, ast.MatchValue)
            and isinstance(node.pattern.value, ast.Constant)
        ):
            hub_types.add(node.pattern.value.value)
    return hub_types


def test_all_connector_types_have_polling_parity():
    """Every non-excluded connector type in connector_hub must be handled by
    _build_polling_connector or explicitly excluded."""
    polling_types = {"filesystem", "github", "gitlab", "jira", "slack", "linear", "rest"}

    excluded_types: dict[str, str] = {
        "shell": "requires runtime_provider — polling has no run context",
        "github_actions_ci": "CI runner — side-effect only",
        "gitlab_ci": "CI runner — side-effect only",
        "azure_pipelines": "CI runner — side-effect only",
        "buildkite": "CI status — not yet implemented in _build_polling_connector",
        "circleci": "CI status — not yet implemented in _build_polling_connector",
        "jenkins": "CI status — not yet implemented in _build_polling_connector",
        "teamcity": "CI status — not yet implemented in _build_polling_connector",
        "azure_key_vault": "credential management — not queryable data",
        "onepassword": "credential management — not queryable data",
        "gitea": "not yet implemented in _build_polling_connector",
        "azure_repos": "not yet implemented in _build_polling_connector",
        "asana": "not yet implemented in _build_polling_connector",
        "monday": "not yet implemented in _build_polling_connector",
        "shortcut": "not yet implemented in _build_polling_connector",
        "trello": "not yet implemented in _build_polling_connector",
        "youtrack": "not yet implemented in _build_polling_connector",
        "confluence": "not yet implemented in _build_polling_connector",
        "notion": "not yet implemented in _build_polling_connector",
        "dropbox_paper": "not yet implemented in _build_polling_connector",
        "sharepoint": "not yet implemented in _build_polling_connector",
        "npm": "not yet implemented in _build_polling_connector",
        "pypi": "not yet implemented in _build_polling_connector",
        "datadog": "not yet implemented in _build_polling_connector",
        "sentry": "not yet implemented in _build_polling_connector",
        "pagerduty": "not yet implemented in _build_polling_connector",
        "grafana": "not yet implemented in _build_polling_connector",
        "opsgenie": "not yet implemented in _build_polling_connector",
        "sonarqube": "not yet implemented in _build_polling_connector",
        "codeclimate": "not yet implemented in _build_polling_connector",
        "snyk": "not yet implemented in _build_polling_connector",
        "trivy": "not yet implemented in _build_polling_connector",
        "discord": "not yet implemented in _build_polling_connector",
        "microsoft_teams": "not yet implemented in _build_polling_connector",
        "n8n": "not yet implemented in _build_polling_connector",
        "bitbucket": "not yet implemented in _build_polling_connector",
        "ticket-tracker": "not yet implemented in _build_polling_connector",
    }

    hub_types = _get_hub_connector_types()
    missing = hub_types - polling_types - set(excluded_types)

    assert not missing, (
        f"connector_hub types missing from _build_polling_connector: {sorted(missing)}. "
        "Add support to _build_polling_connector or add to excluded_types with a reason."
    )


def test_polling_types_are_valid_connectors():
    """Verify each polling-supported type actually instantiates with minimal config."""
    test_cases: list[tuple[str, dict, dict]] = [
        ("filesystem", {"base_path": "/tmp"}, {}),
        ("github", {}, {"token": "x"}),
        ("gitlab", {}, {"token": "x"}),
        ("gitlab", {"base_url": "https://gitlab.example.com/api/v4"}, {"token": "x"}),
        ("jira", {"instance": "x"}, {"token": "x"}),
        ("slack", {}, {"bot_token": "x"}),
        ("linear", {}, {"token": "x"}),
        ("rest", {"base_url": "https://api.example.com"}, {"auth_mode": "bearer", "token": "t"}),
    ]
    for type_id, config, creds in test_cases:
        connector = _build_polling_connector(type_id, config, creds)
        assert isinstance(connector, ConnectorBase), f"{type_id} should return a ConnectorBase instance"


def test_hub_has_minimum_types():
    """Sanity check — the hub should have more types than polling supports."""
    hub_types = _get_hub_connector_types()
    assert len(hub_types) >= 20, (
        f"Expected at least 20 hub connector types, got {len(hub_types)}. Has the hub been refactored?"
    )
