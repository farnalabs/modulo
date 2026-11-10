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

from farna.connectors.asana import AsanaConnector
from farna.connectors.azure_key_vault import AzureKeyVaultConnector
from farna.connectors.azure_pipelines import AzurePipelinesConnector
from farna.connectors.azure_repos import AzureReposConnector
from farna.connectors.base import ConnectorBase, ConnectorType
from farna.connectors.bitbucket import BitbucketConnector
from farna.connectors.buildkite import BuildkiteConnector
from farna.connectors.ci_runner import GitHubActionsCIRunner, GitLabCIRunner, WoodpeckerCIRunner
from farna.connectors.circleci import CircleCIConnector
from farna.connectors.codeclimate import CodeClimateConnector
from farna.connectors.confluence import ConfluenceConnector
from farna.connectors.datadog import DatadogConnector
from farna.connectors.discord import DiscordConnector
from farna.connectors.dropbox_paper import DropboxPaperConnector
from farna.connectors.filesystem import FilesystemConnector
from farna.connectors.gitea import GiteaConnector
from farna.connectors.github import GitHubConnector
from farna.connectors.gitlab import GitLabConnector
from farna.connectors.grafana import GrafanaConnector
from farna.connectors.jenkins import JenkinsConnector
from farna.connectors.jira import JiraConnector
from farna.connectors.microsoft_teams import MicrosoftTeamsConnector
from farna.connectors.monday import MondayConnector
from farna.connectors.n8n import N8NConnector
from farna.connectors.notion import NotionConnector
from farna.connectors.npm import NpmConnector
from farna.connectors.onepassword import OnePasswordConnector
from farna.connectors.opsgenie import OpsgenieConnector
from farna.connectors.pagerduty import PagerDutyConnector
from farna.connectors.plane import PlaneConnector
from farna.connectors.pypi import PyPIConnector
from farna.connectors.sentry import SentryConnector
from farna.connectors.sharepoint import SharePointConnector
from farna.connectors.shell import ShellConnector
from farna.connectors.shortcut import ShortcutConnector
from farna.connectors.slack import SlackConnector
from farna.connectors.snyk import SnykConnector
from farna.connectors.sonarqube import SonarQubeConnector
from farna.connectors.teamcity import TeamCityConnector
from farna.connectors.trello import TrelloConnector
from farna.connectors.youtrack import YouTrackConnector

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
    (
        "woodpecker",
        {"base_url": "https://ci.example.com"},
        {"token": "wp_token"},
        WoodpeckerCIRunner,
        ConnectorType.CI_RUNNER,
    ),
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
    (
        "plane",
        {"base_url": "https://plane.example.com/api/v1", "workspace_slug": "acme"},
        {"token": "plane_token"},
        PlaneConnector,
        ConnectorType.TICKET_TRACKER,
    ),
]
