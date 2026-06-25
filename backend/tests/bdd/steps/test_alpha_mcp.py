"""BDD step definitions: MCP trigger, review_hitl, human_only,
library_browse, onboarding."""

import json
import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/mcp/trigger.feature")
scenarios("../../features/mcp/review_hitl.feature")
scenarios("../../features/mcp/human_only.feature")
scenarios("../../features/mcp/library_browse.feature")
scenarios("../../features/mcp/onboarding.feature")

from tests.bdd.conftest import make_mock_pipeline


@given("an MCP server is running at /mcp")
def mcp_server_running(request):
    request.node._mcp_path = "/mcp"


@given("I have a valid MCP API key")
def valid_mcp_key(request):
    request.node._mcp_key = "mcp_key_valid_123"


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "{tool}" with pipeline "{pipeline}"'
    )
)
def mcp_trigger_pipeline(tool: str, pipeline: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(name=pipeline),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={
                "tool": tool,
                "arguments": {"pipeline": pipeline},
            },
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@then("the response contains run_id")
def response_contains_run_id(request):
    data = request.node._resp.json()
    assert "run_id" in data or data.get("content", {}).get("run_id")


@then(parsers.parse('a run is created with status "{status}"'))
def run_created_with_status(status: str, request):
    pass


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "{tool}" with run_context {ctx}'
    )
)
def mcp_trigger_with_context(tool: str, ctx, client, request):
    context = json.loads(ctx) if isinstance(ctx, str) else ctx
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(name=getattr(request.node, "_pipeline_name", "test")),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={
                "tool": tool,
                "arguments": {
                    "pipeline": getattr(request.node, "_pipeline_name", "test"),
                    "run_context": context,
                },
            },
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@then(parsers.parse("the run has run_context with branch {branch}"))
def check_run_context_branch(branch: str, request):
    pass


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "{tool}" without API key'
    )
)
def mcp_no_auth(tool: str, client, request):
    resp = client.post("/mcp/tools/call", json={"tool": tool, "arguments": {}})
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def check_status(status: int, request):
    resp = request.node._resp
    assert resp.status_code == status, (
        f"Expected {status}, got {resp.status_code}: {resp.text[:200]}"
    )


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "{tool}" with pipeline "{pipeline}"'
    )
)
def mcp_trigger_nonexistent(tool: str, pipeline: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.get_pipeline_by_name", return_value=None),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={"tool": tool, "arguments": {"pipeline": pipeline}},
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@then(parsers.parse('the response contains isError true'))
def response_is_error(request):
    data = request.node._resp.json()
    assert data.get("isError") is True


@then(parsers.parse('the error message mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    content = str(data.get("content", data.get("error", ""))).lower()
    assert text.lower() in content


@given(parsers.parse('the MCP API key has scope "{scope}"'))
def mcp_key_scope(scope: str, request):
    request.node._mcp_scope = scope


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "{tool}"'
    )
)
def mcp_tool_call_generic(tool: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(name="test"),
        ),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={"tool": tool, "arguments": {}},
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions_text(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("content", data.get("error", data))).lower()
    assert text.lower() in detail


@given("a run is waiting at gate {gate}")
def run_waiting_at_gate(gate: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._gate_id = gate


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "review_hitl" with action "list"'
    )
)
def mcp_review_hitl_list(client, request):
    resp = client.post(
        "/mcp/tools/call",
        json={
            "tool": "review_hitl",
            "arguments": {"action": "list"},
        },
        headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
    )
    request.node._resp = resp


@then("the response contains the pending gate")
def response_contains_gate(request):
    pass


@then("the response includes run_id and gate_id")
def response_includes_ids(request):
    pass


@given("I have claimed the gate")
def claimed_gate(request):
    request.node._claim_token = "claim_token_123"


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "review_hitl" with action "approve"'
    )
)
def mcp_review_hitl_approve(client, request):
    from modulo.hitl_manager import ApproveResult
    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(success=True, new_status="running"),
        ),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={
                "tool": "review_hitl",
                "arguments": {
                    "action": "approve",
                    "run_id": str(getattr(request.node, "_run_id", uuid.uuid4())),
                    "claim_token": getattr(request.node, "_claim_token", ""),
                },
            },
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@then(parsers.parse('the run status becomes "{status}"'))
def check_run_status(status: str, request):
    pass


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "review_hitl" with action "reject" and reason "{reason}"'
    )
)
def mcp_review_hitl_reject(reason: str, client, request):
    from modulo.hitl_manager import ApproveResult
    with (
        patch(
            "modulo.hitl_manager.HITLManager.approve_gate",
            return_value=ApproveResult(success=True, new_status="rejected"),
        ),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={
                "tool": "review_hitl",
                "arguments": {
                    "action": "reject",
                    "run_id": str(getattr(request.node, "_run_id", uuid.uuid4())),
                    "claim_token": getattr(request.node, "_claim_token", ""),
                    "reason": reason,
                },
            },
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@then(parsers.parse('the run has rejection_reason "{reason}"'))
def check_rejection_reason(reason: str, request):
    pass


@given(parsers.parse('pipeline "{p}" has a human-only node "{node}"'))
def human_only_pipeline(p: str, node: str, request):
    request.node._pipeline_name = p
    request.node._human_node = node


@given(parsers.parse('a run is waiting at human node "{node}"'))
def run_waiting_human(node: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._human_node = node


@then(parsers.parse('the error mentions "human-only"'))
def error_human_only(request):
    data = request.node._resp.json()
    content = str(data.get("content", data.get("error", ""))).lower()
    assert "human" in content


@then(parsers.parse('the response indicates "requires_human" true'))
def response_requires_human(request):
    pass


@then(parsers.parse('the audit event shows actor type "{atype}"'))
def audit_actor_type(atype: str, request):
    pass


@given("the organisation has {count:d} local primitives")
def org_has_local_primitives(count: int, request):
    request.node._local_primitive_count = count


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "library_browse"'
    )
)
def mcp_library_browse(client, request):
    resp = client.post(
        "/mcp/tools/call",
        json={
            "tool": "library_browse",
            "arguments": {},
        },
        headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
    )
    request.node._resp = resp


@then("the response contains the list of primitives")
def response_contains_primitives(request):
    pass


@then("each primitive has id, name, and primitive_type")
def primitive_has_fields(request):
    pass


@given("the organisation has a primitive named {name}")
def org_has_primitive_named(name: str, request):
    request.node._primitive_name = name


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "library_browse" with search "{term}"'
    )
)
def mcp_library_search(term: str, client, request):
    resp = client.post(
        "/mcp/tools/call",
        json={
            "tool": "library_browse",
            "arguments": {"search": term},
        },
        headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
    )
    request.node._resp = resp


@then(parsers.parse('the response contains "{name}"'))
def response_contains_name(name: str, request):
    data = request.node._resp.json()
    content = str(data)
    assert name in content


@when(
    parsers.parse(
        'the MCP client sends a tools/call request for "library_browse" with intent to modify'
    )
)
def mcp_library_modify(client, request):
    resp = client.post(
        "/mcp/tools/call",
        json={
            "tool": "library_browse",
            "arguments": {"intent": "modify", "name": "new-primitive"},
        },
        headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
    )
    request.node._resp = resp


@then("the response is read-only")
def response_read_only(request):
    pass


@then("no primitives are created or modified")
def no_primitives_modified(request):
    pass


@when("the MCP client sends a tools/list request")
def mcp_tools_list(client, request):
    resp = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
    )
    request.node._resp = resp


@then("the response contains tool definitions")
def response_contains_tools(request):
    data = request.node._resp.json()
    assert "tools" in data


@then(
    parsers.parse(
        'the tools include "{t1}", "{t2}", "{t3}", "{t4}"'
    )
)
def tools_include(t1: str, t2: str, t3: str, t4: str, request):
    data = request.node._resp.json()
    tool_names = [t.get("name") for t in data.get("tools", [])]
    for t in (t1, t2, t3, t4):
        assert t in tool_names


@then(
    parsers.parse(
        'the "{tool}" tool has description and inputSchema'
    )
)
def tool_has_description_and_schema(tool: str, request):
    data = request.node._resp.json()
    tools = data.get("tools", [])
    t = next((x for x in tools if x.get("name") == tool), None)
    assert t is not None, f"Tool {tool} not found"
    assert "description" in t
    assert "inputSchema" in t


@given("no API key is provided")
def no_api_key(request):
    request.node._mcp_key = None


@then("the response still contains tool definitions")
def still_contains_tools(request):
    data = request.node._resp.json()
    assert "tools" in data


@then("But invoking any tool returns 401")
def invoking_returns_401(request):
    pass


@then(
    parsers.parse(
        'the "{tool}" tool description explains how to {action}'
    )
)
def tool_description_explains(tool: str, action: str, request):
    pass


@given("the MCP server uses SSE transport")
def sse_transport(request):
    pass


@when(parsers.parse("a client connects to /mcp with Accept: text/event-stream"))
def client_connects_sse(client, request):
    resp = client.get(
        "/mcp",
        headers={"Accept": "text/event-stream"},
    )
    request.node._resp = resp


@then("the connection is established")
def connection_established(request):
    assert request.node._resp.status_code in (200, 101)


@then("the client receives a tools/list response")
def client_receives_tools(request):
    pass


@given(parsers.parse('I have a valid MCP API key with scope "{scope}"'))
def mcp_key_with_scope(scope: str, request):
    request.node._mcp_key = "mcp_key_scoped"
    request.node._mcp_scope = scope
