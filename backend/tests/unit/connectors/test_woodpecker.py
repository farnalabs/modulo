"""Unit tests for the Woodpecker CI runner (FAR-1766).

No network, no Docker: every HTTP exchange is respx-mocked against the
Woodpecker API routes the connector was verified against (repo lookup ->
pipelines -> per-step logs), and the in-module ``_WoodpeckerTestDouble`` covers
the shared CI-runner scaffolding. Integration against a real Woodpecker
instance is deferred to CI (this workstation has no Docker daemon).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx
import pytest
import respx

from modulo.connectors.base import CIRunStatus, ConnectorType
from modulo.connectors.ci_runner.woodpecker import (
    WoodpeckerCIRunner,
    _decode_entry,
    _epoch_iso,
    _resolve_api_base,
    _split_run_id,
    _step_tree,
    _WoodpeckerTestDouble,
)

API = "https://ci.example.com/api"
TOKEN = "wpt_test"  # nosec B105 - test fixture value, not a credential


@pytest.fixture
def runner() -> WoodpeckerCIRunner:
    return WoodpeckerCIRunner(token=TOKEN, base_url="https://ci.example.com")


@pytest.fixture
def double() -> _WoodpeckerTestDouble:
    return _WoodpeckerTestDouble()


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _mock_lookup(repo_id: int = 7, default_branch: str = "main") -> Any:
    """The slug->numeric-id lookup every operation starts with.

    The body mirrors the real ``model.Repo`` shape (``id`` / ``full_name`` /
    ``default_branch``) — a mock that invents a ``branch`` key would encode a
    key the API never sends and let a wrong-key read pass untested.
    """
    return respx.get(f"{API}/repos/lookup/acme/widget").mock(
        return_value=httpx.Response(
            200,
            json={"id": repo_id, "full_name": "acme/widget", "default_branch": default_branch},
        ),
    )


def _pipeline_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 900,
        "number": 42,
        "status": "running",
        "branch": "feature/x",
        "commit": "abc123",
        "author": "duncan",
        "created": 1767225600,
        "updated": 1767225660,
        "started": 1767225610,
        "finished": 1767225700,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Type identity + base URL normalisation
# ---------------------------------------------------------------------------


def test_connector_type(runner: WoodpeckerCIRunner) -> None:
    assert runner.connector_type == ConnectorType.CI_RUNNER


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://ci.example.com", "https://ci.example.com/api"),
        ("https://ci.example.com/", "https://ci.example.com/api"),
        ("https://ci.example.com/api", "https://ci.example.com/api"),
        ("https://ci.example.com/api/", "https://ci.example.com/api"),
        ("  https://ci.example.com  ", "https://ci.example.com/api"),
    ],
)
def test_api_base_normalisation(configured: str, expected: str) -> None:
    """The instance root gains ``/api`` exactly once, however it was pasted."""
    assert _resolve_api_base(configured) == expected


def test_api_base_rejects_blank() -> None:
    with pytest.raises(ValueError, match="base_url is required"):
        _resolve_api_base("   ")


# ---------------------------------------------------------------------------
# health_check (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"login": "duncan"}))
    result = await runner.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_auth_failure(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/user").mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await runner.health_check()
    assert result.ok is False
    assert "Authentication failed" in result.detail


@respx.mock
async def test_health_check_server_error_reports_detail(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/user").mock(return_value=httpx.Response(500, text="boom"))
    result = await runner.health_check()
    assert result.ok is False
    assert "500" in result.detail


# ---------------------------------------------------------------------------
# trigger_run (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_round_trip(runner: WoodpeckerCIRunner) -> None:
    """lookup -> POST /pipelines produces the contract id AND the request body."""
    _mock_lookup(repo_id=7)
    trigger = respx.post(f"{API}/repos/7/pipelines").mock(
        return_value=httpx.Response(200, json=_pipeline_payload(status="pending")),
    )

    run = await runner.trigger_run(
        pipeline_id="acme/widget",
        branch="feature/x",
        variables={"KEY": "value"},
    )

    # the id must be the QUALIFIED 'owner/repo/build_number' form the readers consume
    assert run.id == "acme/widget/42"
    assert run.pipeline_id == "acme/widget"
    assert run.status == CIRunStatus.QUEUED
    assert run.branch == "feature/x"
    assert run.commit_sha == "abc123"
    assert run.triggered_by == "duncan"
    assert run.duration_seconds == 90
    assert run.created_at == "2026-01-01T00:00:00Z"

    request = trigger.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["branch"] == "feature/x"
    assert body["variables"] == {"KEY": "value"}


@respx.mock
async def test_trigger_run_defaults_to_the_repository_branch(runner: WoodpeckerCIRunner) -> None:
    """An empty caller branch uses the repo default from the lookup, not a guess.

    Woodpecker resolves the branch against the forge and fails the trigger when
    it cannot — so the connector never sends an empty/unknown branch while a
    usable default exists.
    """
    _mock_lookup(default_branch="master")
    trigger = respx.post(f"{API}/repos/7/pipelines").mock(
        return_value=httpx.Response(200, json=_pipeline_payload()),
    )

    run = await runner.trigger_run(pipeline_id="acme/widget")

    assert run.id == "acme/widget/42"
    assert json.loads(trigger.calls[0].request.content)["branch"] == "master"


@respx.mock
async def test_trigger_run_204_fails_loud(runner: WoodpeckerCIRunner) -> None:
    """A 204 (no pipeline created) has no id to qualify — never a bare id."""
    _mock_lookup()
    respx.post(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(204))

    with pytest.raises(ValueError, match="refusing to return an unusable run id"):
        await runner.trigger_run(pipeline_id="acme/widget", branch="main")


@respx.mock
async def test_trigger_run_missing_number_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.post(f"{API}/repos/7/pipelines").mock(
        return_value=httpx.Response(200, json={"status": "pending"}),
    )

    with pytest.raises(ValueError, match="did not yield a consumable run id"):
        await runner.trigger_run(pipeline_id="acme/widget", branch="main")


async def test_trigger_run_rejects_empty_pipeline_id(runner: WoodpeckerCIRunner) -> None:
    with pytest.raises(ValueError, match="pipeline_id is required"):
        await runner.trigger_run(pipeline_id="", branch="main")


@pytest.mark.parametrize("bad", ["widget", "acme/widget/extra", "acme/../widget", "../widget"])
async def test_trigger_run_rejects_malformed_pipeline_id(runner: WoodpeckerCIRunner, bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid pipeline_id"):
        await runner.trigger_run(pipeline_id=bad, branch="main")


# ---------------------------------------------------------------------------
# get_run_status (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_round_trip(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(200, json=_pipeline_payload(status="running")),
    )

    run = await runner.get_run_status("acme/widget/42")

    assert run.status == CIRunStatus.IN_PROGRESS
    # the id get_run_status RETURNS must equal the id it consumed
    assert run.id == "acme/widget/42"


@pytest.mark.parametrize(
    ("api_status", "expected"),
    [
        ("success", CIRunStatus.SUCCESS),
        ("failure", CIRunStatus.FAILURE),
        ("error", CIRunStatus.FAILURE),
        ("killed", CIRunStatus.CANCELLED),
        ("canceled", CIRunStatus.CANCELLED),
        ("declined", CIRunStatus.CANCELLED),
        ("blocked", CIRunStatus.PENDING),
        ("created", CIRunStatus.QUEUED),
        ("pending", CIRunStatus.QUEUED),
        ("running", CIRunStatus.IN_PROGRESS),
        ("nonsense-status", CIRunStatus.UNKNOWN),
    ],
    ids=[
        "success",
        "failure",
        "error",
        "killed",
        "canceled",
        "declined",
        "blocked",
        "created",
        "pending",
        "running",
        "unknown",
    ],
)
@respx.mock
async def test_get_run_status_maps_statuses(runner: WoodpeckerCIRunner, api_status: str, expected: CIRunStatus) -> None:
    """The documented Woodpecker status vocabulary maps to CIRunStatus."""
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(200, json=_pipeline_payload(status=api_status)),
    )

    run = await runner.get_run_status("acme/widget/42")
    assert run.status == expected


async def test_get_run_status_rejects_bare_id(runner: WoodpeckerCIRunner) -> None:
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await runner.get_run_status("1234567890")


def test_split_run_id_contract() -> None:
    assert _split_run_id("acme/widget/42") == ("acme/widget", "42")
    for bad in ("1234567890", "acme/widget", "acme/widget/latest", "acme//42", "../etc/1"):
        with pytest.raises(ValueError, match="Invalid run_id"):
            _split_run_id(bad)


# ---------------------------------------------------------------------------
# get_run_logs (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs_aggregates_steps(runner: WoodpeckerCIRunner) -> None:
    """Per-step logs (the verified /logs/{n}/{step_id} route) are aggregated."""
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(
            200,
            json=_pipeline_payload(
                status="success",
                workflows=[
                    {
                        "id": 1,
                        "name": "build-workflow",
                        "children": [
                            {"id": 11, "name": "clone"},
                            {"id": 22, "name": "build"},
                        ],
                    },
                ],
            ),
        ),
    )
    respx.get(f"{API}/repos/7/logs/42/11").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"line": 1, "data": _b64("Cloning into 'widget'"), "type": 0},
                {"line": 2, "data": _b64("1"), "type": 2},
                # metadata + progress entries are deliberate filters
                {"line": 3, "data": _b64("step started"), "type": 3},
                {"line": 4, "data": _b64("-"), "type": 4},
            ],
        ),
    )
    respx.get(f"{API}/repos/7/logs/42/22").mock(
        return_value=httpx.Response(200, json=[{"line": 1, "data": _b64("compiling..."), "type": 0}]),
    )

    logs = await runner.get_run_logs("acme/widget/42")

    assert "--- Step clone ---" in logs.lines
    assert "Cloning into 'widget'" in logs.lines
    assert "--- exit code: 1 ---" in logs.lines
    assert "--- Step build ---" in logs.lines
    assert "compiling..." in logs.lines
    assert "step started" not in logs.lines
    assert logs.truncated is False
    assert logs.next_cursor is None


@respx.mock
async def test_get_run_logs_cursor_pages_from_offset(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(
            200,
            json=_pipeline_payload(
                status="success",
                workflows=[{"id": 1, "children": [{"id": 11, "name": "build"}]}],
            ),
        ),
    )
    respx.get(f"{API}/repos/7/logs/42/11").mock(
        return_value=httpx.Response(
            200,
            json=[{"line": 1, "data": _b64("line one"), "type": 0}, {"line": 2, "data": _b64("line two"), "type": 0}],
        ),
    )

    first = await runner.get_run_logs("acme/widget/42")
    # header + 2 lines + trailing blank
    assert len(first.lines) == 4

    rest = await runner.get_run_logs("acme/widget/42", cursor="3")
    assert rest.lines == [""]
    assert rest.next_cursor == "4"


@respx.mock
async def test_get_run_logs_unreadable_step_marks_truncated(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(
            200,
            json=_pipeline_payload(
                status="success",
                workflows=[{"id": 1, "children": [{"id": 11, "name": "ok"}, {"id": 22, "name": "gone"}]}],
            ),
        ),
    )
    respx.get(f"{API}/repos/7/logs/42/11").mock(
        return_value=httpx.Response(200, json=[{"line": 1, "data": _b64("kept"), "type": 0}]),
    )
    respx.get(f"{API}/repos/7/logs/42/22").mock(return_value=httpx.Response(404, text="gone"))

    logs = await runner.get_run_logs("acme/widget/42")

    assert "kept" in logs.lines
    assert logs.truncated is True


@respx.mock
async def test_get_run_logs_pending_run_without_steps_is_empty(runner: WoodpeckerCIRunner) -> None:
    """A pipeline that has not materialised its workflows yet has no logs yet."""
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(200, json=_pipeline_payload(status="pending")),
    )

    logs = await runner.get_run_logs("acme/widget/42")

    assert not logs.lines
    assert logs.truncated is False


# ---------------------------------------------------------------------------
# list_runs (respx)
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    route = respx.get(f"{API}/repos/7/pipelines").mock(
        return_value=httpx.Response(
            200, json=[_pipeline_payload(number=41, status="success"), _pipeline_payload(number=42, status="failure")]
        ),
    )

    runs = await runner.list_runs(pipeline_id="acme/widget", limit=5)

    assert [run.id for run in runs] == ["acme/widget/41", "acme/widget/42"]
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE
    params = route.calls[0].request.url.params
    assert params["page"] == "1"
    assert params["perPage"] == "5"
    assert "status" not in params


@respx.mock
async def test_list_runs_status_filter_uses_a_verified_value(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    route = respx.get(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(200, json=[]))

    await runner.list_runs(pipeline_id="acme/widget", status=CIRunStatus.SUCCESS)
    assert route.calls[0].request.url.params["status"] == "success"


@respx.mock
async def test_list_runs_corrupt_body_returns_empty_page(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))

    assert not await runner.list_runs(pipeline_id="acme/widget")


async def test_list_runs_rejects_empty_pipeline_id(runner: WoodpeckerCIRunner) -> None:
    with pytest.raises(ValueError, match="pipeline_id is required for list_runs"):
        await runner.list_runs(pipeline_id=None)


# ---------------------------------------------------------------------------
# In-module test double (shared CI-runner scaffolding)
# ---------------------------------------------------------------------------


async def test_double_trigger_run_produces_a_contract_id(double: _WoodpeckerTestDouble) -> None:
    run = await double.trigger_run(pipeline_id="acme/widget", branch="main", variables={"A": "b"})

    assert run.id == "acme/widget/101"
    assert run.status == CIRunStatus.QUEUED
    assert double._triggered[0]["variables"] == {"A": "b"}


async def test_double_list_runs_id_shape(double: _WoodpeckerTestDouble) -> None:
    runs = await double.list_runs(pipeline_id="acme/widget")
    assert [run.id for run in runs] == ["acme/widget/100"]


async def test_double_rejects_empty_pipeline_id(double: _WoodpeckerTestDouble) -> None:
    with pytest.raises(ValueError, match="pipeline_id is required"):
        await double.trigger_run(pipeline_id="")


async def test_double_round_trips_through_the_real_parser(
    double: _WoodpeckerTestDouble,
    runner: WoodpeckerCIRunner,
) -> None:
    """A double's produced id is accepted by the REAL connector's readers.

    The real readers parse the id BEFORE building the HTTP client, so
    replacing the client factory with a raising sentinel proves the id
    round-trips while keeping the test offline.
    """

    def _reached_client() -> Any:
        raise RuntimeError("reached the HTTP client")

    runner._client = _reached_client  # type: ignore[method-assign]

    triggered = await double.trigger_run(pipeline_id="acme/widget")
    listed = await double.list_runs(pipeline_id="acme/widget")
    for run_id in [triggered.id, *[run.id for run in listed]]:
        assert _split_run_id(run_id)
        with pytest.raises(RuntimeError, match="reached the HTTP client"):
            await runner.get_run_status(run_id)
        with pytest.raises(RuntimeError, match="reached the HTTP client"):
            await runner.get_run_logs(run_id)


async def test_double_health_check_is_ok(double: _WoodpeckerTestDouble) -> None:
    result = await double.health_check()
    assert result.ok is True


# ---------------------------------------------------------------------------
# Payload parsers: malformed input degrades to empty, never crashes
# ---------------------------------------------------------------------------


def test_epoch_iso_handles_absent_and_out_of_range() -> None:
    assert not _epoch_iso(0)
    assert not _epoch_iso("not-a-number")
    # A timestamp outside datetime's representable range degrades to "" rather
    # than raising (OverflowError/OSError/ValueError are all swallowed).
    assert not _epoch_iso(10**18)


def test_step_tree_ignores_malformed_payloads() -> None:
    assert not _step_tree("not-a-dict")
    assert not _step_tree({"workflows": "not-a-list"})
    assert not _step_tree({"workflows": ["not-a-dict"]})
    assert not _step_tree({"workflows": [{"children": "not-a-list"}]})
    # a child that is not an object is skipped
    assert not _step_tree({"workflows": [{"children": ["not-a-dict"]}]})
    # a child with no usable id is skipped (missing id and id <= 0)
    assert not _step_tree({"workflows": [{"children": [{"name": "no-id"}, {"id": 0, "name": "zero"}]}]})
    # valid children are kept; a nameless step falls back to its id
    assert _step_tree({"workflows": [{"children": [{"id": 3}, {"id": 4, "name": "build"}]}]}) == [
        (3, "3"),
        (4, "build"),
    ]


def test_decode_entry_ignores_malformed_entries() -> None:
    assert not _decode_entry("not-a-dict")
    assert not _decode_entry({"type": 0})  # no data
    assert not _decode_entry({"type": 0, "data": ""})
    assert not _decode_entry({"type": 0, "data": 123})
    # invalid base64 is skipped; binascii.Error is a ValueError subclass
    assert not _decode_entry({"type": 0, "data": "A"})


# ---------------------------------------------------------------------------
# Lookup + pipeline read failures
# ---------------------------------------------------------------------------


@respx.mock
async def test_lookup_repo_error_status_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ValueError, match=r"Woodpecker API error \(500\)"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_lookup_repo_non_json_body_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(ValueError, match="non-JSON body resolving repository"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_lookup_repo_non_object_body_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(ValueError, match="non-object body resolving repository"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_repo_id_missing_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(return_value=httpx.Response(200, json={"slug": "acme/widget"}))
    with pytest.raises(ValueError, match="no usable repository id"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_get_pipeline_non_object_body_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(ValueError, match="non-object pipeline body"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_get_pipeline_non_json_body_fails_loud(runner: WoodpeckerCIRunner) -> None:
    """A 200 with a non-JSON body fails loud (not a raw JSONDecodeError)."""
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(ValueError, match="non-JSON pipeline body"):
        await runner.get_run_status("acme/widget/42")


# ---------------------------------------------------------------------------
# Transport / HTTP-status failure paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_transport_error(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/user").mock(side_effect=httpx.ConnectError("no route"))
    result = await runner.health_check()
    assert result.ok is False
    assert "HTTP error" in result.detail


@respx.mock
async def test_trigger_run_non_object_body_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.post(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(ValueError, match="non-object pipeline body"):
        await runner.trigger_run(pipeline_id="acme/widget", branch="main")


@respx.mock
async def test_trigger_run_http_status_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.post(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ValueError, match=r"Woodpecker API error \(500\)"):
        await runner.trigger_run(pipeline_id="acme/widget", branch="main")


@respx.mock
async def test_trigger_run_transport_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(ValueError, match="connection error"):
        await runner.trigger_run(pipeline_id="acme/widget", branch="main")


@respx.mock
async def test_get_run_status_http_status_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ValueError, match=r"Woodpecker API error \(500\)"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_get_run_status_transport_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(ValueError, match="connection error"):
        await runner.get_run_status("acme/widget/42")


@respx.mock
async def test_get_run_logs_terminal_pipeline_without_steps_logs_a_warning(
    runner: WoodpeckerCIRunner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A terminal pipeline with no workflow tree is an unexpected shape (warned)."""
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(200, json=_pipeline_payload(status="success")),
    )
    with caplog.at_level(logging.WARNING):
        logs = await runner.get_run_logs("acme/widget/42")

    assert not logs.lines
    assert any("carries no workflow/step tree" in record.message for record in caplog.records)


@respx.mock
async def test_get_run_logs_non_json_step_body_marks_truncated(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(
        return_value=httpx.Response(
            200,
            json=_pipeline_payload(
                status="success",
                workflows=[{"id": 1, "children": [{"id": 11, "name": "ok"}, {"id": 22, "name": "bad"}]}],
            ),
        ),
    )
    respx.get(f"{API}/repos/7/logs/42/11").mock(
        return_value=httpx.Response(200, json=[{"line": 1, "data": _b64("kept"), "type": 0}]),
    )
    respx.get(f"{API}/repos/7/logs/42/22").mock(return_value=httpx.Response(200, text="<html>oops</html>"))

    logs = await runner.get_run_logs("acme/widget/42")

    assert "kept" in logs.lines
    assert logs.truncated is True


@respx.mock
async def test_get_run_logs_http_status_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines/42").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ValueError, match=r"Woodpecker API error \(500\)"):
        await runner.get_run_logs("acme/widget/42")


@respx.mock
async def test_get_run_logs_transport_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(ValueError, match="connection error"):
        await runner.get_run_logs("acme/widget/42")


@respx.mock
async def test_list_runs_unknown_status_warns_and_lists_all(
    runner: WoodpeckerCIRunner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _mock_lookup()
    route = respx.get(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(200, json=[]))
    with caplog.at_level(logging.WARNING):
        await runner.list_runs(pipeline_id="acme/widget", status=CIRunStatus.UNKNOWN)

    assert "status" not in route.calls[0].request.url.params
    assert any("UNKNOWN status" in record.message for record in caplog.records)


@respx.mock
async def test_list_runs_unmapped_status_warns_and_lists_all(
    runner: WoodpeckerCIRunner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _mock_lookup()
    route = respx.get(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(200, json=[]))
    with caplog.at_level(logging.WARNING):
        await runner.list_runs(pipeline_id="acme/widget", status=CIRunStatus.TIMED_OUT)

    assert "status" not in route.calls[0].request.url.params
    assert any("No Woodpecker mapping" in record.message for record in caplog.records)


@respx.mock
async def test_list_runs_http_status_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    _mock_lookup()
    respx.get(f"{API}/repos/7/pipelines").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(ValueError, match=r"Woodpecker API error \(500\)"):
        await runner.list_runs(pipeline_id="acme/widget")


@respx.mock
async def test_list_runs_transport_error_fails_loud(runner: WoodpeckerCIRunner) -> None:
    respx.get(f"{API}/repos/lookup/acme/widget").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(ValueError, match="connection error"):
        await runner.list_runs(pipeline_id="acme/widget")


# ---------------------------------------------------------------------------
# Test double: the HTTP-free paths
# ---------------------------------------------------------------------------


def test_double_client_is_unavailable(double: _WoodpeckerTestDouble) -> None:
    with pytest.raises(RuntimeError, match="Test double has no HTTP client"):
        double._client()


async def test_double_status_and_logs_are_self_contained(double: _WoodpeckerTestDouble) -> None:
    double._status = CIRunStatus.SUCCESS
    status = await double.get_run_status("acme/widget/100")
    assert status.id == "acme/widget/100"
    assert status.pipeline_id == "acme/widget"
    assert status.status == CIRunStatus.SUCCESS

    logs = await double.get_run_logs("acme/widget/100")
    assert logs.run_id == "acme/widget/100"
