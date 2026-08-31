"""Unit tests for shared connector credential redaction (FAR-513).

Covers three layers:

1. The shared ``modulo.connectors.security`` helpers — ``credential_values``,
   ``redact_text``, ``redact_exc``, ``CredentialRedactor`` and the ``redacting``
   decorator (value-based redaction mirroring ``RestConnector._redact``).
2. Defense-in-depth: ``sanitize_error_text``'s vendor credential patterns.
3. Per-connector wiring: each migrated vendor connector seeds a
   ``CredentialRedactor`` with its credential values and redacts a 401 /
   transport error message that echoes those credentials before it escapes.

Every connector test mocks the transport with ``respx`` so no real network call
is made, and asserts the credential value never appears in the surfaced error
message (and the ``***`` mask was applied).
"""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.datadog import DatadogConnector
from modulo.connectors.github import GitHubConnector, GitHubError
from modulo.connectors.gitlab import GitLabConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.linear import LinearConnector
from modulo.connectors.n8n import N8NConnector
from modulo.connectors.pagerduty import PagerDutyConnector
from modulo.connectors.security import (
    CredentialRedactor,
    credential_values,
    redact_exc,
    redact_text,
    redacting,
)
from modulo.connectors.sentry import SentryConnector
from modulo.connectors.slack import SlackConnector
from modulo.connectors.sonarqube import SonarQubeConnector
from modulo.connectors.ticket_tracker.trello import TrelloTicketTracker
from modulo.connectors.trello import TrelloConnector
from modulo.core.pipeline_engine.error_codes import sanitize_error_text

# ---------------------------------------------------------------------------
# Shared module helpers
# ---------------------------------------------------------------------------


def test_credential_values_collects_and_sorts_longest_first() -> None:
    creds = {"token": "abc123456", "api_key": "short", "nested": {"secret": "averylongsecretvalue"}}
    values = credential_values(creds)
    assert "abc123456" in values
    assert "short" in values
    assert "averylongsecretvalue" in values
    # Longest first so a value that is a substring of a longer value is fully redacted.
    assert values == tuple(sorted(values, key=len, reverse=True))


def test_credential_values_ignores_short_values_and_empty() -> None:
    assert not credential_values(None)
    assert not credential_values({})
    assert not credential_values({"x": "ab"})


def test_redact_text_strips_every_credential_value() -> None:
    out = redact_text("Bearer abc123456 failed", ["abc123456"])
    assert out == "Bearer *** failed"
    assert "abc123456" not in out


def test_redact_text_masks_each_credential_and_is_noop_without_it() -> None:
    text = "api key = secretvalue123 and token = secretvalue123"
    out = redact_text(text, ["secretvalue123"])
    assert "secretvalue123" not in out
    assert out.count("***") == 2
    assert redact_text(text, ["unused"]) == text


def test_redact_exc_returns_original_when_no_credential() -> None:
    exc = ValueError("ordinary error")
    assert redact_exc(exc, ["secretvalue123"]) is exc


def test_redact_exc_preserves_value_error_type() -> None:
    exc = ValueError("boom secretvalue123")
    repaired = CredentialRedactor(["secretvalue123"]).redact_exc(exc)
    assert isinstance(repaired, ValueError)
    assert "secretvalue123" not in str(repaired)
    assert "***" in str(repaired)


def test_redact_exc_preserves_value_error_subclass() -> None:
    class SubError(ValueError):
        pass

    exc = SubError("boom secretvalue123")
    repaired = CredentialRedactor(["secretvalue123"]).redact_exc(exc)
    assert isinstance(repaired, SubError)
    assert "secretvalue123" not in str(repaired)


def test_redact_exc_preserves_httpx_status_error() -> None:
    request = httpx.Request("GET", "https://api.trello.com/1/boards?key=k&token=secretvalue123")
    response = httpx.Response(401, text="unauthorized", request=request)
    exc = httpx.HTTPStatusError("Client error", request=request, response=response)
    repaired = CredentialRedactor(["secretvalue123"]).redact_exc(exc)
    assert isinstance(repaired, httpx.HTTPStatusError)
    assert repaired.response is response
    assert "secretvalue123" not in str(repaired)


def test_redact_exc_scrubs_request_and_response_query_url() -> None:
    # A Trello-style HTTPStatusError embeds the query-string credentials in the
    # message AND in the attached request/response URL objects. Redacting only
    # the message leaks the credential through ``exc.request.url`` /
    # ``exc.response.request.url`` (the FAR-507 gap reintroduced by the shared
    # generic redactor), so the rebuilt exception must scrub those too.
    url = "https://api.trello.com/1/boards?key=k&token=secretvalue123"
    request = httpx.Request("GET", url)
    response = httpx.Response(500, text="boom", request=request)
    exc = httpx.HTTPStatusError(
        "Client error for url 'https://api.trello.com/1/boards?key=k&token=secretvalue123'",
        request=request,
        response=response,
    )
    repaired = CredentialRedactor(["secretvalue123"]).redact_exc(exc)
    assert "secretvalue123" not in str(repaired)
    assert "secretvalue123" not in str(repaired.request.url)
    assert "secretvalue123" not in str(repaired.response.request.url)
    assert "***" in str(repaired.request.url)


def test_redact_exc_preserves_httpx_request_error() -> None:
    exc = httpx.ConnectError("timeout secretvalue123")
    repaired = CredentialRedactor(["secretvalue123"]).redact_exc(exc)
    assert isinstance(repaired, httpx.ConnectError)
    assert "secretvalue123" not in str(repaired)


def test_redacting_decorator_redacts_escaping_exception() -> None:
    class Obj:
        def __init__(self) -> None:
            self._redactor = CredentialRedactor(["secretvalue123"])

        @redacting
        async def work(self) -> str:
            raise ValueError("boom secretvalue123")

    with pytest.raises(ValueError) as exinfo:
        import asyncio

        asyncio.run(Obj().work())
    assert "secretvalue123" not in str(exinfo.value)
    assert "***" in str(exinfo.value)


def test_redacting_decorator_is_noop_for_clean_errors() -> None:
    class Obj:
        def __init__(self) -> None:
            self._redactor = CredentialRedactor(["secretvalue123"])

        @redacting
        async def work(self) -> str:
            raise ValueError("ordinary error")

    with pytest.raises(ValueError, match="ordinary error") as exinfo:
        import asyncio

        asyncio.run(Obj().work())
    assert str(exinfo.value) == "ordinary error"


# ---------------------------------------------------------------------------
# sanitize_error_text defense-in-depth vendor patterns
# ---------------------------------------------------------------------------


def test_sanitize_error_text_redacts_vendor_formats() -> None:
    samples = [
        "bad token lin_api_abcdefghijklmnopqrstuvwxyz",
        "key: DD-API-KEY: 0123456789abcdef0123456789abcdef",
        "application_key: DD-APPLICATION-KEY = 0123456789abcdef0123456789abcdef",
        "n8n api key: 0123456789abcdef0123456789abcdef01234",
        "pagerduty token u+0123456789abcdef0123456789abcd",
        "slack xapp-1234567890-abcdef",
        "gitlab glpat-abcdefghijklmnop",
        "github ghp_abcdefghijklmnopqrstuvwxyz123456",
    ]
    for text in samples:
        sanitized = sanitize_error_text(text)
        # Every secret token/prefix should be fully replaced by <redacted>.
        assert "<redacted>" in sanitized, f"no redaction for: {text!r} -> {sanitized!r}"


# ---------------------------------------------------------------------------
# Per-connector wiring: a 401/transport error that echoes the credential is redacted
# ---------------------------------------------------------------------------

# Distinctive, >=4-char credential values per connector (never a real secret).
_DATADOG_KEY = "dd_api_key_secret_123456"
_DATADOG_APP = "dd_app_key_secret_123456"
_SENTRY_TOKEN = "sntry_auth_token_1234567890"
_N8N_TOKEN = "n8n_api_key_secret_1234567890"
_PD_TOKEN = "pd_token_secret_1234567890"
_SLACK_TOKEN = "xoxb-secret_token_1234567890"
_GITHUB_TOKEN = "ghp_secret_token_1234567890123456"
_GITLAB_TOKEN = "glpat-secret_token_1234567890"
_LINEAR_TOKEN = "lin_api_secret_token_1234567890"
_JIRA_TOKEN = "jira_secret_token_1234567890"
_TRELLO_KEY = "trello_key_secret_123456"
_TRELLO_TOKEN = "trello_token_secret_123456"
_SONAR_TOKEN = "sonar_bearer_token_1234567809"


async def test_github_redacts_token_from_401_error() -> None:
    connector = GitHubConnector(token=_GITHUB_TOKEN)
    with respx.mock:
        respx.get("https://api.github.com/user/repos").mock(
            return_value=httpx.Response(401, text=f"Bad credentials ({_GITHUB_TOKEN})")
        )
        with pytest.raises(GitHubError) as exinfo:
            await connector.query(ConnectorQuery(resource="repos"))
    assert _GITHUB_TOKEN not in str(exinfo.value)
    assert "***" in str(exinfo.value)


async def test_gitlab_redacts_token_from_401_error() -> None:
    connector = GitLabConnector(token=_GITLAB_TOKEN)
    with respx.mock:
        respx.get("https://gitlab.com/api/v4/projects").mock(
            return_value=httpx.Response(401, text=f"Unauthorized ({_GITLAB_TOKEN})")
        )
        with pytest.raises(ValueError) as exinfo:
            await connector.query(ConnectorQuery(resource="projects"))
    assert _GITLAB_TOKEN not in str(exinfo.value)
    assert "***" in str(exinfo.value)


async def test_gitlab_redacts_token_from_health_check_detail() -> None:
    # Health-check fallback branches surface ``response.text`` (via
    # ``_error_detail``) in the returned HealthResult, which the ``@redacting``
    # decorator does NOT intercept — the detail must be redacted explicitly.
    connector = GitLabConnector(token=_GITLAB_TOKEN)
    with respx.mock:
        respx.get("https://gitlab.com/api/v4/user").mock(return_value=httpx.Response(500, text=f"echo {_GITLAB_TOKEN}"))
        result = await connector.health_check()
    assert result.ok is False
    assert _GITLAB_TOKEN not in result.detail
    assert "***" in result.detail


async def test_slack_redacts_token_from_401_error() -> None:
    connector = SlackConnector(bot_token=_SLACK_TOKEN)
    with respx.mock:
        respx.get("https://slack.com/api/conversations.list").mock(
            return_value=httpx.Response(401, text=f"invalid_auth ({_SLACK_TOKEN})")
        )
        with pytest.raises(ValueError) as exinfo:
            await connector.query(ConnectorQuery(resource="channels"))
    assert _SLACK_TOKEN not in str(exinfo.value)
    assert "***" in str(exinfo.value)


async def test_linear_redacts_token_from_401_error() -> None:
    connector = LinearConnector(token=_LINEAR_TOKEN)
    with respx.mock:
        respx.post("https://api.linear.app/graphql").mock(
            return_value=httpx.Response(401, text=f"Unauthorized ({_LINEAR_TOKEN})")
        )
        with pytest.raises(ValueError) as exinfo:
            await connector.query(ConnectorQuery(resource="issue", filters={"issue_ref": "ENG-1"}))
    assert _LINEAR_TOKEN not in str(exinfo.value)
    assert "***" in str(exinfo.value)


async def test_jira_redacts_token_from_401_error() -> None:
    connector = JiraConnector(instance="your-domain.atlassian.net", creds={"token": _JIRA_TOKEN})
    with respx.mock:
        respx.get("https://your-domain.atlassian.net/rest/api/3/issue/ENG-1").mock(
            return_value=httpx.Response(401, text=f"Unauthorized ({_JIRA_TOKEN})")
        )
        with pytest.raises(ValueError) as exinfo:
            await connector.query(ConnectorQuery(resource="issue", filters={"issue_key": "ENG-1"}))
    assert _JIRA_TOKEN not in str(exinfo.value)
    assert "***" in str(exinfo.value)


async def test_trello_ticket_tracker_redacts_credentials_from_401_error() -> None:
    connector = TrelloTicketTracker(
        config={"board_id": "board123"},
        creds={"api_key": _TRELLO_KEY, "token": _TRELLO_TOKEN},
    )
    with respx.mock:
        respx.get("https://api.trello.com/1/boards/board123/cards").mock(
            return_value=httpx.Response(401, text=f"invalid token ({_TRELLO_TOKEN})")
        )
        with pytest.raises(ValueError) as exinfo:
            await connector.list_tickets()
    assert _TRELLO_TOKEN not in str(exinfo.value)
    assert _TRELLO_KEY not in str(exinfo.value)
    assert "***" in str(exinfo.value)


async def test_trello_connector_redacts_token_from_query_url() -> None:
    # Trello puts key + token in the QUERY STRING, so the HTTPStatusError message
    # (which echoes the request URL) contains them; the boundary redaction strips them.
    connector = TrelloConnector(api_key=_TRELLO_KEY, token=_TRELLO_TOKEN)
    with respx.mock:
        respx.get("https://api.trello.com/1/members/me/boards").mock(
            return_value=httpx.Response(500, text="server error")
        )
        with pytest.raises(httpx.HTTPStatusError) as exinfo:
            await connector.query(ConnectorQuery(resource="boards"))
    assert _TRELLO_TOKEN not in str(exinfo.value)
    assert _TRELLO_KEY not in str(exinfo.value)
    assert "***" in str(exinfo.value)
    assert _TRELLO_TOKEN not in str(exinfo.value.request.url)
    assert _TRELLO_KEY not in str(exinfo.value.request.url)
    assert _TRELLO_TOKEN not in str(exinfo.value.response.request.url)


# header-auth connectors surface response text in the health_check detail
@pytest.mark.parametrize(
    ("builder", "path", "token"),
    [
        (
            lambda: DatadogConnector(_DATADOG_KEY, _DATADOG_APP),
            "https://api.datadoghq.com/api/v1/validate",
            _DATADOG_KEY,
        ),
        (lambda: SentryConnector(token=_SENTRY_TOKEN, organization="org"), "https://sentry.io/api/0/", _SENTRY_TOKEN),
        (lambda: PagerDutyConnector(token=_PD_TOKEN), "https://api.pagerduty.com/users", _PD_TOKEN),
        (
            lambda: N8NConnector(token=_N8N_TOKEN, base_url="https://n8n.example.com"),
            "https://n8n.example.com/rest/workflows",
            _N8N_TOKEN,
        ),
        (
            lambda: SonarQubeConnector(token=_SONAR_TOKEN, base_url="https://sonar.example.com"),
            "https://sonar.example.com/api/system/health",
            _SONAR_TOKEN,
        ),
    ],
)
async def test_header_auth_connectors_redact_token_from_health_check_detail(builder, path, token) -> None:
    connector = builder()
    with respx.mock:
        respx.get(path).mock(return_value=httpx.Response(500, text=f"echo {token}"))
        result = await connector.health_check()
    assert result.ok is False
    assert token not in result.detail
    assert "***" in result.detail
