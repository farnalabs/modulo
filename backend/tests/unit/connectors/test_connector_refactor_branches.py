"""Focused branch coverage for the S3776 connector decompositions.

Covers the defensive branches split out of the Azure Pipelines run parser, the
Jira transport-error mapper and the SharePoint page-parameter builder.
"""

import httpx

from modulo.connectors.azure_pipelines import AzurePipelinesConnector
from modulo.connectors.base import ConnectorQuery
from modulo.connectors.jira import _network_error_message
from modulo.connectors.sharepoint import SharePointConnector


def _azure_connector() -> AzurePipelinesConnector:
    return AzurePipelinesConnector(token="pat", organization="org", project="proj")


def test_parse_run_ignores_non_dict_repositories() -> None:
    run = _azure_connector()._parse_run({"id": 7, "resources": {"repositories": []}})

    assert not run.branch
    assert not run.commit_sha


def test_jira_network_error_message_maps_connection_errors() -> None:
    assert _network_error_message(httpx.ConnectError("boom")) == "Jira API connection error"


def test_jira_network_error_message_maps_timeouts() -> None:
    assert _network_error_message(httpx.TimeoutException("slow")) == "Jira API timeout"


def test_sharepoint_page_params_without_limit_or_cursor() -> None:
    params = SharePointConnector._page_params(ConnectorQuery(resource="sites", limit=0))

    assert params == {}
