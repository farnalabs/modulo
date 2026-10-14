"""Shared health-check contract suite for token-API connectors.

Every connector's ``health_check()`` degrades failure the same way: ``200``
-> ``ok=True`` (plus a connector-specific positive detail), auth failures
(401/403) -> a connector-specific ``"Invalid ..."`` / ``"... permissions"``
detail, other HTTP statuses -> the status code echoed in the detail,
transport errors -> the error message in the detail, and unexpected
exceptions -> ``ok=False`` via ``health_check_failure`` without raising.

Rather than repeating near-identical test bodies in each connector's test
file, this module exercises that contract once, parametrized over the
connectors that share it. Every case was moved verbatim from the
per-connector test file named in the per-spec provenance comment — same
route spec, same response body, same assertions; only the boilerplate
(decorator + fixture plumbing) is factored into *_run_health_scenario.

Connector-specific cases are NOT the shared contract and stay in each
connector's own test file: request-shape assertions (jira token-auth
Authorization header, self-hosted path), scope-probe mechanics (gitlab
token-info non-fatal probes / scope-cache warming / self-hosted probe root,
gitea custom base URLs), connector-specific detail text for timeout errors
(gitlab), body-driven ok/False contracts (slack ``{"ok": false}``, sonarqube
GREEN/YELLOW/RED health levels), and the SSRF egress gate
(test_connector_egress_gate.py).

The five CI-runner connectors (buildkite, circleci, azure_pipelines,
jenkins, teamcity) share an identical ``health_check`` body — "Authentication
failed" for auth responses and the status code for anything else — so their
cases live here too. Their test doubles (canned state machines, no HTTP) each
carried an identical one-line ``test_double_health_check``; the five copies
are deduplicated here as ``test_ci_test_double_health_check_ok``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest
import respx

from modulo.connectors.azure_key_vault import AzureKeyVaultConnector
from modulo.connectors.azure_pipelines import AzurePipelinesConnector, _AzurePipelinesTestDouble
from modulo.connectors.azure_repos import AzureReposConnector
from modulo.connectors.bitbucket import BitbucketConnector
from modulo.connectors.buildkite import BuildkiteConnector, _BuildkiteTestDouble
from modulo.connectors.circleci import CircleCIConnector, _CircleCITestDouble
from modulo.connectors.codeclimate import CodeClimateConnector
from modulo.connectors.datadog import DatadogConnector
from modulo.connectors.discord import DiscordConnector
from modulo.connectors.gitea import GiteaConnector
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.grafana import GrafanaConnector
from modulo.connectors.jenkins import JenkinsConnector, _JenkinsTestDouble
from modulo.connectors.jira import JiraConnector
from modulo.connectors.microsoft_teams import MicrosoftTeamsConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.npm import NpmConnector
from modulo.connectors.onepassword import OnePasswordConnector
from modulo.connectors.opsgenie import OpsgenieConnector
from modulo.connectors.pagerduty import PagerDutyConnector
from modulo.connectors.pypi import PyPIConnector
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.sharepoint import SharePointConnector
from modulo.connectors.snyk import SnykConnector
from modulo.connectors.teamcity import TeamCityConnector, _TeamCityTestDouble

# --------------------------------------------------------------------------
# Spec + scenario data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _HealthRoute:
    """One mocked HTTP endpoint the connector's health check may hit."""

    url: str
    params: dict[str, str | int] | None = None
    response: httpx.Response | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class _HealthScenario:
    """One contract case: what to mock, and what health_check must report.

    Assertion shape matches each original case: ``detail_equals`` pins an
    exact positive detail, ``detail_contains`` pins substring containment,
    and both left empty means the original asserted only the ``ok`` flag.
    """

    case: str
    routes: tuple[_HealthRoute, ...]
    expect_ok: bool
    detail_contains: tuple[str, ...] = ()
    detail_equals: str | None = None
    token: bool = False


@dataclass(frozen=True)
class _HealthSpec:
    """One connector's placement in the shared health-check contract.

    ``token_builder`` is the token-carrying construction used by cases in
    token-API connectors whose no-token builder would short-circuit before
    the request (PyPI and npm authorise only with a token).
    """

    connector: str
    builder: Callable[[], object]
    scenarios: tuple[_HealthScenario, ...]
    token_builder: Callable[[], object] | None = None


# --------------------------------------------------------------------------
# The contract matrix — cases moved verbatim from each connector's test file
# --------------------------------------------------------------------------

_DATADOG = _HealthSpec(
    connector="datadog",  # test_datadog.py — copied verbatim (3 cases)
    builder=lambda: DatadogConnector(api_key="dummy_api_key", app_key="dummy_app_key", site="us"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://api.datadoghq.com/api/v1/validate",
                    response=httpx.Response(200, json={"valid": True}),
                ),
            ),
            expect_ok=True,
            detail_equals="Datadog API key validated",
        ),
        _HealthScenario(
            case="invalid_key",
            routes=(
                _HealthRoute(
                    url="https://api.datadoghq.com/api/v1/validate",
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://api.datadoghq.com/api/v1/validate",
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
    ),
)

_SNYK = _HealthSpec(
    connector="snyk",  # test_snyk.py — copied verbatim (6 cases)
    builder=lambda: SnykConnector(token="snyk_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://api.snyk.io/rest/orgs",
                    params={"limit": 1, "version": "2024-10-15"},
                    response=httpx.Response(200, json={"data": []}),
                ),
            ),
            expect_ok=True,
            detail_contains=("validated",),
        ),
        _HealthScenario(
            case="unauthorized",
            routes=(
                _HealthRoute(
                    url="https://api.snyk.io/rest/orgs",
                    params={"limit": 1, "version": "2024-10-15"},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="forbidden",
            routes=(
                _HealthRoute(
                    url="https://api.snyk.io/rest/orgs",
                    params={"limit": 1, "version": "2024-10-15"},
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("permissions",),
        ),
        _HealthScenario(
            case="connection_error",
            routes=(
                _HealthRoute(
                    url="https://api.snyk.io/rest/orgs",
                    params={"limit": 1, "version": "2024-10-15"},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Cannot connect",),
        ),
        _HealthScenario(
            case="generic_error",
            routes=(
                _HealthRoute(
                    url="https://api.snyk.io/rest/orgs",
                    params={"limit": 1, "version": "2024-10-15"},
                    error=ValueError("weird error"),
                ),
            ),
            expect_ok=False,
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://api.snyk.io/rest/orgs",
                    params={"limit": 1, "version": "2024-10-15"},
                    response=httpx.Response(500, text="Internal Server Error"),
                ),
            ),
            expect_ok=False,
            detail_contains=("500",),
        ),
    ),
)

_PYPI = _HealthSpec(
    connector="pypi",  # test_pypi.py — copied verbatim (6 cases)
    builder=lambda: PyPIConnector(),
    token_builder=lambda: PyPIConnector(token="pypi_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://pypi.org/pypi/",
                    response=httpx.Response(200, text="PyPI"),
                ),
            ),
            expect_ok=True,
            detail_contains=("reachable",),
        ),
        _HealthScenario(
            case="unauthorized",
            routes=(
                _HealthRoute(
                    url="https://pypi.org/pypi/",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
            token=True,
        ),
        _HealthScenario(
            case="forbidden",
            routes=(
                _HealthRoute(
                    url="https://pypi.org/pypi/",
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("permissions",),
            token=True,
        ),
        _HealthScenario(
            case="connection_error",
            routes=(
                _HealthRoute(
                    url="https://pypi.org/pypi/",
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Cannot connect",),
        ),
        _HealthScenario(
            case="generic_error",
            routes=(
                _HealthRoute(
                    url="https://pypi.org/pypi/",
                    error=ValueError("weird error"),
                ),
            ),
            expect_ok=False,
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://pypi.org/pypi/",
                    response=httpx.Response(500, text="Internal Server Error"),
                ),
            ),
            expect_ok=False,
            detail_contains=("500",),
        ),
    ),
)

_NPM = _HealthSpec(
    connector="npm",  # test_npm.py — copied verbatim (6 cases)
    builder=lambda: NpmConnector(),
    token_builder=lambda: NpmConnector(token="npm_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://registry.npmjs.org/-/v1/search",
                    params={"text": "express", "size": 1},
                    response=httpx.Response(200, json={"objects": [], "total": 0}),
                ),
            ),
            expect_ok=True,
            detail_contains=("reachable",),
        ),
        _HealthScenario(
            case="unauthorized",
            routes=(
                _HealthRoute(
                    url="https://registry.npmjs.org/-/v1/search",
                    params={"text": "express", "size": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
            token=True,
        ),
        _HealthScenario(
            case="forbidden",
            routes=(
                _HealthRoute(
                    url="https://registry.npmjs.org/-/v1/search",
                    params={"text": "express", "size": 1},
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("permissions",),
            token=True,
        ),
        _HealthScenario(
            case="connection_error",
            routes=(
                _HealthRoute(
                    url="https://registry.npmjs.org/-/v1/search",
                    params={"text": "express", "size": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Cannot connect",),
        ),
        _HealthScenario(
            case="generic_error",
            routes=(
                _HealthRoute(
                    url="https://registry.npmjs.org/-/v1/search",
                    params={"text": "express", "size": 1},
                    error=ValueError("weird error"),
                ),
            ),
            expect_ok=False,
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://registry.npmjs.org/-/v1/search",
                    params={"text": "express", "size": 1},
                    response=httpx.Response(500, text="Internal Server Error"),
                ),
            ),
            expect_ok=False,
            detail_contains=("500",),
        ),
    ),
)

_N8N = _HealthSpec(
    connector="n8n",  # test_n8n.py — copied verbatim (6 cases)
    builder=lambda: N8NConnector(token="n8n_test_token", base_url="http://localhost:5678"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="http://localhost:5678/api/v1/workflows",
                    params={"limit": 1},
                    response=httpx.Response(200, json={"data": []}),
                ),
            ),
            expect_ok=True,
            detail_contains=("reachable",),
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="http://localhost:5678/api/v1/workflows",
                    params={"limit": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid n8n API token",),
        ),
        _HealthScenario(
            case="connect_error",
            routes=(
                _HealthRoute(
                    url="http://localhost:5678/api/v1/workflows",
                    params={"limit": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Cannot connect",),
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="http://localhost:5678/api/v1/workflows",
                    params={"limit": 1},
                    response=httpx.Response(429, text="Too Many Requests"),
                ),
            ),
            expect_ok=False,
            detail_contains=("429",),
        ),
        _HealthScenario(
            case="generic_error",
            routes=(
                _HealthRoute(
                    url="http://localhost:5678/api/v1/workflows",
                    params={"limit": 1},
                    error=RuntimeError("unexpected"),
                ),
            ),
            expect_ok=False,
        ),
        # TimeoutException is an httpx.HTTPError subclass like ConnectError;
        # the original asserted only ok (no detail text).
        _HealthScenario(
            case="network_timeout",
            routes=(
                _HealthRoute(
                    url="http://localhost:5678/api/v1/workflows",
                    params={"limit": 1},
                    error=httpx.TimeoutException("timed out"),
                ),
            ),
            expect_ok=False,
        ),
    ),
)

_DISCORD = _HealthSpec(
    connector="discord",  # test_discord.py — copied verbatim (4 cases)
    builder=lambda: DiscordConnector(token="discord_bot_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://discord.com/api/v10/users/@me",
                    response=httpx.Response(200, json={"id": "123", "username": "ModuloBot"}),
                ),
            ),
            expect_ok=True,
            detail_equals="ModuloBot",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="https://discord.com/api/v10/users/@me",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid Discord bot token",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://discord.com/api/v10/users/@me",
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://discord.com/api/v10/users/@me",
                    response=httpx.Response(429, text="Too Many Requests"),
                ),
            ),
            expect_ok=False,
            detail_contains=("429",),
        ),
    ),
)

_MICROSOFT_TEAMS = _HealthSpec(
    connector="microsoft_teams",  # test_microsoft_teams.py — copied verbatim (4 cases)
    builder=lambda: MicrosoftTeamsConnector(token="ms_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/users",
                    params={"$top": 1, "$select": "id"},
                    response=httpx.Response(200, json={"value": [{"id": "U1"}]}),
                ),
            ),
            expect_ok=True,
            detail_equals="Microsoft Graph API token validated",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/users",
                    params={"$top": 1, "$select": "id"},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid Microsoft Graph API token",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/users",
                    params={"$top": 1, "$select": "id"},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/users",
                    params={"$top": 1, "$select": "id"},
                    response=httpx.Response(429, text="Too Many Requests"),
                ),
            ),
            expect_ok=False,
            detail_contains=("429",),
        ),
    ),
)

_CODECLIMATE = _HealthSpec(
    connector="codeclimate",  # test_codeclimate.py — copied verbatim (4 cases)
    builder=lambda: CodeClimateConnector(token="cc_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://api.codeclimate.com/v1/repos",
                    params={"limit": 1},
                    response=httpx.Response(200, json={"data": []}),
                ),
            ),
            expect_ok=True,
            detail_equals="Code Climate API token validated",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="https://api.codeclimate.com/v1/repos",
                    params={"limit": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://api.codeclimate.com/v1/repos",
                    params={"limit": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
        _HealthScenario(
            case="http_error",
            routes=(
                _HealthRoute(
                    url="https://api.codeclimate.com/v1/repos",
                    params={"limit": 1},
                    response=httpx.Response(500, text="Internal Server Error"),
                ),
            ),
            expect_ok=False,
            detail_contains=("HTTP 500",),
        ),
    ),
)

_PAGERDUTY = _HealthSpec(
    connector="pagerduty",  # test_pagerduty.py — copied verbatim (4 cases)
    builder=lambda: PagerDutyConnector(token="pd_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://api.pagerduty.com/users",
                    params={"limit": 1},
                    response=httpx.Response(200, json={"users": [{"id": "U1"}]}),
                ),
            ),
            expect_ok=True,
            detail_equals="PagerDuty API token validated",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="https://api.pagerduty.com/users",
                    params={"limit": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid PagerDuty API token",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://api.pagerduty.com/users",
                    params={"limit": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://api.pagerduty.com/users",
                    params={"limit": 1},
                    response=httpx.Response(429, text="Too Many Requests"),
                ),
            ),
            expect_ok=False,
            detail_contains=("429",),
        ),
    ),
)

_SENTRY = _HealthSpec(
    connector="sentry",  # test_sentry.py — copied verbatim (3 cases)
    builder=lambda: SentryConnector(token="sntry_test_token", organization="test-org"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://sentry.io/api/0/",
                    response=httpx.Response(200, json={}),
                ),
            ),
            expect_ok=True,
            detail_equals="Sentry API token validated",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="https://sentry.io/api/0/",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://sentry.io/api/0/",
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
    ),
)

_ONEPASSWORD = _HealthSpec(
    connector="onepassword",  # test_onepassword.py — copied verbatim (4 cases)
    builder=lambda: OnePasswordConnector(token="op_test_token", base_url="http://localhost:8080"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="http://localhost:8080/v1/vaults",
                    params={"limit": 1},
                    response=httpx.Response(200, json=[]),
                ),
            ),
            expect_ok=True,
            detail_equals="1Password Connect token validated",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="http://localhost:8080/v1/vaults",
                    params={"limit": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="other_http_error",
            routes=(
                _HealthRoute(
                    url="http://localhost:8080/v1/vaults",
                    params={"limit": 1},
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("HTTP 403",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="http://localhost:8080/v1/vaults",
                    params={"limit": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
    ),
)

_OPSGENIE = _HealthSpec(
    connector="opsgenie",  # test_opsgenie.py — copied verbatim (5 cases)
    builder=lambda: OpsgenieConnector(api_key="og_test_key"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://api.opsgenie.com/v2/alerts",
                    params={"limit": 1},
                    response=httpx.Response(200, json={"data": [], "totalCount": 0}),
                ),
            ),
            expect_ok=True,
            detail_equals="Opsgenie API key validated",
        ),
        _HealthScenario(
            case="invalid_key",
            routes=(
                _HealthRoute(
                    url="https://api.opsgenie.com/v2/alerts",
                    params={"limit": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid Opsgenie API key",),
        ),
        _HealthScenario(
            case="forbidden",
            routes=(
                _HealthRoute(
                    url="https://api.opsgenie.com/v2/alerts",
                    params={"limit": 1},
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid Opsgenie API key",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://api.opsgenie.com/v2/alerts",
                    params={"limit": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="https://api.opsgenie.com/v2/alerts",
                    params={"limit": 1},
                    response=httpx.Response(429, text="Too Many Requests"),
                ),
            ),
            expect_ok=False,
            detail_contains=("429",),
        ),
    ),
)

_GRAFANA = _HealthSpec(
    connector="grafana",  # test_grafana.py — copied verbatim (5 cases)
    builder=lambda: GrafanaConnector(token="glc_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="http://localhost:3000/api/health",
                    response=httpx.Response(200, json={"commit": "abc123"}),
                ),
            ),
            expect_ok=True,
            detail_equals="Grafana API healthy",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="http://localhost:3000/api/health",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid", "token"),
        ),
        _HealthScenario(
            case="forbidden",
            routes=(
                _HealthRoute(
                    url="http://localhost:3000/api/health",
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="http://localhost:3000/api/health",
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
        _HealthScenario(
            case="other_status",
            routes=(
                _HealthRoute(
                    url="http://localhost:3000/api/health",
                    response=httpx.Response(503, text="Service Unavailable"),
                ),
            ),
            expect_ok=False,
            detail_contains=("503",),
        ),
    ),
)

_AZURE_KEY_VAULT = _HealthSpec(
    connector="azure_key_vault",  # test_azure_key_vault.py — copied verbatim (4 cases)
    builder=lambda: AzureKeyVaultConnector(token="az_kv_test_token", vault_url="https://myvault.vault.azure.net"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://myvault.vault.azure.net/secrets",
                    params={"api-version": "7.4", "maxresults": 1},
                    response=httpx.Response(200, json={"value": []}),
                ),
            ),
            expect_ok=True,
            detail_equals="Azure Key Vault token validated",
        ),
        _HealthScenario(
            case="invalid_token",
            routes=(
                _HealthRoute(
                    url="https://myvault.vault.azure.net/secrets",
                    params={"api-version": "7.4", "maxresults": 1},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Invalid",),
        ),
        _HealthScenario(
            case="other_http_error",
            routes=(
                _HealthRoute(
                    url="https://myvault.vault.azure.net/secrets",
                    params={"api-version": "7.4", "maxresults": 1},
                    response=httpx.Response(403, text="Forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("HTTP 403",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://myvault.vault.azure.net/secrets",
                    params={"api-version": "7.4", "maxresults": 1},
                    error=httpx.ConnectError("connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("connection refused",),
        ),
    ),
)

_BITBUCKET = _HealthSpec(
    connector="bitbucket",  # test_bitbucket.py — copied verbatim (3 cases)
    builder=lambda: BitbucketConnector(token="bitbucket_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://api.bitbucket.org/2.0/user",
                    response=httpx.Response(200, json={"username": "myuser", "display_name": "My User"}),
                ),
            ),
            expect_ok=True,
            detail_equals="myuser",
        ),
        _HealthScenario(
            case="fail",
            routes=(
                _HealthRoute(
                    url="https://api.bitbucket.org/2.0/user",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("401",),
        ),
        _HealthScenario(
            # corrupt_body_no_crash: a non-dict user body must degrade to
            # success with an empty username, never AttributeError.
            case="corrupt_body",
            routes=(
                _HealthRoute(
                    url="https://api.bitbucket.org/2.0/user",
                    response=httpx.Response(200, json=["garbage"]),
                ),
            ),
            expect_ok=True,
            detail_equals="",
        ),
    ),
)

_AZURE_REPOS = _HealthSpec(
    connector="azure_repos",  # test_azure_repos.py — copied verbatim (3 cases)
    builder=lambda: AzureReposConnector(token="azure_test_token", organization="myorg"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://app.vssps.visualstudio.com/_apis/profile/profiles/me",
                    params={"api-version": "7.0"},
                    response=httpx.Response(200, json={"displayName": "Duncan Tait"}),
                ),
            ),
            expect_ok=True,
            detail_equals="Duncan Tait",
        ),
        _HealthScenario(
            case="fail",
            routes=(
                _HealthRoute(
                    url="https://app.vssps.visualstudio.com/_apis/profile/profiles/me",
                    params={"api-version": "7.0"},
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("401",),
        ),
        _HealthScenario(
            # corrupt_body_no_crash: a non-dict profile body must degrade to
            # success with an empty display name.
            case="corrupt_body",
            routes=(
                _HealthRoute(
                    url="https://app.vssps.visualstudio.com/_apis/profile/profiles/me",
                    params={"api-version": "7.0"},
                    response=httpx.Response(200, json=["garbage"]),
                ),
            ),
            expect_ok=True,
            detail_equals="",
        ),
    ),
)

_SHAREPOINT = _HealthSpec(
    connector="sharepoint",  # test_sharepoint.py — copied verbatim (3 cases)
    builder=lambda: SharePointConnector(token="test_sharepoint_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/sites/root",
                    response=httpx.Response(200, json={"displayName": "Contoso Portal"}),
                ),
            ),
            expect_ok=True,
            detail_equals="Contoso Portal",
        ),
        _HealthScenario(
            case="fail",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/sites/root",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("401",),
        ),
        _HealthScenario(
            # corrupt_body_no_crash: a non-dict site body must degrade to
            # success with an empty detail, never AttributeError.
            case="corrupt_body",
            routes=(
                _HealthRoute(
                    url="https://graph.microsoft.com/v1.0/sites/root",
                    response=httpx.Response(200, json=["not-a-site"]),
                ),
            ),
            expect_ok=True,
            detail_equals="",
        ),
    ),
)

_JIRA = _HealthSpec(
    connector="jira",  # test_jira.py — ok/fail copied verbatim (2 cases;
    # token-auth header assertion and self-hosted path stay in test_jira.py)
    builder=lambda: JiraConnector(
        instance="test-domain.atlassian.net",
        creds={"email": "user@example.com", "api_token": "jira_api_token"},
    ),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://test-domain.atlassian.net/rest/api/3/myself",
                    response=httpx.Response(200, json={"displayName": "Alice"}),
                ),
            ),
            expect_ok=True,
            detail_equals="Alice",
        ),
        _HealthScenario(
            case="fail",
            routes=(
                _HealthRoute(
                    url="https://test-domain.atlassian.net/rest/api/3/myself",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("401",),
        ),
    ),
)

_GITLAB = _HealthSpec(
    connector="gitlab",  # test_gitlab.py — base ok/scope/fail/transport copied
    # verbatim (4 cases); token-info non-fatal probes, scope-cache warming,
    # self-hosted probe root and timeout stay in test_gitlab.py
    builder=lambda: GitLabConnector(token="glpat_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://gitlab.com/api/v4/user",
                    response=httpx.Response(200, json={"username": "myuser"}),
                ),
                _HealthRoute(
                    url="https://gitlab.com/api/v4/projects",
                    response=httpx.Response(200, json=[{"id": 1}]),
                ),
                _HealthRoute(
                    url="https://gitlab.com/oauth/token/info",
                    response=httpx.Response(200, json={"scope": ["read_api", "write_repository", "api"]}),
                ),
            ),
            expect_ok=True,
            detail_equals="myuser",
        ),
        _HealthScenario(
            case="missing_scopes",
            routes=(
                _HealthRoute(
                    url="https://gitlab.com/api/v4/user",
                    response=httpx.Response(200, json={"username": "myuser"}),
                ),
                _HealthRoute(
                    url="https://gitlab.com/api/v4/projects",
                    response=httpx.Response(403, text="forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Missing scopes",),
        ),
        _HealthScenario(
            case="fail",
            routes=(
                _HealthRoute(
                    url="https://gitlab.com/api/v4/user",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("401",),
        ),
        _HealthScenario(
            case="network_error",
            routes=(
                _HealthRoute(
                    url="https://gitlab.com/api/v4/user",
                    error=httpx.ConnectError("Connection refused"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Connection refused",),
        ),
    ),
)

_GITEA = _HealthSpec(
    connector="gitea",  # test_gitea.py — copied verbatim (3 cases); the
    # custom-base-url health check stays in test_gitea.py
    builder=lambda: GiteaConnector(token="gitea_test_token"),
    scenarios=(
        _HealthScenario(
            case="ok",
            routes=(
                _HealthRoute(
                    url="https://codeberg.org/api/v1/user",
                    response=httpx.Response(200, json={"login": "myuser"}),
                ),
                _HealthRoute(
                    url="https://codeberg.org/api/v1/repos",
                    response=httpx.Response(200, json=[{"id": 1}]),
                ),
            ),
            expect_ok=True,
            detail_equals="myuser",
        ),
        _HealthScenario(
            case="missing_scopes",
            routes=(
                _HealthRoute(
                    url="https://codeberg.org/api/v1/user",
                    response=httpx.Response(200, json={"login": "myuser"}),
                ),
                _HealthRoute(
                    url="https://codeberg.org/api/v1/repos",
                    response=httpx.Response(403, text="forbidden"),
                ),
            ),
            expect_ok=False,
            detail_contains=("Missing scopes",),
        ),
        _HealthScenario(
            case="fail",
            routes=(
                _HealthRoute(
                    url="https://codeberg.org/api/v1/user",
                    response=httpx.Response(401, text="Unauthorized"),
                ),
            ),
            expect_ok=False,
            detail_contains=("401",),
        ),
    ),
)


def _ci_runner_spec(
    connector: str,
    builder: Callable[[], object],
    url: str,
    ok_body: dict,
    params: dict[str, str] | None = None,
) -> _HealthSpec:
    """The CI-runner contract: ok / Authentication failed / status-code echo.

    All five CI connectors' ``health_check`` bodies are identical, so the
    three cases differ only in the mocked URL, the 200 body, and (for azure
    devops only) the ``api-version`` query param the original mocks matched
    on.
    """
    return _HealthSpec(
        connector=connector,  # test_<connector>.py — copied verbatim (3 cases)
        builder=builder,
        scenarios=(
            _HealthScenario(
                case="ok",
                routes=(
                    _HealthRoute(
                        url=url,
                        params=params,
                        response=httpx.Response(200, json=ok_body),
                    ),
                ),
                expect_ok=True,
            ),
            _HealthScenario(
                case="fail_401",
                routes=(
                    _HealthRoute(
                        url=url,
                        params=params,
                        response=httpx.Response(401, text="Unauthorized"),
                    ),
                ),
                expect_ok=False,
                detail_contains=("Authentication failed",),
            ),
            _HealthScenario(
                case="fail_500",
                routes=(
                    _HealthRoute(
                        url=url,
                        params=params,
                        response=httpx.Response(500, text="Internal Server Error"),
                    ),
                ),
                expect_ok=False,
                detail_contains=("500",),
            ),
        ),
    )


_BUILDKITE = _ci_runner_spec(
    "buildkite",
    lambda: BuildkiteConnector(token="bkt_test"),
    "https://api.buildkite.com/v2/user",
    {"id": "test-user"},
)

_CIRCLECI = _ci_runner_spec(
    "circleci",
    lambda: CircleCIConnector(token="cct_test"),
    "https://circleci.com/api/v2/me",
    {"login": "testuser"},
)

_AZURE_PIPELINES = _ci_runner_spec(
    "azure_pipelines",
    lambda: AzurePipelinesConnector(token="apt_test", organization="myorg", project="myproject"),
    "https://dev.azure.com/myorg/_apis/projects",
    {"value": [{"id": "proj-1"}], "count": 1},
    params={"api-version": "7.0"},
)

_JENKINS = _ci_runner_spec(
    "jenkins",
    lambda: JenkinsConnector(username="admin", token="secret", base_url="http://jenkins.example.com"),
    "http://jenkins.example.com/api/json",
    {"nodeName": "master"},
)

_TEAMCITY = _ci_runner_spec(
    "teamcity",
    lambda: TeamCityConnector(token="secret", base_url="http://teamcity.example.com"),
    "http://teamcity.example.com/app/rest/server",
    {"version": "2024.07"},
)

# --------------------------------------------------------------------------
# Flattened cases + parametrized tests
# --------------------------------------------------------------------------

_HEALTH_SPECS = (
    _DATADOG,
    _SNYK,
    _PYPI,
    _NPM,
    _N8N,
    _DISCORD,
    _MICROSOFT_TEAMS,
    _CODECLIMATE,
    _PAGERDUTY,
    _SENTRY,
    _ONEPASSWORD,
    _OPSGENIE,
    _GRAFANA,
    _AZURE_KEY_VAULT,
    _BITBUCKET,
    _AZURE_REPOS,
    _SHAREPOINT,
    _JIRA,
    _GITLAB,
    _GITEA,
    _BUILDKITE,
    _CIRCLECI,
    _AZURE_PIPELINES,
    _JENKINS,
    _TEAMCITY,
)

_HEALTH_CASES: list[tuple[_HealthSpec, _HealthScenario]] = [
    (spec, scenario) for spec in _HEALTH_SPECS for scenario in spec.scenarios
]

_CI_DOUBLES: list[tuple[str, Callable[[], object]]] = [
    ("buildkite", _BuildkiteTestDouble),
    ("circleci", _CircleCITestDouble),
    ("azure_pipelines", _AzurePipelinesTestDouble),
    ("teamcity", _TeamCityTestDouble),
    ("jenkins", _JenkinsTestDouble),
]


def _parametrized_cases() -> list[tuple[Callable[[], object], _HealthScenario]]:
    """(builder, scenario) pairs for every contract case in the matrix.

    PyPI/npm auth cases (``unauthorized``/``forbidden``) need the token-carrying
    connector, exactly as those files' ``connector_with_token`` fixtures did.
    """
    token_cases = {
        ("pypi", "unauthorized"),
        ("pypi", "forbidden"),
        ("npm", "unauthorized"),
        ("npm", "forbidden"),
    }
    pairs = []
    for spec, scenario in _HEALTH_CASES:
        builder = spec.builder
        if (spec.connector, scenario.case) in token_cases and spec.token_builder is not None:
            builder = spec.token_builder
        pairs.append((builder, scenario))
    return pairs


@pytest.mark.parametrize(
    "_builder_scenario",
    _parametrized_cases(),
    ids=[f"{spec.connector}[{scenario.case}]" for spec, scenario in _HEALTH_CASES],
)
async def test_health_check_contract_matrix(_builder_scenario: tuple[Callable[[], object], _HealthScenario]) -> None:
    """Each matrix case degrades exactly as its original connector test did."""
    builder, scenario = _builder_scenario
    connector = builder()
    with respx.mock:
        for route in scenario.routes:
            mocked = respx.get(route.url, params=route.params)
            if route.error is not None:
                mocked.mock(side_effect=route.error)
            else:
                mocked.mock(return_value=route.response)
        result = await connector.health_check()
        if scenario.expect_ok:
            assert result.ok is True
        else:
            assert result.ok is False
        for fragment in scenario.detail_contains:
            assert fragment in result.detail
        if scenario.detail_equals is not None:
            assert result.detail == scenario.detail_equals


@pytest.mark.parametrize(
    "double_builder",
    [builder for _, builder in _CI_DOUBLES],
    ids=[name for name, _ in _CI_DOUBLES],
)
async def test_ci_test_double_health_check_ok(double_builder: Callable[[], object]) -> None:
    """The CI test doubles must answer an out-of-the-box health check ok."""
    result = await double_builder().health_check()
    assert result.ok is True
