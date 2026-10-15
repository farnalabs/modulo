"""Shared connector-hub test fixtures (FAR-1773).

Non-test fixture module: holds the known-type build-case matrix consumed by
``test_build_connector``, ``test_buildable_type_ids`` and
``tests/unit/api/test_connectors_endpoint``. Extracted so no test module
imports another test module.

Each row: ``(type_id, config, creds, expected_class, expected_type)``.
``expected_class`` is asserted with identity semantics so that distinct
implementations sharing a ConnectorType (e.g. the CI runners) stay distinct.
"""

from typing import Any

from modulo.connectors.asana import AsanaConnector
from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.base import ConnectorBase, ConnectorType
from modulo.connectors.bitbucket import BitbucketConnector
from modulo.connectors.buildkite import BuildkiteConnector
from modulo.connectors.ci_runner import GitHubActionsCIRunner, GitLabCIRunner
from modulo.connectors.circleci import CircleCIConnector
from modulo.connectors.codeclimate import CodeClimateConnector
from modulo.connectors.confluence import ConfluenceConnector
from modulo.connectors.datadog import DatadogConnector
from modulo.connectors.discord import DiscordConnector
from modulo.connectors.dropbox_paper import DropboxPaperConnector
from modulo.connectors.filesystem import FilesystemConnector
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.github import GitHubConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector
from modulo.connectors.monday import MondayConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.notion import NotionConnector
from modulo.connectors.npm import NpmConnector
from modulo.connectors.onepassword import OnePasswordConnector
from modulo.connectors.opsgenie import OpsgenieConnector
from modulo.connectors.pagerduty import PagerDutyConnector
from modulo.connectors.pypi import PyPIConnector
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.sharepoint import SharePointConnector
from modulo.connectors.shell import ShellConnector
from modulo.connectors.shortcut import ShortcutConnector
from modulo.connectors.slack import SlackConnector
from modulo.connectors.snyk import SnykConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.teamcity import TeamCityConnector
from modulo.connectors.trello import TrelloConnector
from modulo.connectors.youtrack import YouTrackConnector

BUILD_CASES: list[tuple[str, dict[str, Any], dict[str, Any], type[ConnectorBase], ConnectorType]] = [
    ("filesystem", {"base_path": "/tmp"}, {}, FilesystemConnector, ConnectorType.FILESYSTEM),
    ("gitea", {}, {"token": "gitea_token"}, GiteaConnector, ConnectorType.GITEA),
    (
        "azure_repos",
        {"organization": "acme"},
        {"token": "azure_repos_token"},
        AzureReposConnector,
        ConnectorType.AZURE_REPOS,
    ),
    ("bitbucket", {}, {"token": "bb_token"}, BitbucketConnector, ConnectorType.BITBUCKET),
    ("github", {}, {"token": "gh_token"}, GitHubConnector, ConnectorType.GITHUB),
    ("github_actions_ci", {}, {"token": "gha_token"}, GitHubActionsCIRunner, ConnectorType.CI_RUNNER),
    ("gitlab_ci", {}, {"token": "gl_token"}, GitLabCIRunner, ConnectorType.CI_RUNNER),
    ("gitlab", {}, {"token": "gitlab_token"}, GitLabConnector, ConnectorType.GITLAB),
    ("shell", {"allowed_commands": ["echo", "cat"]}, {}, ShellConnector, ConnectorType.SHELL),
    ("jira", {"instance": "acme.atlassian.net"}, {"token": "jira_token"}, JiraConnector, ConnectorType.JIRA),
    ("slack", {}, {"bot_token": "xoxb-test"}, SlackConnector, ConnectorType.SLACK),
    ("sharepoint", {}, {"token": "sp_token"}, SharePointConnector, ConnectorType.SHAREPOINT),
    ("shortcut", {}, {"token": "sc_token"}, ShortcutConnector, ConnectorType.SHORTCUT),
    ("trello", {}, {"api_key": "tt_trello", "token": "tt_tok"}, TrelloConnector, ConnectorType.TRELLO),
    ("asana", {}, {"personal_access_token": "asana_pat_123"}, AsanaConnector, ConnectorType.ASANA),
    ("monday", {}, {"api_key": "monday_key"}, MondayConnector, ConnectorType.MONDAY),
    (
        "youtrack",
        {"base_url": "https://youtrack.example.com/api"},
        {"token": "yt_token"},
        YouTrackConnector,
        ConnectorType.YOUTRACK,
    ),
    ("notion", {}, {"token": "ntn_token"}, NotionConnector, ConnectorType.NOTION),
    ("npm", {}, {"token": "npm_token"}, NpmConnector, ConnectorType.NPM),
    ("pypi", {}, {"token": "pypi_token"}, PyPIConnector, ConnectorType.PYPI),
    ("dropbox_paper", {}, {"token": "dbp_token"}, DropboxPaperConnector, ConnectorType.DROPBOX_PAPER),
    ("buildkite", {}, {"token": "bk_token"}, BuildkiteConnector, ConnectorType.BUILDKITE),
    ("circleci", {}, {"token": "cc_token"}, CircleCIConnector, ConnectorType.CI_RUNNER),
    ("jenkins", {}, {"username": "u", "token": "jenkins_token"}, JenkinsConnector, ConnectorType.JENKINS),
    (
        "confluence",
        {"instance": "my-domain.atlassian.net/wiki"},
        {"token": "confluence_token"},
        ConfluenceConnector,
        ConnectorType.CONFLUENCE,
    ),
    ("teamcity", {}, {"token": "tc_token"}, TeamCityConnector, ConnectorType.TEAMCITY),
    (
        "azure_key_vault",
        {"vault_url": "https://acme.vault.azure.net"},
        {"token": "kv_token"},
        AzureKeyVaultConnector,
        ConnectorType.AZURE_KEY_VAULT,
    ),
    (
        "azure_pipelines",
        {"organization": "acme"},
        {"token": "ap_token"},
        AzurePipelinesConnector,
        ConnectorType.AZURE_PIPELINES,
    ),
    ("datadog", {}, {"api_key": "dd_key", "application_key": "dd_app"}, DatadogConnector, ConnectorType.DATADOG),
    ("sentry", {"organization": "acme"}, {"token": "sentry_token"}, SentryConnector, ConnectorType.SENTRY),
    ("pagerduty", {}, {"token": "pd_token"}, PagerDutyConnector, ConnectorType.PAGERDUTY),
    ("grafana", {}, {"token": "grafana_token"}, GrafanaConnector, ConnectorType.GRAFANA),
    ("microsoft_teams", {}, {"token": "mst_token"}, MicrosoftTeamsConnector, ConnectorType.MICROSOFT_TEAMS),
    ("discord", {}, {"token": "discord_token"}, DiscordConnector, ConnectorType.DISCORD),
    ("onepassword", {}, {"token": "1p_token"}, OnePasswordConnector, ConnectorType.ONEPASSWORD),
    ("opsgenie", {}, {"api_key": "og_key"}, OpsgenieConnector, ConnectorType.OPSGENIE),
    ("sonarqube", {}, {"token": "sq_token"}, SonarQubeConnector, ConnectorType.SONARQUBE),
    ("codeclimate", {}, {"token": "cc2_token"}, CodeClimateConnector, ConnectorType.CODECLIMATE),
    ("snyk", {}, {"token": "snyk_token"}, SnykConnector, ConnectorType.SNYK),
    ("n8n", {}, {"token": "n8n_token"}, N8NConnector, ConnectorType.N8N),
]
