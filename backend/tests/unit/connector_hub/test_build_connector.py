"""Unit tests for ConnectorHub._build_connector factory coverage."""

import pytest

from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub import _build_connector


def test_build_gitea_connector():
    connector = _build_connector("gitea", {"base_url": "https://gitea.example.com"}, {"token": "gitea_token"})
    assert connector.connector_type == ConnectorType.GITEA


def test_build_azure_repos_connector():
    connector = _build_connector(
        "azure_repos",
        {"organization": "acme"},
        {"token": "azure_repos_token"},
    )
    assert connector.connector_type == ConnectorType.AZURE_REPOS


def test_build_bitbucket_connector():
    connector = _build_connector("bitbucket", {}, {"token": "bb_token"})
    assert connector.connector_type == ConnectorType.BITBUCKET


def test_build_github_actions_ci_runner():
    connector = _build_connector("github_actions_ci", {}, {"token": "gha_token"})
    assert connector.connector_type == ConnectorType.CI_RUNNER


def test_build_gitlab_ci_runner():
    connector = _build_connector("gitlab_ci", {}, {"token": "gl_token"})
    assert connector.connector_type == ConnectorType.CI_RUNNER


def test_build_gitlab_connector():
    connector = _build_connector("gitlab", {}, {"token": "gitlab_token"})
    assert connector.connector_type == ConnectorType.GITLAB


def test_build_gitlab_connector_self_hosted():
    connector = _build_connector(
        "gitlab",
        {"base_url": "https://gitlab.example.com/api/v4"},
        {"token": "gitlab_token"},
    )
    assert connector.connector_type == ConnectorType.GITLAB
    assert connector._base_url == "https://gitlab.example.com/api/v4"


def test_build_linear_connector():
    connector = _build_connector("linear", {}, {"api_key": "linear_key"})
    assert connector.connector_type == ConnectorType.LINEAR


def test_build_jira_connector_with_instance():
    connector = _build_connector("jira", {"instance": "acme.atlassian.net"}, {"token": "jira_token"})
    assert connector.connector_type == ConnectorType.JIRA


def test_build_jira_connector_with_base_url():
    connector = _build_connector(
        "jira",
        {"base_url": "https://jira.example.com/rest/api/2"},
        {"token": "jira_token"},
    )
    assert connector.connector_type == ConnectorType.JIRA
    assert connector._base_url == "https://jira.example.com/rest/api/2"


def test_build_jira_connector_with_api_version():
    connector = _build_connector(
        "jira",
        {"instance": "jira.example.com", "api_version": 2},
        {"token": "jira_token"},
    )
    assert connector.connector_type == ConnectorType.JIRA
    assert connector._base_url == "https://jira.example.com/rest/api/2"


def test_build_jira_connector_missing_instance_raises():
    with pytest.raises(ValueError, match="instance"):
        _build_connector("jira", {}, {"token": "jira_token"})


def test_build_slack_connector():
    connector = _build_connector("slack", {}, {"bot_token": "xoxb-test"})
    assert connector.connector_type == ConnectorType.SLACK


def test_build_sharepoint_connector():
    connector = _build_connector("sharepoint", {}, {"token": "sp_token"})
    assert connector.connector_type == ConnectorType.SHAREPOINT


def test_build_npm_connector():
    connector = _build_connector("npm", {}, {"token": "npm_token"})
    assert connector.connector_type == ConnectorType.NPM


def test_build_pypi_connector():
    connector = _build_connector("pypi", {}, {"token": "pypi_token"})
    assert connector.connector_type == ConnectorType.PYPI


def test_build_dropbox_paper_connector():
    connector = _build_connector("dropbox_paper", {}, {"token": "dbp_token"})
    assert connector.connector_type == ConnectorType.DROPBOX_PAPER


def test_build_buildkite_connector():
    connector = _build_connector("buildkite", {}, {"token": "bk_token"})
    assert connector.connector_type == ConnectorType.BUILDKITE


def test_build_circleci_connector():
    connector = _build_connector("circleci", {}, {"token": "cc_token"})
    assert connector.connector_type == ConnectorType.CI_RUNNER


def test_build_jenkins_connector():
    connector = _build_connector("jenkins", {}, {"username": "u", "token": "jenkins_token"})
    assert connector.connector_type == ConnectorType.JENKINS


def test_build_teamcity_connector():
    connector = _build_connector("teamcity", {}, {"token": "tc_token"})
    assert connector.connector_type == ConnectorType.TEAMCITY


def test_build_azure_key_vault_connector():
    connector = _build_connector(
        "azure_key_vault",
        {"vault_url": "https://acme.vault.azure.net"},
        {"token": "kv_token"},
    )
    assert connector.connector_type == ConnectorType.AZURE_KEY_VAULT


def test_build_azure_pipelines_connector():
    connector = _build_connector(
        "azure_pipelines",
        {"organization": "acme"},
        {"token": "ap_token"},
    )
    assert connector.connector_type == ConnectorType.AZURE_PIPELINES


def test_build_datadog_connector():
    connector = _build_connector("datadog", {}, {"api_key": "dd_key", "app_key": "dd_app"})
    assert connector.connector_type == ConnectorType.DATADOG


def test_build_sentry_connector():
    connector = _build_connector("sentry", {"organization": "acme"}, {"token": "sentry_token"})
    assert connector.connector_type == ConnectorType.SENTRY


def test_build_pagerduty_connector():
    connector = _build_connector("pagerduty", {}, {"token": "pd_token"})
    assert connector.connector_type == ConnectorType.PAGERDUTY


def test_build_grafana_connector():
    connector = _build_connector("grafana", {}, {"token": "grafana_token"})
    assert connector.connector_type == ConnectorType.GRAFANA


def test_build_microsoft_teams_connector():
    connector = _build_connector("microsoft_teams", {}, {"token": "mst_token"})
    assert connector.connector_type == ConnectorType.MICROSOFT_TEAMS


def test_build_discord_connector():
    connector = _build_connector("discord", {}, {"token": "discord_token"})
    assert connector.connector_type == ConnectorType.DISCORD


def test_build_onepassword_connector():
    connector = _build_connector("onepassword", {}, {"token": "1p_token"})
    assert connector.connector_type == ConnectorType.ONEPASSWORD


def test_build_opsgenie_connector():
    connector = _build_connector("opsgenie", {}, {"api_key": "og_key"})
    assert connector.connector_type == ConnectorType.OPSGENIE


def test_build_sonarqube_connector():
    connector = _build_connector("sonarqube", {}, {"token": "sq_token"})
    assert connector.connector_type == ConnectorType.SONARQUBE


def test_build_codeclimate_connector():
    connector = _build_connector("codeclimate", {}, {"token": "cc2_token"})
    assert connector.connector_type == ConnectorType.CODECLIMATE


def test_build_snyk_connector():
    connector = _build_connector("snyk", {}, {"token": "snyk_token"})
    assert connector.connector_type == ConnectorType.SNYK


def test_build_trivy_connector():
    connector = _build_connector("trivy", {}, {"token": "trivy_token"})
    assert connector.connector_type == ConnectorType.TRIVY


def test_build_n8n_connector():
    connector = _build_connector("n8n", {}, {"token": "n8n_token"})
    assert connector.connector_type == ConnectorType.N8N


def test_build_ticket_tracker_github_provider():
    connector = _build_connector("ticket-tracker", {"provider": "github"}, {"token": "tt_token"})
    assert connector.connector_type == ConnectorType.GITHUB


def test_build_ticket_tracker_trello_provider():
    connector = _build_connector("ticket-tracker", {"provider": "trello"}, {"api_key": "tt_trello", "token": "tt_tok"})
    assert connector.connector_type == ConnectorType.TICKET_TRACKER


def test_build_ticket_tracker_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown ticket-tracker provider"):
        _build_connector("ticket-tracker", {"provider": "asana"}, {})


def test_build_missing_credential_raises():
    """Missing credential key raises ValueError naming the key and type."""
    with pytest.raises(ValueError, match="Missing credential key 'token' for connector type 'github'"):
        _build_connector("github", {}, {})


def test_build_require_config_non_string_raises():
    """_require_config raises TypeError when the config value is not a string."""
    with pytest.raises(TypeError, match="must be a string"):
        _build_connector("filesystem", {"base_path": 123}, {})


def test_build_unknown_type_raises():
    with pytest.raises(ValueError, match="Unknown connector type"):
        _build_connector("definitely-not-a-connector", {}, {})
