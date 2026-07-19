"""Hypothesis property-based fuzz tests for connector response parsing.

Tests that every connector's ``query()`` method handles arbitrary/malformed
JSON responses gracefully — returning a ``ConnectorResult`` with a ``records``
list and never raising a non-API exception (non ``httpx.HTTPStatusError``,
non ``ValueError``, non ``KeyError``).
"""

from typing import Any

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from modulo.connectors.base import ConnectorQuery, ConnectorResult
from modulo.connectors.github import GitHubConnector
from modulo.connectors.jira import JiraConnector
from modulo.connectors.linear import LinearConnector
from modulo.connectors.notion import NotionConnector
from modulo.connectors.slack import SlackConnector

# ── Connector fixtures ───────────────────────────────────────────────────


@pytest.fixture()
def gh_connector():
    return GitHubConnector(token="ghp_test_fuzz")


@pytest.fixture()
def linear_connector():
    return LinearConnector(api_key="lin-api-key-fuzz")


@pytest.fixture()
def notion_connector():
    return NotionConnector(token="ntn_token_fuzz")


@pytest.fixture()
def slack_connector():
    return SlackConnector(bot_token="xoxb-fuzz-test")


@pytest.fixture()
def jira_connector():
    return JiraConnector(
        instance="fuzz-test.atlassian.net",
        creds={"email": "test@example.com", "api_token": "fuzz-token"},
    )


# ── JSON generation strategies ───────────────────────────────────────────

#: Sentinel used to signal "draw any type" inside *scalar* below.
_ANY = "___any___"


def _scalar() -> st.SearchStrategy:
    """Lowest-level JSON leaf values (no recursion)."""
    return st.none() | st.booleans() | st.integers(min_value=-(10**9), max_value=10**9) | st.text(max_size=200)


def _json_val(max_depth: int = 3) -> st.SearchStrategy[Any]:
    """Recursive JSON value strategy."""
    if max_depth <= 0:
        return _scalar()
    scalar = _scalar()
    obj = st.dictionaries(st.text(min_size=1, max_size=20), _json_val(max_depth - 1), max_size=5)
    arr = st.lists(_json_val(max_depth - 1), max_size=5)
    return scalar | obj | arr


@st.composite
def json_obj(draw):
    """Arbitrary JSON object (flatish, ~3 levels deep)."""
    return draw(
        st.dictionaries(
            st.text(min_size=1, max_size=20),
            _json_val(max_depth=2),
            min_size=0,
            max_size=10,
        )
    )


@st.composite
def json_obj_list(draw):
    """List of arbitrary JSON objects."""
    n = draw(st.integers(min_value=0, max_value=10))
    return [draw(json_obj()) for _ in range(n)]


@st.composite
def graphql_response(draw):
    """GraphQL envelope with optional ``data`` / ``errors``."""
    has_data = draw(st.booleans())
    has_errors = draw(st.booleans())
    resp: dict[str, Any] = {}
    if has_data:
        resp["data"] = draw(json_obj())
    if has_errors:
        resp["errors"] = draw(
            st.lists(
                st.dictionaries(st.text(max_size=20), _json_val(1), min_size=1, max_size=3),
                min_size=1,
                max_size=3,
            )
        )
    if not has_data and not has_errors:
        resp["data"] = draw(st.none() | json_obj())
    return resp


@st.composite
def slack_api_response(draw):
    """Slack Web API envelope with ``ok`` field."""
    is_ok = draw(st.booleans())
    body: dict[str, Any] = draw(json_obj())
    body["ok"] = is_ok
    if is_ok:
        body.setdefault("channels", draw(st.lists(json_obj(), max_size=5)))
        body.setdefault("messages", draw(st.lists(json_obj(), max_size=5)))
        body.setdefault("members", draw(st.lists(json_obj(), max_size=5)))
    else:
        body.setdefault("error", draw(st.text(max_size=50)))
    body.setdefault(
        "response_metadata", draw(st.none() | st.dictionaries(st.text(max_size=20), st.text(max_size=50), max_size=3))
    )
    return body


@st.composite
def notion_response(draw):
    """Notion paginated list envelope (``results`` + ``next_cursor``)."""
    body: dict[str, Any] = {"object": "list"}
    has_results = draw(st.booleans())
    body["results"] = draw(json_obj_list()) if has_results else []
    body["next_cursor"] = draw(st.none() | st.text(max_size=50))
    return body


# ── Mutation strategy ────────────────────────────────────────────────────


@st.composite
def mutate_response(draw, valid_response: dict) -> dict:
    """Take a *valid* response dict and produce a malformed variant.

    Mutations are applied top-level only:
    * remove a random key
    * coerce a value to a random different type
    * inject an extra unknown field
    * truncate arrays
    """
    import copy

    result = copy.deepcopy(valid_response)
    keys = list(result.keys())

    mutation = draw(
        st.sampled_from(
            [
                "remove_key",
                "change_type",
                "add_field",
                "truncate_array",
                "identity",
            ]
        )
    )

    match mutation:
        case "remove_key":
            if keys:
                key = draw(st.sampled_from(keys))
                del result[key]

        case "change_type":
            if keys:
                key = draw(st.sampled_from(keys))
                result[key] = draw(_scalar())

        case "add_field":
            result[draw(st.text(min_size=1, max_size=20))] = draw(_scalar())

        case "truncate_array":
            for k in keys:
                if isinstance(result[k], list) and result[k]:
                    keep = draw(st.integers(min_value=0, max_value=max(0, len(result[k]) - 1)))
                    result[k] = result[k][:keep]

    return result


# ── Helper ───────────────────────────────────────────────────────────────

_SAFE_EXCEPTIONS = (httpx.HTTPStatusError, ValueError)
"""Exceptions that connectors may legitimately raise from malformed input."""


async def _assert_safe_query(connector, q: ConnectorQuery) -> None:
    """Call ``connector.query(q)`` and assert it only raises *safe* exceptions."""
    try:
        result = await connector.query(q)
        assert isinstance(result, ConnectorResult)
        assert isinstance(result.records, list)
    except _SAFE_EXCEPTIONS:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# GitHub fuzz tests
# ═══════════════════════════════════════════════════════════════════════════


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_github_repos_fuzz(gh_connector, data):
    with respx.mock:
        respx.get("https://api.github.com/user/repos").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj_list()))
        )
        await _assert_safe_query(gh_connector, ConnectorQuery(resource="repos"))


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_github_pulls_fuzz(gh_connector, data):
    with respx.mock:
        respx.get("https://api.github.com/repos/owner/repo/pulls").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj_list()))
        )
        await _assert_safe_query(
            gh_connector,
            ConnectorQuery(
                resource="pulls",
                filters={"repo": "owner/repo"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_github_file_fuzz(gh_connector, data):
    with respx.mock:
        respx.get("https://api.github.com/repos/owner/repo/contents/README.md").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            gh_connector,
            ConnectorQuery(
                resource="file",
                filters={"repo": "owner/repo", "path": "README.md"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_github_issues_fuzz(gh_connector, data):
    with respx.mock:
        respx.get("https://api.github.com/repos/owner/repo/issues").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj_list()))
        )
        await _assert_safe_query(
            gh_connector,
            ConnectorQuery(
                resource="issues",
                filters={"repo": "owner/repo"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_github_single_issue_fuzz(gh_connector, data):
    with respx.mock:
        respx.get("https://api.github.com/repos/owner/repo/issues/1").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            gh_connector,
            ConnectorQuery(
                resource="issue",
                filters={"repo": "owner/repo", "issue_number": 1},
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Linear fuzz tests
# ═══════════════════════════════════════════════════════════════════════════


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_linear_issue_fuzz(linear_connector, data):
    with respx.mock:
        respx.post("https://api.linear.app/graphql").mock(
            return_value=httpx.Response(200, json=data.draw(graphql_response()))
        )
        await _assert_safe_query(
            linear_connector,
            ConnectorQuery(
                resource="issue",
                filters={"id": "fuzz-id-001"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_linear_search_fuzz(linear_connector, data):
    with respx.mock:
        respx.post("https://api.linear.app/graphql").mock(
            return_value=httpx.Response(200, json=data.draw(graphql_response()))
        )
        await _assert_safe_query(
            linear_connector,
            ConnectorQuery(
                resource="search",
                filters={"query": "fuzz"},
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Notion fuzz tests
# ═══════════════════════════════════════════════════════════════════════════


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_notion_database_fuzz(notion_connector, data):
    with respx.mock:
        respx.get("https://api.notion.com/v1/databases/fuzz-db-id").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            notion_connector,
            ConnectorQuery(
                resource="database",
                filters={"database_id": "fuzz-db-id"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_notion_page_fuzz(notion_connector, data):
    with respx.mock:
        respx.get("https://api.notion.com/v1/pages/fuzz-page-id").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            notion_connector,
            ConnectorQuery(
                resource="page",
                filters={"page_id": "fuzz-page-id"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_notion_databases_search_fuzz(notion_connector, data):
    with respx.mock:
        respx.post("https://api.notion.com/v1/search").mock(
            return_value=httpx.Response(200, json=data.draw(notion_response()))
        )
        await _assert_safe_query(notion_connector, ConnectorQuery(resource="databases"))


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_notion_blocks_fuzz(notion_connector, data):
    with respx.mock:
        respx.get("https://api.notion.com/v1/blocks/fuzz-block-id/children").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            notion_connector,
            ConnectorQuery(
                resource="blocks",
                filters={"block_id": "fuzz-block-id"},
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Slack fuzz tests
# ═══════════════════════════════════════════════════════════════════════════


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_slack_channels_fuzz(slack_connector, data):
    with respx.mock:
        respx.get("https://slack.com/api/conversations.list").mock(
            return_value=httpx.Response(200, json=data.draw(slack_api_response()))
        )
        await _assert_safe_query(slack_connector, ConnectorQuery(resource="channels"))


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_slack_messages_fuzz(slack_connector, data):
    with respx.mock:
        respx.get("https://slack.com/api/conversations.history").mock(
            return_value=httpx.Response(200, json=data.draw(slack_api_response()))
        )
        await _assert_safe_query(
            slack_connector,
            ConnectorQuery(
                resource="messages",
                filters={"channel": "C123456"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_slack_users_fuzz(slack_connector, data):
    with respx.mock:
        respx.get("https://slack.com/api/users.list").mock(
            return_value=httpx.Response(200, json=data.draw(slack_api_response()))
        )
        await _assert_safe_query(slack_connector, ConnectorQuery(resource="users"))


# ═══════════════════════════════════════════════════════════════════════════
# Jira fuzz tests
# ═══════════════════════════════════════════════════════════════════════════


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_jira_issue_fuzz(jira_connector, data):
    with respx.mock:
        respx.get("https://fuzz-test.atlassian.net/rest/api/3/issue/FUZZ-1").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            jira_connector,
            ConnectorQuery(
                resource="issue",
                filters={"issue_key": "FUZZ-1"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_jira_search_fuzz(jira_connector, data):
    with respx.mock:
        respx.post("https://fuzz-test.atlassian.net/rest/api/3/search").mock(
            return_value=httpx.Response(200, json=data.draw(json_obj()))
        )
        await _assert_safe_query(
            jira_connector,
            ConnectorQuery(
                resource="search",
                filters={"jql": "project = FUZZ"},
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Mutation-based fuzz tests — start with valid templates and apply mutations
# ═══════════════════════════════════════════════════════════════════════════

_GITHUB_ISSUE_TPL = {"number": 1, "title": "Bug", "state": "open", "body": "desc", "labels": [], "assignee": None}
_LINEAR_ISSUE_TPL = {
    "id": "abc-123",
    "title": "Issue",
    "description": "desc",
    "priority": 2,
    "state": {"id": "st1", "name": "Todo"},
    "assignee": None,
}
_NOTION_PAGE_TPL = {
    "object": "page",
    "id": "page-1",
    "properties": {"title": {"id": "title", "type": "title", "title": [{"plain_text": "Hello"}]}},
    "archived": False,
}
_SLACK_MESSAGE_TPL = {"type": "message", "text": "Hello", "user": "U001", "ts": "1234567890.001"}
_JIRA_ISSUE_TPL = {
    "id": "10000",
    "key": "FUZZ-1",
    "fields": {"summary": "Issue", "issuetype": {"name": "Task"}, "status": {"name": "Open"}},
}


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_github_single_issue_mutated_fuzz(gh_connector, data):
    with respx.mock:
        mutated = data.draw(mutate_response(_GITHUB_ISSUE_TPL))
        respx.get("https://api.github.com/repos/owner/repo/issues/1").mock(
            return_value=httpx.Response(200, json=mutated)
        )
        await _assert_safe_query(
            gh_connector,
            ConnectorQuery(
                resource="issue",
                filters={"repo": "owner/repo", "issue_number": 1},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_linear_single_issue_mutated_fuzz(linear_connector, data):
    """Linear issue endpoint fed mutated valid responses."""
    with respx.mock:
        mutated = data.draw(mutate_response(_LINEAR_ISSUE_TPL))
        # Wrap in GraphQL envelope like Linear's API returns
        gql_payload = {"data": {"issue": mutated}}
        respx.post("https://api.linear.app/graphql").mock(return_value=httpx.Response(200, json=gql_payload))
        await _assert_safe_query(
            linear_connector,
            ConnectorQuery(
                resource="issue",
                filters={"id": "abc-123"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_notion_page_mutated_fuzz(notion_connector, data):
    with respx.mock:
        mutated = data.draw(mutate_response(_NOTION_PAGE_TPL))
        respx.get("https://api.notion.com/v1/pages/page-1").mock(return_value=httpx.Response(200, json=mutated))
        await _assert_safe_query(
            notion_connector,
            ConnectorQuery(
                resource="page",
                filters={"page_id": "page-1"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_slack_message_mutated_fuzz(slack_connector, data):
    with respx.mock:
        mutated = data.draw(mutate_response(_SLACK_MESSAGE_TPL))
        slack_body = {"ok": True, "messages": [mutated]}
        respx.get("https://slack.com/api/conversations.history").mock(return_value=httpx.Response(200, json=slack_body))
        await _assert_safe_query(
            slack_connector,
            ConnectorQuery(
                resource="messages",
                filters={"channel": "C123456"},
            ),
        )


@settings(
    max_examples=5, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture], deadline=5000
)
@given(data=st.data())
async def test_jira_issue_mutated_fuzz(jira_connector, data):
    with respx.mock:
        mutated = data.draw(mutate_response(_JIRA_ISSUE_TPL))
        respx.get("https://fuzz-test.atlassian.net/rest/api/3/issue/FUZZ-1").mock(
            return_value=httpx.Response(200, json=mutated)
        )
        await _assert_safe_query(
            jira_connector,
            ConnectorQuery(
                resource="issue",
                filters={"issue_key": "FUZZ-1"},
            ),
        )
