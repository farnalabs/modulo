"""Coverage-focused tests for connector batch 16."""

from __future__ import annotations

import asyncio
from typing import Self
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from modulo.connectors import security
from modulo.connectors._rate_bucket import (
    PerDestinationRateLimiter,
    RedisTokenBucket,
    SharedBudgetUnavailableError,
    TokenBucket,
)
from modulo.connectors.base import CIRunStatus, ConnectorPayload, ConnectorQuery
from modulo.connectors.ci_runner.github_actions import GitHubActionsCIRunner
from modulo.connectors.ci_runner.gitlab_ci import GitLabCIRunner
from modulo.connectors.linear import LinearConnector, _authorization_header, _retry_delay
from modulo.connectors.pagerduty import PagerDutyConnector


class _FakeScript:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeRedis:
    def __init__(self, script: _FakeScript) -> None:
        self.script = script

    def register_script(self, _lua: str) -> _FakeScript:
        return self.script


@pytest.mark.parametrize("rate", [0, -1])
def test_token_bucket_rejects_invalid_rate(rate: float) -> None:
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(rate, 1)


def test_token_bucket_rejects_invalid_burst_and_tokens() -> None:
    with pytest.raises(ValueError, match="burst"):
        TokenBucket(1, 0)
    bucket = TokenBucket(1, 1)
    with pytest.raises(ValueError, match="tokens"):
        asyncio.run(bucket.consume(0))


async def test_token_bucket_denies_and_refills() -> None:
    now = [0.0]
    bucket = TokenBucket(2, 1, clock=lambda: now[0])
    assert await bucket.consume() is True
    assert await bucket.consume() is False
    now[0] = 0.5
    assert await bucket.consume() is True


@pytest.mark.parametrize("rate", [0, -1])
def test_redis_bucket_rejects_invalid_rate(rate: float) -> None:
    with pytest.raises(ValueError, match="rate"):
        RedisTokenBucket(_FakeRedis(_FakeScript([])), rate, 1)


def test_redis_bucket_rejects_invalid_burst() -> None:
    with pytest.raises(ValueError, match="burst"):
        RedisTokenBucket(_FakeRedis(_FakeScript([])), 1, 0)


async def test_redis_bucket_validates_tokens_and_corrupt_results() -> None:
    script = _FakeScript([None, -1, 1, 0])
    bucket = RedisTokenBucket(_FakeRedis(script), 1, 2, key_prefix="p:")
    with pytest.raises(ValueError, match="tokens"):
        await bucket.consume("x", 0)
    with pytest.raises(SharedBudgetUnavailableError):
        await bucket.consume("x")
    with pytest.raises(SharedBudgetUnavailableError):
        await bucket.consume("x")
    assert await bucket.consume("x") is True
    assert await bucket.consume("x") is False
    assert script.calls[2]["keys"] == ["p:x"]
    assert len(script.calls[2]["args"]) == 4  # type: ignore[arg-type]


async def test_per_destination_local_bucket_and_saturation(caplog: pytest.LogCaptureFixture) -> None:
    limiter = PerDestinationRateLimiter(rate=0.001, burst=1)
    assert await limiter.consume("api/x") is True
    assert await limiter.consume("api/x") is False
    assert await limiter.consume("api/x") is False
    assert limiter.saturation_count == 2
    assert limiter.saturations["api/x"] == 2
    assert sum(1 for record in caplog.records if record.message == "rest.rate_limit.saturated") == 1


@pytest.mark.parametrize("value", [None, ""])
def test_shared_limiter_requires_tenant(value: str | None) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        PerDestinationRateLimiter(rate=1, burst=1, redis_client=object(), tenant_id=value)


async def test_shared_limiter_retries_then_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    script = _FakeScript([OSError("down"), OSError("still down")])
    limiter = PerDestinationRateLimiter(
        rate=1, burst=1, redis_client=_FakeRedis(script), tenant_id="org", key_prefix="p:"
    )
    with pytest.raises(SharedBudgetUnavailableError, match="unavailable"):
        await limiter.consume("dest")
    assert not limiter.buckets
    assert any(record.message == "rest.rate_limit.degraded" for record in caplog.records)


async def test_shared_limiter_retry_success_and_saturation(caplog: pytest.LogCaptureFixture) -> None:
    script = _FakeScript([OSError("down"), 1, 0, 0])
    limiter = PerDestinationRateLimiter(rate=1, burst=1, redis_client=_FakeRedis(script), tenant_id="org")
    assert await limiter.consume("dest") is True
    assert await limiter.consume("dest") is False
    assert await limiter.consume("dest") is False
    assert limiter.saturation_count == 2
    assert any(record.message == "rest.rate_limit.redis_error" for record in caplog.records)


def test_security_helpers_and_rebuild_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert security.credential_values({"nested": ["long-secret", 3], "short": "x"}) == ("long-secret",)
    assert security.redact_text(123, ["123"]) == "***"
    assert security.basic_auth_wire_secrets("user", "pass")[2].startswith("Basic ")
    request = httpx.Request("GET", "https://example.test/?token=long-secret", headers={"X": "long-secret"})
    assert security._scrub_request(request, ["long-secret"]) is not request
    clean = httpx.Response(200, text="ok", request=httpx.Request("GET", "https://example.test"))
    assert security._scrub_response(clean, ["unused"]) is clean
    monkeypatch.setattr(security, "_rebuild_generic_error", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError()))
    repaired = security.redact_exc(ValueError("long-secret"), ["long-secret"])
    assert isinstance(repaired, security.CredentialRedactionError)


def test_linear_helpers() -> None:
    assert _authorization_header(" lin_api_key ") == "lin_api_key"
    assert _authorization_header("lin_oauth_token") == "Bearer lin_oauth_token"
    assert _retry_delay(100) == 10.0
    with pytest.raises(ValueError, match="token"):
        LinearConnector()
    assert LinearConnector("token").connector_type.value == "linear"


async def test_linear_surface_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = LinearConnector("lin_api_secret")
    connector._graphql = AsyncMock(
        side_effect=[
            {
                "issue": {
                    "id": "uuid",
                    "identifier": "ENG-1",
                    "title": "Bug",
                    "state": {"name": "Todo"},
                    "assignee": None,
                    "url": "u",
                    "updatedAt": "now",
                },
            },
            {"issue": {"description": None}},
            {"issue": {"comments": {"nodes": [{"id": "c", "body": "b", "createdAt": "t"}, "bad"]}}},
            {"issue": {"id": "uuid"}},
            {"issue": {"team": {"states": {"nodes": [{"id": "s", "name": "Done", "type": "completed"}]}}}},
            {"issueUpdate": {"issue": {"id": "uuid", "state": {"name": "Done"}}}},
            {"issue": {"id": "uuid"}},
            {"commentCreate": {"comment": {"id": "c", "body": "hello"}}},
        ]
    )
    assert (await connector.resolve("ENG-1"))["assignee"] is None
    assert not await connector.get_issue_body("ENG-1")
    assert await connector.get_comments("ENG-1") == [{"id": "c", "body": "b", "created_at": "t"}]
    assert await connector.update_status("ENG-1", " done ") == {"issue_id": "uuid", "status": "Done"}
    assert await connector._post_comment("ENG-1", "hello") == {"issue_id": "uuid", "comment_id": "c", "body": "hello"}
    monkeypatch.setattr(
        connector,
        "resolve",
        AsyncMock(return_value={"id": "i", "title": "T", "status": "Todo", "link": "u", "assignee": None}),
    )
    ticket = await connector.get_ticket("ENG-1")
    assert ticket.title == "T"
    with pytest.raises(ValueError, match="status"):
        await connector.update_status("ENG-1", "")


async def test_linear_query_write_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = LinearConnector("token")
    monkeypatch.setattr(connector, "resolve", AsyncMock(return_value={"id": "i"}))
    monkeypatch.setattr(connector, "get_comments", AsyncMock(return_value=[]))
    monkeypatch.setattr(connector, "get_issue_body", AsyncMock(return_value="body"))
    monkeypatch.setattr(connector, "comment", AsyncMock(return_value={"ok": True}))
    for query in [ConnectorQuery("issue"), ConnectorQuery("comments"), ConnectorQuery("issue_body")]:
        with pytest.raises(ValueError, match="requires"):
            await connector.query(query)
    assert (await connector.query(ConnectorQuery("issue", {"ticket_id": "T"}))).total == 1
    assert not (await connector.query(ConnectorQuery("comments", {"issue_ref": "T"}))).records
    assert (await connector.query(ConnectorQuery("issue_body", {"issue_ref": "T"}))).records == [{"body": "body"}]
    with pytest.raises(ValueError, match="Unsupported"):
        await connector.query(ConnectorQuery("nope"))
    with pytest.raises(ValueError, match="body"):
        await connector.write(ConnectorPayload("comment", {"issue_ref": "T"}))
    with pytest.raises(ValueError, match="status"):
        await connector.write(ConnectorPayload("status_update", {"issue_ref": "T"}))
    with pytest.raises(ValueError, match="Unsupported"):
        await connector.write(ConnectorPayload("nope", {}))


@respx.mock
async def test_rate_bucket_constructor_and_local_missing_bucket_branches() -> None:
    with pytest.raises(ValueError, match="rate"):
        PerDestinationRateLimiter(rate=0, burst=1)
    with pytest.raises(ValueError, match="burst"):
        PerDestinationRateLimiter(rate=1, burst=0)
    limiter = PerDestinationRateLimiter(rate=1, burst=1, buckets={})
    assert await limiter.consume("new") is True
    assert limiter.key("new") == "default:new"


async def test_linear_retry_transport_and_graphql_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = LinearConnector("lin_secret")
    monkeypatch.setattr("modulo.connectors.linear.asyncio.sleep", AsyncMock())
    connector._client = lambda: (_ for _ in ()).throw(httpx.ConnectError("lin_secret"))  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="transport error"):
        await connector._request_once({}, 3)
    response = httpx.Response(500, text="server", request=httpx.Request("POST", "https://api.linear.app/graphql"))
    connector._client = lambda: _ResponseClient(response)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="HTTP 500"):
        await connector._request_once({}, 3)
    with pytest.raises(ValueError, match="Linear API error"):
        connector._extract_data({"errors": [{"message": "lin_secret failed"}]})


class _ResponseClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return self.response


async def test_linear_graphql_errors_and_health() -> None:
    connector = LinearConnector("lin_secret")
    with pytest.raises(ValueError, match="Linear API error"):
        connector._extract_data({"errors": "lin_secret failed"})
    connector._graphql = AsyncMock(side_effect=ValueError("lin_secret failed"))
    result = await connector.health_check()
    assert result.ok is False
    assert "lin_secret" not in result.detail
    with pytest.raises(ValueError, match="issue_ref"):
        connector._issue_args(" ")
    connector._graphql = AsyncMock(return_value={"issue": None})
    with pytest.raises(ValueError, match="not found"):
        await connector.resolve("ENG-404")


async def test_linear_status_and_write_success() -> None:
    connector = LinearConnector("token")
    connector._resolve_id = AsyncMock(return_value="uuid")
    connector._fetch_team_states = AsyncMock(return_value=[{"id": "s", "name": "Todo"}])
    connector._graphql = AsyncMock(return_value={"issueUpdate": {"issue": {"id": "uuid", "state": {"name": "Todo"}}}})
    assert await connector.update_status("ENG-1", "TODO") == {"issue_id": "uuid", "status": "Todo"}
    connector.comment = AsyncMock(return_value={"comment_id": "c"})
    assert await connector.write(
        ConnectorPayload("comment", {"ticket_id": "ENG-1", "body": "hello"}),
    ) == {"comment_id": "c"}
    assert await connector.write(ConnectorPayload("status_update", {"ticket_id": "ENG-1", "status": "Todo"})) == {
        "issue_id": "uuid",
        "status": "Todo",
    }
    assert connector._match_state([{"name": "Done"}], "Todo") is None


@respx.mock
async def test_github_parse_duration_and_all_http_branches() -> None:
    runner = GitHubActionsCIRunner("secret")
    assert runner._parse_duration_seconds({"run_started_at": "bad", "updated_at": "bad"}) is None
    assert (
        runner._parse_duration_seconds(
            {"run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:01:00Z"},
        )
        == 60
    )
    respx.get("https://api.github.com/repos/o/r/actions/runs/1/logs").mock(return_value=httpx.Response(202))
    with pytest.raises(ValueError, match="still preparing"):
        await runner.get_run_logs("o/r/1")
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(500, text="secret"))
    result = await runner.health_check()
    assert result.ok is False
    assert "secret" not in result.detail
    with pytest.raises(ValueError, match="Invalid run_id"):
        await runner.get_run_status("/1")


@respx.mock
async def test_github_dispatch_repository_and_transport_errors() -> None:
    runner = GitHubActionsCIRunner("secret")
    respx.post("https://api.github.com/repos/o/actions/workflows/r/dispatches").mock(return_value=httpx.Response(204))
    respx.get("https://api.github.com/repos/o/actions/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": [{"id": 1, "status": "queued"}]}),
    )
    assert (await runner.trigger_run("o/r")).id == "o/1"
    respx.get("https://api.github.com/repos/o/r/actions/runs/2").mock(return_value=httpx.Response(500, text="bad"))
    with pytest.raises(ValueError, match="GitHub API error"):
        await runner.get_run_status("o/r/2")
    with pytest.raises(ValueError, match="Invalid run_id"):
        await runner.get_run_status("o/r/")


@respx.mock
async def test_gitlab_all_status_logs_and_error_branches() -> None:
    runner = GitLabCIRunner("secret")
    assert runner._parse_run({"id": 1, "status": "created", "project_id": 2}).status == CIRunStatus.PENDING
    assert runner._parse_run({"id": 1, "status": "mystery"}).status == CIRunStatus.UNKNOWN
    respx.post("https://gitlab.com/api/v4/projects/1/pipeline").mock(return_value=httpx.Response(500, text="bad"))
    with pytest.raises(ValueError, match="GitLab API error"):
        await runner.trigger_run("1")
    respx.get("https://gitlab.com/api/v4/projects/1/pipelines/2/jobs").mock(
        return_value=httpx.Response(500, text="bad"),
    )
    with pytest.raises(ValueError, match="GitLab API error"):
        await runner.get_run_logs("1/2")
    respx.get("https://gitlab.com/api/v4/projects/1/pipelines/2").mock(return_value=httpx.Response(500, text="bad"))
    with pytest.raises(ValueError, match="GitLab API error"):
        await runner.get_run_status("1/2")
    respx.get("https://gitlab.com/api/v4/projects/1/pipelines").mock(return_value=httpx.Response(200, json=[]))
    assert not await runner.list_runs("1", status=CIRunStatus.QUEUED)


@respx.mock
async def test_pagerduty_all_query_resources() -> None:
    connector = PagerDutyConnector("secret")
    payloads = {
        "teams": {"teams": [{"id": "t"}], "total": 1, "more": False},
        "users": {"users": [{"id": "u"}], "total": 1, "more": False},
        "escalation_policies": {"escalation_policies": [{"id": "e"}], "total": 1, "more": False},
        "schedules": {"schedules": [{"id": "s"}], "total": 1, "more": False},
        "on_calls": {"oncalls": [{"id": "o"}], "total": 1, "more": False},
    }
    for resource, body in payloads.items():
        path = "oncalls" if resource == "on_calls" else resource
        respx.get(f"https://api.pagerduty.com/{path}").mock(return_value=httpx.Response(200, json=body))
        result = await connector.query(ConnectorQuery(resource, limit=1))
        assert len(result.records) == 1
    with pytest.raises(ValueError, match="title"):
        await connector.write(ConnectorPayload("incident", {"service_id": "s"}))
    with pytest.raises(ValueError, match="incident_id"):
        await connector.write(ConnectorPayload("incident_acknowledge", {}))
    with pytest.raises(ValueError, match="incident_id"):
        await connector.write(ConnectorPayload("incident_resolve", {}))
    with pytest.raises(ValueError, match="content"):
        await connector.write(ConnectorPayload("note", {"incident_id": "i"}))


def test_security_request_error_variants() -> None:
    request = httpx.Request("GET", "https://example.test/?token=long-secret")
    exc = httpx.ReadTimeout("timeout", request=request)
    repaired = security.redact_exc(exc, ["long-secret"], scrub_url=True, chain_cause=False)
    assert "long-secret" not in str(repaired.request.url)
    assert repaired.__cause__ is None


@respx.mock
async def test_github_logs_redirects_and_errors() -> None:
    runner = GitHubActionsCIRunner("gh_secret")
    respx.get("https://api.github.com/repos/o/r/actions/runs/1/logs").mock(
        return_value=httpx.Response(202, headers={"location": "https://logs.example.test/a"})
    )
    respx.get("https://logs.example.test/a").mock(return_value=httpx.Response(200, text="a\nb"))
    logs = await runner.get_run_logs("o/r/1", cursor="2")
    assert logs.lines == ["a", "b"]
    assert logs.next_cursor == "4"
    with pytest.raises(ValueError, match="Invalid run_id"):
        await runner.get_run_logs("bad")


@respx.mock
async def test_github_and_gitlab_error_paths() -> None:
    gh = GitHubActionsCIRunner("gh_secret")
    respx.get("https://api.github.com/repos/o/r/actions/runs/1").mock(
        return_value=httpx.Response(500, text="gh_secret"),
    )
    with pytest.raises(ValueError, match="GitHub API error"):
        await gh.get_run_status("o/r/1")
    respx.get("https://api.github.com/repos/o/r/actions/runs").mock(return_value=httpx.Response(500, text="oops"))
    with pytest.raises(ValueError, match="GitHub API error"):
        await gh.list_runs("o/r", status=CIRunStatus.UNKNOWN)
    gl = GitLabCIRunner("gl_secret")
    respx.get("https://gitlab.com/api/v4/projects?per_page=1").mock(return_value=httpx.Response(500, text="oops"))
    result = await gl.health_check()
    assert result.ok is False
    respx.get("https://gitlab.com/api/v4/projects/1/pipelines").mock(return_value=httpx.Response(500, text="oops"))
    with pytest.raises(ValueError, match="GitLab API error"):
        await gl.list_runs("1", status=CIRunStatus.UNKNOWN)
    with pytest.raises(ValueError, match="project_id"):
        await gl.get_run_status("/1")


@respx.mock
async def test_gitlab_logs_skip_bad_jobs_and_trace() -> None:
    gl = GitLabCIRunner("gl_secret")
    respx.get("https://gitlab.com/api/v4/projects/1/pipelines/2/jobs").mock(
        return_value=httpx.Response(200, json=[{}, {"id": 2, "name": "bad"}, {"id": 3, "name": "good"}])
    )
    respx.get("https://gitlab.com/api/v4/projects/1/jobs/2/trace").mock(return_value=httpx.Response(404))
    respx.get("https://gitlab.com/api/v4/projects/1/jobs/3/trace").mock(return_value=httpx.Response(200, text="line"))
    logs = await gl.get_run_logs("1/2", cursor="100")
    assert logs.truncated is True
    assert logs.next_cursor == str(100 + len(logs.lines))


@pytest.mark.parametrize("status", [401, 500])
@respx.mock
async def test_pagerduty_health_error_branches(status: int) -> None:
    connector = PagerDutyConnector("pd_secret")
    respx.get("https://api.pagerduty.com/users").mock(return_value=httpx.Response(status, text="pd_secret"))
    result = await connector.health_check()
    assert result.ok is False
    assert "pd_secret" not in result.detail


@respx.mock
async def test_pagerduty_writes_optional_fields_and_cursor() -> None:
    connector = PagerDutyConnector("pd_secret")
    route = respx.post("https://api.pagerduty.com/incidents").mock(
        return_value=httpx.Response(200, json={"incident": {"id": "i"}}),
    )
    result = await connector.write(
        ConnectorPayload(
            "incident",
            {
                "title": "t",
                "service_id": "s",
                "urgency": "high",
                "body": "details",
                "escalation_policy_id": "e",
                "priority_id": "p",
            },
        ),
    )
    assert result["id"] == "i"
    assert route.calls[0].request is not None
    respx.get("https://api.pagerduty.com/services").mock(
        return_value=httpx.Response(
            200,
            json={"services": [{"id": "s"}], "total": 1, "offset": "bad", "more": True},
        ),
    )
    page = await connector.query(ConnectorQuery("services", cursor="bad"))
    assert page.next_cursor == "1"
    with pytest.raises(ValueError, match="Unsupported"):
        await connector.query(ConnectorQuery("unknown"))
