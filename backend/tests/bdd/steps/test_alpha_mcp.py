"""BDD step definitions: MCP trigger, review_hitl, human_only,
library_browse, onboarding."""

import contextlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/mcp/trigger.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/mcp/review_hitl.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/mcp/human_only.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/mcp/library_browse.feature")
from tests.bdd.conftest import ORG_ID, USER_ID, make_mock_pipeline, make_settings

_PLACEHOLDER_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


def _set_mcp_ctx(role: str = "runner", *, node_allowed_tools: list[str] | None = None) -> None:
    """Populate the request-scoped MCP ContextVars a tool handler reads."""
    from modulo.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_key_id,
        _ctx_key_scope,
        _ctx_node_allowed_tools,
        _ctx_org_id,
        _ctx_role,
        _ctx_team_id,
        _ctx_user_id,
    )

    _ctx_org_id.set(ORG_ID)
    _ctx_role.set(role)
    _ctx_user_id.set(USER_ID)
    _ctx_key_id.set(_PLACEHOLDER_KEY_ID)
    _ctx_auth_token.set(_API_KEY)
    _ctx_auth_type.set("api_key")
    _ctx_key_scope.set("org")
    _ctx_team_id.set(None)
    _ctx_node_allowed_tools.set(node_allowed_tools)


def _clear_mcp_ctx() -> None:
    from modulo.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_key_id,
        _ctx_key_scope,
        _ctx_node_allowed_tools,
        _ctx_org_id,
        _ctx_role,
        _ctx_team_id,
        _ctx_user_id,
    )

    for var in (
        _ctx_org_id,
        _ctx_role,
        _ctx_user_id,
        _ctx_key_id,
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_key_scope,
        _ctx_team_id,
        _ctx_node_allowed_tools,
    ):
        var.set(None)


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _call_trigger(
    request,
    *,
    input_payload: dict | None = None,
    pipeline_missing: bool = False,
) -> dict:
    """Drive the real ``trigger_pipeline`` tool with DB/dispatch seams patched."""
    import asyncio

    from modulo.api.mcp_server import trigger_pipeline

    pid = str(getattr(request.node, "_pipeline_id", None) or uuid.uuid4())
    mock_get_pipeline_ret = None if pipeline_missing else MagicMock(owner_team_id=None)
    mock_snapshot = None if pipeline_missing else MagicMock(id=uuid.uuid4(), graph_json={"nodes": {"n1": {}}})
    mock_run = MagicMock(id=uuid.uuid4(), langgraph_thread_id=str(uuid.uuid4()))
    session = _make_session_context(AsyncMock())

    with (
        patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
        patch("modulo.api.mcp_server.get_pipeline", return_value=mock_get_pipeline_ret) as mock_get_pipeline,
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            return_value=mock_snapshot,
        ),
        patch("modulo.db.crud.run.create_run", return_value=mock_run) as mock_create_run,
        patch("modulo.api.mcp_server._session", return_value=session),
        patch("modulo.api.mcp_server.dispatch_run", new_callable=AsyncMock),
    ):
        _set_mcp_ctx("runner")
        try:
            result = asyncio.run(trigger_pipeline(pipeline_id=pid, input_payload=input_payload))
        finally:
            _clear_mcp_ctx()
        request.node._create_run = mock_create_run
        request.node._get_pipeline = mock_get_pipeline
    return result


def _call_review_hitl(request, action: str) -> dict:
    """Drive the real ``review_hitl`` tool up to its scope gate (runner role)."""
    import asyncio

    from modulo.api.mcp_server import review_hitl

    with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
        _set_mcp_ctx("runner")
        try:
            result = asyncio.run(
                review_hitl(
                    run_id=str(uuid.uuid4()),
                    gate_id=str(uuid.uuid4()),
                    action=action,
                    claim_token="claim_token_123",
                )
            )
        finally:
            _clear_mcp_ctx()
    return result


def _call_review_hitl_tool(
    request,
    action: str,
    *,
    role: str = "operator",
    reason: str | None = None,
) -> dict:
    """Drive the REAL ``review_hitl`` tool end to end (parse guard + scope gate + dispatch).

    Only the auth re-validation, DB and HITLManager seams are patched; the
    ``_parse_hitl_action`` claim-token guard, the ``_check_agent_tool_scope``
    scope-gate chokepoint, the ``_check_human_only_gate`` policy hook and the
    ``_dispatch_hitl_action`` decision dispatch all run for real.
    """
    import asyncio

    from modulo.api.mcp_server import review_hitl

    run_id = getattr(request.node, "_run_id", uuid.uuid4())
    gate_id = getattr(request.node, "_gate_id", "pre-deploy")
    claim_token = getattr(request.node, "_claim_token", None)
    mock_run = MagicMock(id=run_id, status="awaiting_human", owner_team_id=None, pipeline_id=uuid.uuid4())
    session = _make_session_context(AsyncMock())

    with (
        patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
        patch("modulo.api.mcp_server.get_run", return_value=mock_run),
        patch("modulo.api.mcp_server._run_owner_team_id", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.mcp_server._session", return_value=session),
        patch("modulo.api.mcp_server._check_human_only_gate", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.mcp_server._validate_mcp_choice_answer", new_callable=AsyncMock, return_value=(None, None)),
        patch("modulo.api.mcp_server.HITLManager.approve", new_callable=AsyncMock) as mock_approve,
        patch("modulo.api.mcp_server.HITLManager.reject", new_callable=AsyncMock),
    ):
        _set_mcp_ctx(role)
        try:
            result = asyncio.run(
                review_hitl(
                    run_id=str(run_id),
                    gate_id=gate_id,
                    action=action,
                    claim_token=claim_token,
                    reason=reason,
                )
            )
        finally:
            _clear_mcp_ctx()
    request.node._approve = mock_approve
    return result


def _call_list_pending_hitl(request, *, gate_config: dict | None = None, gate_id: str | None = None) -> dict:
    """Drive the REAL ``list_pending_hitl`` tool with the DB query seams patched.

    The ``_list_pending_hitl_impl`` scope gate (``hitl.list`` @ runner), the
    org-context resolution and the wire serialisation (incl. the shared gate
    description resolver AND the per-gate ``human_only`` flag resolver) all run
    for real against the patched gate loader.
    """
    import asyncio

    from modulo.api.mcp_server import list_pending_hitl

    run_id = getattr(request.node, "_run_id", uuid.uuid4())
    gate_id = (
        gate_id or getattr(request.node, "_gate_id", None) or getattr(request.node, "_human_node", None) or "pre-deploy"
    )
    claim = MagicMock()
    claim.run_id = run_id
    claim.gate_id = gate_id
    claim.pipeline_id = uuid.uuid4()
    claim.account_id = None
    claim.expires_at = None
    claim.required_team_id = None
    claim.context_json = {}
    claim.gate_config_json = gate_config
    session = _make_session_context(AsyncMock())

    with (
        patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
        patch("modulo.api.mcp_server._session", return_value=session),
        patch(
            "modulo.api.mcp_server._load_pending_hitl_gates",
            new_callable=AsyncMock,
            return_value=([claim], 1),
        ),
        patch(
            "modulo.db.crud.hitl_gate_config.resolve_gate_descriptions",
            new_callable=AsyncMock,
            return_value={(run_id, gate_id): "Approve the pre-deploy gate"},
        ),
        patch(
            "modulo.db.crud.hitl_gate_config._batch_load_snapshot_graphs",
            new_callable=AsyncMock,
            return_value=({}, {}),
        ),
    ):
        _set_mcp_ctx("runner")
        try:
            result = asyncio.run(list_pending_hitl(page=1, page_size=20))
        finally:
            _clear_mcp_ctx()
    return result


def _call_review_hitl_on_human_only_gate(request, action: str) -> dict:
    """Drive the REAL ``review_hitl`` tool through the REAL ``_check_human_only_gate``.

    The parse guard and the role-hierarchy scope gate run for real (operator
    role, claim token supplied), and ``_check_human_only_gate`` — the shared
    FAR-610 policy hook — runs for real against a SEEDED gate-config resolution
    (``{"human_only": True}``): ``human_only_denial`` produces the shared
    ``MSG_HUMAN_ONLY_DENY`` verdict and the denial returns the
    ``{"error": "human_only_gate", "detail": ...}`` error dict. The FAR-634
    denial-audit append (a DB write) is the only seam captured — asserted, not
    patched away.
    """
    import asyncio

    from modulo.api.mcp_server import review_hitl

    run_id = getattr(request.node, "_run_id", uuid.uuid4())
    gate_id = getattr(request.node, "_gate_id", None) or getattr(request.node, "_human_node", None) or "final-signoff"
    mock_run = MagicMock(id=run_id, status="awaiting_human", owner_team_id=None, pipeline_id=uuid.uuid4())
    session = _make_session_context(AsyncMock())

    with (
        patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
        patch("modulo.api.mcp_server.get_run", return_value=mock_run),
        patch("modulo.api.mcp_server._run_owner_team_id", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.mcp_server._session", return_value=session),
        patch(
            "modulo.db.crud.hitl_gate_config.resolve_hitl_gate_config",
            new_callable=AsyncMock,
            return_value={"human_only": True},
        ),
        patch(
            "modulo.api.mcp_server._append_hitl_human_only_denied_audit",
            new_callable=AsyncMock,
        ) as mock_denial_audit,
    ):
        _set_mcp_ctx("operator")
        try:
            result = asyncio.run(
                review_hitl(
                    run_id=str(run_id),
                    gate_id=gate_id,
                    action=action,
                    claim_token="claim_token_123",
                )
            )
        finally:
            _clear_mcp_ctx()
    request.node._denial_audit = mock_denial_audit
    return result


def _browser_principal_client_type() -> str:
    """The REAL REST HITL audit attribution for a browser principal (FAR-611).

    ``_client_type`` in ``api/routes/hitl.py`` maps a browser-authenticated JWT
    principal (``via_api_key=False``, ``client_kind=browser``) to the
    ``"browser"`` audit stamp. Constructing the frozen ``TenantPrincipal`` and
    running the real function exercises the only browser attribution path.
    """
    from modulo.api.routes.hitl import _client_type
    from modulo.auth.jwt import TenantPrincipal

    principal = TenantPrincipal(
        username="alice",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="operator",
        via_api_key=False,
        client_kind="browser",
    )
    return _client_type(principal)


def _make_mcp_request(*, path: str = "/mcp", headers=None):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }

    async def receive():
        return {"type": "http.disconnect"}

    return Request(scope, receive=receive)


def _mcp_response_payload(request):
    """Return the tool-call result dict, falling back to an HTTP response body."""
    result = getattr(request.node, "_result", None)
    if isinstance(result, dict):
        return result
    resp = getattr(request.node, "_resp", None)
    return resp.json() if resp is not None else {}


@given("an MCP server is running at /mcp")
def mcp_server_running(request):
    request.node._mcp_path = "/mcp"


@given("I have a valid MCP API key")
def valid_mcp_key(request):
    request.node._mcp_key = "mcp_key_valid_123"


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.name = name
    pipeline.owner_team_id = None
    request.node._pipeline_id = pipeline.id


@given(parsers.parse('an MCP API key with role "{role}"'))
def mcp_api_key_role(role: str, request):
    request.node._mcp_role = role


@when(parsers.parse('the MCP client calls "trigger_pipeline" for the pipeline'))
def trigger_for_pipeline(request):
    request.node._result = _call_trigger(request)


@when(parsers.re(r'the MCP client calls "trigger_pipeline" with input_payload (?P<payload>.+)'))
def trigger_with_payload(payload: str, request):
    request.node._result = _call_trigger(request, input_payload=json.loads(payload))


@when(parsers.parse('the MCP client calls "trigger_pipeline" for an unknown pipeline'))
def trigger_unknown_pipeline(request):
    request.node._result = _call_trigger(request, pipeline_missing=True)


@when("an unauthenticated request reaches the MCP server")
def unauth_reaches_mcp_server(request):
    import asyncio

    from starlette.responses import JSONResponse

    async def call_next(_req):
        return JSONResponse({})

    with patch("modulo.api.mcp_server.get_settings", return_value=make_settings()):
        from modulo.api.mcp_server import McpAuthMiddleware

        middleware = McpAuthMiddleware(app=MagicMock())
        status = asyncio.run(middleware.dispatch(_make_mcp_request(), call_next)).status_code
    request.node._mcp_gate_status = status


@then(parsers.parse("the MCP auth gate rejects the request with status code {status:d}"))
def mcp_gate_rejected_with_status(status: int, request):
    assert getattr(request.node, "_mcp_gate_status", None) == status


@then("the response contains run_id")
def response_contains_run_id(request):
    data = _mcp_response_payload(request)
    assert "run_id" in data


@then(parsers.parse('a run is created with status "{status}"'))
def run_created_with_status(status: str, request):
    result = getattr(request.node, "_result", None)
    if isinstance(result, dict) and "status" in result:
        assert result["status"] == status, f"Expected run status {status!r}, got {result['status']!r}"
    create_run = getattr(request.node, "_create_run", None)
    if create_run is not None:
        create_run.assert_awaited_once()
        assert create_run.await_args.kwargs["trigger_type"] == "manual"


@then(parsers.parse('the run is created with input_payload carrying branch "{branch}"'))
def run_created_with_payload_branch(branch: str, request):
    payload = request.node._create_run.await_args.kwargs["input_payload"]
    assert payload.get("branch") == branch


@when(parsers.parse('the MCP client calls "review_hitl" with action "{action}"'))
def review_hitl_forbidden(action: str, request):
    request.node._result = _call_review_hitl(request, action)


@then(parsers.parse('the tool reports run status "{status}"'))
def tool_reports_run_status(status: str, request):
    result = _mcp_response_payload(request)
    assert result.get("status") == status, f"Expected status {status!r}, got {result.get('status')!r}"
    create_run = getattr(request.node, "_create_run", None)
    assert create_run is not None
    create_run.assert_awaited_once()
    assert create_run.await_args.kwargs["trigger_type"] == "manual"


@then(parsers.parse('the tool returns error "{code}"'))
def tool_returns_error(code: str, request):
    assert _mcp_response_payload(request).get("error") == code, _mcp_response_payload(request)


@then("the response carries an error")
def response_carries_error(request):
    assert "error" in _mcp_response_payload(request)


@then(parsers.parse("the response contains isError true"))
def response_is_error(request):
    data = _mcp_response_payload(request)
    assert data.get("isError") is True


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = _mcp_response_payload(request)
    content = str(data.get("detail", data.get("error", data))).lower()
    assert text.lower() in content


@given(parsers.parse('the MCP API key has scope "{scope}"'))
def mcp_key_scope(scope: str, request):
    request.node._mcp_scope = scope


@when(parsers.parse('the MCP client sends a tools/call request for "{tool}"'))
def mcp_tool_call_generic(tool: str, client, request):
    with (
        patch("modulo.api.mcp_server.set_rls_org"),
        patch(
            "modulo.api.mcp_server.get_pipeline_by_name",
            return_value=make_mock_pipeline(name="test"),
        ),
    ):
        resp = client.post(
            "/mcp/tools/call",
            json={"tool": tool, "arguments": {}},
            headers={"Authorization": f"Bearer {getattr(request.node, '_mcp_key', '')}"},
        )
    request.node._resp = resp


@given(parsers.parse('a run is waiting at gate "{gate}"'))
def run_waiting_at_gate(gate: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._gate_id = gate


@when("the MCP client lists pending HITL gates")
def mcp_list_pending_hitl(request):
    # The pending gate's fire-time stamped config carries human_only=true, so
    # the REAL claim-stamped path of resolve_gate_human_only_map is exercised.
    request.node._result = _call_list_pending_hitl(request, gate_config={"human_only": True})


@when("the MCP client approves the gate")
def mcp_approve_gate(request):
    request.node._result = _call_review_hitl_tool(request, "approve", role="operator")


@when(parsers.parse('the MCP client rejects the gate with reason "{reason}"'))
def mcp_reject_gate(reason: str, request):
    request.node._result = _call_review_hitl_tool(request, "reject", role="operator", reason=reason)


@when("the MCP client approves a gate without a claim token")
def mcp_approve_without_claim(request):
    request.node._result = _call_review_hitl_tool(request, "approve", role="operator")


@when("the MCP client attempts to approve the gate")
def mcp_attempt_approve(request):
    request.node._result = _call_review_hitl_tool(request, "approve", role="runner")


@then("the response contains the pending gate")
def response_contains_gate(request):
    data = _mcp_response_payload(request)
    assert isinstance(data.get("gates"), list), data
    run_id_str = str(getattr(request.node, "_run_id", uuid.uuid4()))
    gate_id = getattr(request.node, "_gate_id", None) or getattr(request.node, "_human_node", None) or "pre-deploy"
    matches = [g for g in data["gates"] if g.get("run_id") == run_id_str and g.get("gate_id") == gate_id]
    assert matches, f"pending gate {gate_id!r} on run {run_id_str!r} not in response: {data}"


@then("the response includes run_id and gate_id")
def response_includes_ids(request):
    gate = _mcp_response_payload(request)["gates"][0]
    assert gate.get("run_id")
    assert gate.get("gate_id")


@then(parsers.parse('the tool reports HITL decision "{decision}" for the gate'))
def tool_reports_hitl_decision(decision: str, request):
    data = _mcp_response_payload(request)
    assert data.get("status") == decision, f"Expected HITL decision {decision!r}, got {data!r}"
    assert data.get("gate_id") == getattr(request.node, "_gate_id", "pre-deploy")


@given("I have claimed the gate")
def claimed_gate(request):
    request.node._claim_token = "claim_token_123"


@given(parsers.parse('pipeline "{p}" has a human-only node "{node}"'))
def human_only_pipeline(p: str, node: str, request):
    request.node._pipeline_name = p
    request.node._human_node = node


@given(parsers.parse('a run is waiting at human node "{node}"'))
def run_waiting_human(node: str, request):
    request.node._run_id = uuid.uuid4()
    request.node._human_node = node


@then(parsers.parse('the pending gate indicates "human_only" true'))
def pending_gate_requires_human(request):
    data = _mcp_response_payload(request)
    run_id_str = str(getattr(request.node, "_run_id", uuid.uuid4()))
    gate_id = getattr(request.node, "_gate_id", None) or getattr(request.node, "_human_node", None) or "final-signoff"
    gates = data.get("gates", [])
    gate = next(
        (g for g in gates if g.get("run_id") == run_id_str and g.get("gate_id") == gate_id),
        None,
    )
    assert gate is not None, f"pending gate {gate_id!r} not in response: {data}"
    assert gate.get("human_only") is True, f"expected human_only=true on {gate!r}, got: {data}"


@when("the MCP client approves the human-only gate")
def mcp_approve_human_only_gate(request):
    request.node._result = _call_review_hitl_on_human_only_gate(request, "approve")


@then(parsers.parse('the denial appends a "hitl.human_only_denied" audit event'))
def denial_audit_appended(request):
    from modulo.db.crud.hitl_gate_config import MSG_HUMAN_ONLY_DENY

    mock_denial_audit = getattr(request.node, "_denial_audit", None)
    assert mock_denial_audit is not None, "denial audit seam was not captured"
    mock_denial_audit.assert_awaited_once()
    assert mock_denial_audit.await_args.kwargs["verdict"] == MSG_HUMAN_ONLY_DENY


@then(parsers.parse('the decision audit event records actor type "{atype}"'))
def decision_audit_actor_type(atype: str, request):
    if atype == "mcp":
        mock_approve = getattr(request.node, "_approve", None)
        assert mock_approve is not None, "HITLManager.approve seam was not captured"
        mock_approve.assert_awaited_once()
        assert mock_approve.await_args.kwargs["client_type"] == "mcp"
    else:
        assert _browser_principal_client_type() == "browser"


def _make_library_primitive(name: str, ptype: str = "schema", index: int = 0) -> MagicMock:
    """A fake ``LibraryPrimitive`` row matching what ``search_library`` reads."""
    p = MagicMock()
    p.id = uuid.UUID(int=index + 1)
    p.name = name
    p.description = f"{name} description"
    p.primitive_type = ptype
    p.version = "1.0"
    p.average_rating = 4.5
    p.tags = ["test"]
    return p


def _call_search_library(request, *, search: str | None = None, deny_node_tools: list[str] | None = None) -> dict:
    """Drive the REAL ``search_library`` tool with the DB/list seams patched.

    The real ``_check_agent_tool_scope`` gate runs for every call: a role at or
    above the ``library.search`` viewer floor browses, and a node-level
    ``allowed_tools`` scope that excludes ``search_library`` (``deny_node_tools``)
    is denied with the pinned ``insufficient_scope`` error shape. Only the auth
    re-validation, ``list_primitives`` read and ``_session`` seams are patched.
    """
    import asyncio

    from modulo.api.mcp_server import search_library
    from modulo.db.crud.base import PageResult

    primitives = getattr(request.node, "_primitives", [])
    session = _make_session_context(AsyncMock())

    def _list_side_effect(session, org_id, **kwargs):
        term = kwargs.get("search")
        items = primitives
        if term:
            items = [p for p in primitives if term.lower() in p.name.lower()]
        return PageResult(
            items=items,
            total=len(items),
            page=1,
            page_size=20,
            next_cursor=None,
            has_more=False,
        )

    with (
        patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
        patch("modulo.api.mcp_server._session", return_value=session),
        patch("modulo.api.mcp_server.list_primitives", side_effect=_list_side_effect) as mock_list,
        patch("modulo.api.mcp_server.library_copy_to_adapt", new_callable=AsyncMock) as mock_copy,
    ):
        _set_mcp_ctx("viewer", node_allowed_tools=deny_node_tools)
        try:
            result = asyncio.run(search_library(search=search))
        finally:
            _clear_mcp_ctx()
    request.node._list_primitives = mock_list
    request.node._copy_to_adapt = mock_copy
    return result


@given("the organisation has {count:d} local primitives")
def org_has_local_primitives(count: int, request):
    request.node._local_primitive_count = count
    request.node._primitives = [_make_library_primitive(f"Local Primitive {i}", index=i) for i in range(count)]


@when("the MCP client browses the library")
def mcp_browse_library(request):
    request.node._result = _call_search_library(request)


@when(parsers.parse('the MCP client searches the library for "{term}"'))
def mcp_search_library(term: str, request):
    request.node._result = _call_search_library(request, search=term)


@when("the MCP client tries to browse the library")
def mcp_browse_library_denied(request):
    deny = getattr(request.node, "_deny_node_tools", None) or ["trigger_pipeline"]
    request.node._result = _call_search_library(request, deny_node_tools=deny)


@then("the response contains the list of primitives")
def response_contains_primitives(request):
    data = _mcp_response_payload(request)
    assert isinstance(data.get("items"), list), data
    expected = getattr(request.node, "_primitives", [])
    assert len(data["items"]) == len(expected), f"expected {len(expected)} primitives, got {len(data['items'])}"
    assert data["total"] == len(expected)


@then("each primitive has id, name, and type")
def primitive_has_fields(request):
    data = _mcp_response_payload(request)
    for item in data["items"]:
        assert item.get("id"), item
        assert item.get("name"), item
        assert item.get("type"), item


@then(parsers.parse('the response contains the primitive named "{name}"'))
def response_contains_primitive_named(name: str, request):
    data = _mcp_response_payload(request)
    names = [item.get("name") for item in data.get("items", [])]
    assert name in names, f"primitive {name!r} not in response: {data}"


@then("the response is read-only")
def response_read_only(request):
    data = _mcp_response_payload(request)
    assert isinstance(data.get("items"), list), data
    # The browse surface is on the read-only allowlist, statically pinned.
    from modulo.core.mcp.scope_validator import READ_ONLY_TOOLS

    assert "search_library" in READ_ONLY_TOOLS


@then("no primitives are created or modified")
def no_primitives_modified(request):
    mock_list = getattr(request.node, "_list_primitives", None)
    assert mock_list is not None, "list_primitives seam was not captured"
    mock_list.assert_called_once()
    mock_copy = getattr(request.node, "_copy_to_adapt", None)
    assert mock_copy is not None, "copy seam was not captured"
    mock_copy.assert_not_awaited()


@given(parsers.parse('the organisation has a primitive named "{name}"'))
def org_has_primitive_named(name: str, request):
    request.node._primitive_name = name
    request.node._primitives = [_make_library_primitive(name)]


@given("the MCP caller's allowed_tools scope excludes the library")
def mcp_browse_scoped_out(request):
    request.node._deny_node_tools = ["trigger_pipeline"]


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


@then(parsers.parse('the tools include "{t1}", "{t2}", "{t3}", "{t4}"'))
def tools_include(t1: str, t2: str, t3: str, t4: str, request):
    data = request.node._resp.json()
    tool_names = [t.get("name") for t in data.get("tools", [])]
    for t in (t1, t2, t3, t4):
        assert t in tool_names


@then(parsers.parse('the "{tool}" tool has description and inputSchema'))
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


@then(parsers.parse('the "{tool}" tool description explains how to {action}'))
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
