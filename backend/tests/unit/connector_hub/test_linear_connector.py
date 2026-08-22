"""Unit tests for LinearConnector — GraphQL endpoint mocked via httpx + respx."""

import json

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.linear import LinearConnector

TOKEN = "lin_test_api_key"
_URL = "https://api.linear.app/graphql"

_ISSUE = {
    "id": "c1a2b3c4-0000-0000-0000-000000000001",
    "identifier": "ENG-123",
    "title": "Flaky deploy",
    "description": "The deploy flakes on staging.",
    "state": {"name": "In Progress"},
    "assignee": {"name": "Dana"},
    "url": "https://linear.app/team/issue/ENG-123",
    "updatedAt": "2026-08-20T10:00:00.000Z",
}

_STATES = {
    "issue": {
        "team": {
            "states": {
                "nodes": [
                    {"id": "s1", "name": "Todo", "type": "unstarted"},
                    {"id": "s2", "name": "In Progress", "type": "started"},
                    {"id": "s3", "name": "Done", "type": "completed"},
                ]
            }
        }
    }
}


def _issue_response():
    return httpx.Response(200, json={"data": {"issue": _ISSUE}})


def _states_response():
    return httpx.Response(200, json={"data": _STATES})


def _viewer_response():
    return httpx.Response(200, json={"data": {"viewer": {"id": "u1", "name": "Modulo Bot"}}})


def _comment_create_response():
    return httpx.Response(
        200,
        json={"data": {"commentCreate": {"success": True, "comment": {"id": "cm1", "body": "[Modulo] hello"}}}},
    )


def _issue_update_response():
    return httpx.Response(
        200,
        json={"data": {"issueUpdate": {"success": True, "issue": {"id": _ISSUE["id"], "state": {"name": "Done"}}}}},
    )


def _graphql_router(request: httpx.Request) -> httpx.Response:
    """Branch the mock based on which GraphQL operation is being sent."""
    body = request.read().decode("utf-8")
    try:
        import json

        parsed = json.loads(body)
    except Exception:
        return httpx.Response(200, json={"data": {}})
    query = parsed.get("query", "")
    if "commentCreate" in query:
        return _comment_create_response()
    if "issueUpdate" in query:
        return _issue_update_response()
    if "states" in query or "team" in query:
        return _states_response()
    if "viewer" in query:
        return _viewer_response()
    return _issue_response()


@pytest.fixture
def connector():
    return LinearConnector(token=TOKEN)


def test_connector_type(connector):
    assert connector.connector_type == ConnectorType.LINEAR


def test_init_requires_token():
    with pytest.raises(ValueError, match="token"):
        LinearConnector(token="")


# ---------------------------------------------------------------------------
# T1 — resolve
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_by_identifier(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    fact = await connector.resolve("ENG-123")
    assert fact["identifier"] == "ENG-123"
    assert fact["title"] == "Flaky deploy"
    assert fact["status"] == "In Progress"
    assert fact["assignee"] == "Dana"
    assert fact["link"] == _ISSUE["url"]
    assert fact["updated_at"] == _ISSUE["updatedAt"]


@respx.mock
async def test_resolve_by_uuid(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    fact = await connector.resolve("c1a2b3c4-0000-0000-0000-000000000001")
    assert fact["id"] == "c1a2b3c4-0000-0000-0000-000000000001"


@respx.mock
async def test_resolve_not_found(connector):
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"data": {"issue": None}}))
    with pytest.raises(ValueError, match="not found"):
        await connector.resolve("NOPE-1")


# ---------------------------------------------------------------------------
# T2 — read enrichment
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_issue_body(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    body = await connector.get_issue_body("ENG-123")
    assert "flakes" in body


@respx.mock
async def test_get_comments(connector):
    comments_payload = {
        "data": {
            "issue": {
                "comments": {
                    "nodes": [
                        {"id": "c1", "body": "first", "createdAt": "2026-08-20T09:00:00.000Z"},
                        {"id": "c2", "body": "second", "createdAt": "2026-08-20T09:30:00.000Z"},
                    ]
                }
            }
        }
    }
    respx.post(_URL).mock(return_value=httpx.Response(200, json=comments_payload))
    comments = await connector.get_comments("ENG-123")
    assert len(comments) == 2
    assert comments[0]["body"] == "first"


# ---------------------------------------------------------------------------
# T3 — scoped status update
# ---------------------------------------------------------------------------


@respx.mock
async def test_update_status(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    result = await connector.update_status("ENG-123", "Done")
    assert result["status"] == "Done"
    assert result["issue_id"] == _ISSUE["id"]


@respx.mock
async def test_update_status_unknown(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    with pytest.raises(ValueError, match="not found"):
        await connector.update_status("ENG-123", "Shipped")


# ---------------------------------------------------------------------------
# T3 — scoped comment (structured + prefixed)
# ---------------------------------------------------------------------------


@respx.mock
async def test_comment_is_prefixed(connector):
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return _graphql_router(request)

    respx.post(_URL).mock(side_effect=_capture)
    result = await connector.comment("ENG-123", "hello")
    assert result["comment_id"] == "cm1"

    sent = json.loads(captured["body"])
    assert sent["variables"]["body"] == "[Modulo] hello"


@respx.mock
async def test_comment_dedup_no_double_prefix(connector):
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return _graphql_router(request)

    respx.post(_URL).mock(side_effect=_capture)
    result = await connector.comment("ENG-123", "[Modulo] hello")
    assert result["comment_id"] == "cm1"

    sent = json.loads(captured["body"])
    assert sent["variables"]["body"] == "[Modulo] hello"


async def test_comment_empty_issue_ref(connector):
    with pytest.raises(ValueError, match="issue_ref"):
        await connector.comment("", "hello")


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_check_ok(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    result = await connector.health_check()
    assert result.ok is True


@respx.mock
async def test_health_check_fail(connector):
    respx.post(_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    result = await connector.health_check()
    assert result.ok is False
    assert "401" in result.detail


# ---------------------------------------------------------------------------
# query / write routing (hub-facing surface)
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_issue_resource(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    result = await connector.query(ConnectorQuery(resource="issue", filters={"issue_ref": "ENG-123"}))
    assert result.total == 1
    assert result.records[0]["identifier"] == "ENG-123"


@respx.mock
async def test_query_issue_resource_missing_ref(connector):
    with pytest.raises(ValueError, match="issue_ref"):
        await connector.query(ConnectorQuery(resource="issue"))


@respx.mock
async def test_query_comments_resource(connector):
    comments_payload = {
        "data": {"issue": {"comments": {"nodes": [{"id": "c1", "body": "x", "createdAt": "2026-08-20T09:00:00.000Z"}]}}}
    }
    respx.post(_URL).mock(return_value=httpx.Response(200, json=comments_payload))
    result = await connector.query(ConnectorQuery(resource="comments", filters={"issue_ref": "ENG-123"}))
    assert result.records[0]["body"] == "x"


@respx.mock
async def test_query_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Linear query"):
        await connector.query(ConnectorQuery(resource="bogus"))


@respx.mock
async def test_write_comment_resource(connector):
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return _graphql_router(request)

    respx.post(_URL).mock(side_effect=_capture)
    result = await connector.write(
        ConnectorPayload(resource="comment", data={"issue_ref": "ENG-123", "body": "looks good"})
    )
    assert result["comment_id"] == "cm1"

    sent = json.loads(captured["body"])
    assert sent["variables"]["body"] == "[Modulo] looks good"


@respx.mock
async def test_write_status_update_resource(connector):
    respx.post(_URL).mock(side_effect=_graphql_router)
    result = await connector.write(
        ConnectorPayload(resource="status_update", data={"issue_ref": "ENG-123", "status": "Done"})
    )
    assert result["status"] == "Done"


@respx.mock
async def test_write_unsupported_resource(connector):
    with pytest.raises(ValueError, match="Unsupported Linear write"):
        await connector.write(ConnectorPayload(resource="bogus", data={}))


# ---------------------------------------------------------------------------
# GraphQL error propagation
# ---------------------------------------------------------------------------


@respx.mock
async def test_graphql_error_raises(connector):
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"errors": [{"message": "Something broke"}]}))
    with pytest.raises(ValueError, match="Something broke"):
        await connector.resolve("ENG-123")
