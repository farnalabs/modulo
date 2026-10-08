"""Run-id contract round-trip for the in-module CI test doubles (FAR-1568).

Every real CI connector produces a ``CIRun.id`` its OWN
``get_run_status``/``get_run_logs`` consumes (the run-id contract on
``CIRunnerBase``, FAR-1141). The in-module ``_*TestDouble`` classes unit tests
drive must emit the SAME shape: a test that only ever round-trips an id through
the double itself cannot see a contract break, so a bare id would sail through
the suite while the real parser rejects it in production.

These tests feed each double's produced id to the **real** connector's
``get_run_status``/``get_run_logs``. The real client factory is replaced with a
sentinel that raises ``_ReachedHttpClientError``:

* ``_ReachedHttpClientError`` -> the real parser ACCEPTED the id and got as far as
  the HTTP client, i.e. the id round-trips;
* ``ValueError`` -> the real parser rejected the id (the pre-fix defect).

The companion test asserts a bare id IS rejected, proving the round-trip check
is discriminating rather than vacuous.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from modulo.connectors.azure_pipelines import AzurePipelinesConnector, _AzurePipelinesTestDouble
from modulo.connectors.buildkite import BuildkiteConnector, _BuildkiteTestDouble
from modulo.connectors.ci_runner.github_actions import GitHubActionsCIRunner, _GitHubActionsTestDouble
from modulo.connectors.ci_runner.gitlab_ci import GitLabCIRunner, _GitLabCITestDouble
from modulo.connectors.circleci import _CircleCITestDouble
from modulo.connectors.jenkins import JenkinsConnector, _JenkinsTestDouble
from modulo.connectors.teamcity import _TeamCityTestDouble

#: Connectors whose contract requires a QUALIFIED id (>=1 slash). CircleCI (the
#: pipeline UUID) and TeamCity (the build id) are bare by contract - see
#: ``test_bare_by_contract_ids`` below.
_QUALIFIED_CASES = [
    pytest.param(
        _GitHubActionsTestDouble,
        lambda: GitHubActionsCIRunner(token="ghp_test"),
        "owner/repo/ci.yml",
        id="github_actions",
    ),
    pytest.param(
        _GitLabCITestDouble,
        lambda: GitLabCIRunner(token="glpat_test"),
        "12345",
        id="gitlab_ci",
    ),
    pytest.param(
        _BuildkiteTestDouble,
        lambda: BuildkiteConnector(token="bkt_test"),
        "my-org/my-pipeline",
        id="buildkite",
    ),
    pytest.param(
        _AzurePipelinesTestDouble,
        lambda: AzurePipelinesConnector(token="apt_test", organization="test-org"),
        "1",
        id="azure_pipelines",
    ),
    pytest.param(
        _JenkinsTestDouble,
        lambda: JenkinsConnector(username="test", token="test"),
        "my-job",
        id="jenkins",
    ),
]

#: The five real connectors, for the discriminating-power check.
_REAL_CASES = [
    pytest.param(real, id=name)
    for name, real in [
        ("github_actions", lambda: GitHubActionsCIRunner(token="ghp_test")),
        ("gitlab_ci", lambda: GitLabCIRunner(token="glpat_test")),
        ("buildkite", lambda: BuildkiteConnector(token="bkt_test")),
        ("azure_pipelines", lambda: AzurePipelinesConnector(token="apt_test", organization="test-org")),
        ("jenkins", lambda: JenkinsConnector(username="test", token="test")),
    ]
]


class _ReachedHttpClientError(Exception):
    """Raised in place of the real HTTP client: the id PASSED the real parser."""


def _no_http(connector: Any) -> Any:
    """Swap a real connector's client factory for a raising sentinel.

    ``get_run_status``/``get_run_logs`` parse the id BEFORE building the
    client, so reaching the client is proof the id was accepted - and it keeps
    the test offline.
    """

    def _raise() -> Any:
        raise _ReachedHttpClientError

    connector._client = _raise
    return connector


async def _assert_round_trips(connector: Any, run_id: str) -> None:
    """The real parser must accept ``run_id`` for status AND logs."""
    with pytest.raises(_ReachedHttpClientError):
        await connector.get_run_status(run_id)
    with pytest.raises(_ReachedHttpClientError):
        await connector.get_run_logs(run_id)


@pytest.mark.parametrize(("double_cls", "real_factory", "pipeline_id"), _QUALIFIED_CASES)
async def test_trigger_run_id_round_trips_through_the_real_parser(
    double_cls: Callable[[], Any],
    real_factory: Callable[[], Any],
    pipeline_id: str,
) -> None:
    """A double's triggered id must be consumable by the REAL connector."""
    double = double_cls()
    run = await double.trigger_run(pipeline_id=pipeline_id)

    await _assert_round_trips(_no_http(real_factory()), run.id)


@pytest.mark.parametrize(("double_cls", "real_factory", "pipeline_id"), _QUALIFIED_CASES)
async def test_list_runs_ids_round_trip_through_the_real_parser(
    double_cls: Callable[[], Any],
    real_factory: Callable[[], Any],
    pipeline_id: str,
) -> None:
    """A double's listed ids must be consumable by the REAL connector too."""
    double = double_cls()
    runs = await double.list_runs(pipeline_id=pipeline_id)
    assert len(runs) == 1

    real = _no_http(real_factory())
    for listed in runs:
        await _assert_round_trips(real, listed.id)


@pytest.mark.parametrize("real_factory", _REAL_CASES)
async def test_a_bare_id_is_rejected_by_the_real_parser(real_factory: Callable[[], Any]) -> None:
    """Discriminating-power check: the round-trip above must not be vacuous.

    A bare (unqualified) id is exactly what the doubles emitted before
    FAR-1568, and every one of these real parsers rejects it up front.
    """
    real = real_factory()

    with pytest.raises(ValueError, match="Invalid run_id format"):
        await real.get_run_status("1234567890")
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await real.get_run_logs("1234567890")


async def test_bare_by_contract_ids() -> None:
    """CircleCI and TeamCity ids are BARE by contract - qualified is wrong here.

    The run-id contract names the CircleCI pipeline UUID and the TeamCity build
    id verbatim, and both real parsers consume the id as-is. This pins that
    shape so a later "qualify everything" sweep does not silently break them.
    """
    circle = await _CircleCITestDouble().trigger_run(pipeline_id="gh/owner/repo")
    teamcity = await _TeamCityTestDouble().trigger_run(pipeline_id="MyBuild")

    assert "/" not in circle.id
    assert "/" not in teamcity.id
    assert len(circle.id) > 0
    assert len(teamcity.id) > 0
