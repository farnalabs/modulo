"""Step definitions for the Feedback Review Inbox and Eval Proposals BDD scenarios (PRD §8.20).

Wires the scenarios in ``feedback_inbox.feature`` to the real
``/api/v1/feedback/inbox``, ``/api/v1/feedback/proposals``, review,
detect-gap and publish routes (``modulo/api/routes/feedback.py``) through the
shared TestClient + mock-session pattern used by the nearby feedback /
dashboard step modules. The FeedbackManager CRUD seams are patched at their
route use-site while the routing, permission gating (``require_permission``
resolving through the real tenant principal), request parsing, validation,
audit dispatch and response serialisation all run for real — so the scenarios
assert the actual API contract for the review workflow and the eval-proposal
publish surface.
"""

import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

scenarios("../features/eval/feedback_inbox.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000102")
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000103")

_RECORD_PIPELINE_NAME = "Scorecard Pipeline"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _record_uuid(record_id: str) -> uuid.UUID:
    """Deterministically map a feature-file record id onto a UUID."""
    return uuid.uuid5(_ORG_ID, record_id)


def _make_record(**overrides: object) -> MagicMock:
    """FeedbackRecord-shaped mock matching ``_serialise_record``'s reads."""
    r = MagicMock()
    r.id = overrides.get("id", uuid.uuid4())
    r.run_id = overrides.get("run_id", _RUN_ID)
    r.account_id = overrides.get("account_id", _ACCOUNT_ID)
    r.gate_id = overrides.get("gate_id", "gate-1")
    r.rejection_reason = overrides.get("rejection_reason", "Wrong output")
    r.rejected_output = overrides.get("rejected_output", {"result": "bad"})
    r.producing_node_id = overrides.get("producing_node_id", "node-b")
    r.producing_agent_id = overrides.get("producing_agent_id")
    r.feedback_status = overrides.get("feedback_status", "pending")
    r.feedback_handler_type = overrides.get("feedback_handler_type", "human")
    r.correction_run_id = overrides.get("correction_run_id")
    r.eval_gap = overrides.get("eval_gap", False)
    r.needs_human_review = overrides.get("needs_human_review", True)
    r.annotation = overrides.get("annotation")
    r.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    return r


def _make_session(*, run_row: Any | None = None) -> AsyncMock:
    """AsyncSession double dispatching the feedback routes' reads.

    ``require_permission``'s per-request org kill-switch read
    (``organisations.authz_enforce``) resolves to None (defaults to enforce=True
    in ``resolve_authz_enforce``, so the admin principal passes the org-role
    gate for real). The publish route additionally resolves the record's run,
    which is served from ``run_row`` when provided. Every other read returns an
    empty result so un-seeded queries are deterministic.
    """
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = AsyncMock(return_value=bind)
    session.in_transaction = MagicMock(return_value=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt).lower()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.all.return_value = []
        if "authz_enforce" in text:
            return result
        if run_row is not None and "from runs" in text:
            run_result = MagicMock()
            run_result.scalar_one_or_none.return_value = run_row
            return run_result
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_test_client(session: AsyncMock) -> TestClient:
    """Build an admin-principal TestClient against the real feedback app."""

    async def override_session() -> Any:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    return TestClient(app)


def _clear_overrides() -> None:
    for dep in (get_settings, get_db_session, _get_engine, get_current_tenant_user):
        app.dependency_overrides.pop(dep, None)


def _dispatch(request: Any, method: str, url: str, *, json: dict[str, Any] | None = None) -> None:
    """Issue the request against an admin client and stash the response."""
    with ExitStack() as stack:
        for patcher in _active_patchers(request):
            stack.enter_context(patcher)
        client = _make_test_client(_ctx(request)["session"])
        try:
            resp = client.request(method, url, json=json)
        finally:
            _clear_overrides()
            client.close()
    request.node._resp = resp


def _active_patchers(request: Any) -> list[patch]:
    return _ctx(request).get("patchers", [])


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {"session": _make_session()}
    return cast("dict[str, Any]", request.node._ctx)


def _body(request: Any) -> dict[str, Any]:
    resp = request.node._resp
    body = resp.json()
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


def _standard_patches() -> list[patch]:
    """RLS + audit seams every covered endpoint touches."""
    return [
        patch("modulo.api.routes.feedback.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.feedback.set_rls_user_context", new=AsyncMock()),
        patch("modulo.api.routes.feedback.append_audit_event_isolated", new=AsyncMock(return_value=MagicMock())),
    ]


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am an admin feedback reviewer")
def _given_auth_reviewer() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given("the organisation has feedback records awaiting review")
def _given_inbox_records(request: Any) -> None:
    ctx = _ctx(request)
    ctx["state"] = "inbox"
    ctx["record"] = _make_record(eval_gap=False, run_id=_RUN_ID)


@given(parsers.parse('a feedback record with id "{record_id}" is in the inbox'))
def _given_inbox_item(request: Any, record_id: str) -> None:
    ctx = _ctx(request)
    ctx["state"] = "inbox-item"
    ctx["record_id"] = record_id
    ctx["record"] = _make_record(id=_record_uuid(record_id), eval_gap=False, run_id=_RUN_ID)


@given("the organisation has an eval proposal record")
def _given_gap_proposal(request: Any) -> None:
    ctx = _ctx(request)
    ctx["state"] = "proposal-gap"
    ctx["record"] = _make_record(id=_record_uuid("rec-1"), eval_gap=True, run_id=_RUN_ID)


@given("the organisation has a non-gap feedback record")
def _given_non_gap(request: Any) -> None:
    ctx = _ctx(request)
    ctx["state"] = "proposal-nongap"
    ctx["record"] = _make_record(id=_record_uuid("rec-1"), eval_gap=False, run_id=_RUN_ID)


@given("the organisation has a resolved feedback record")
def _given_resolved_record(request: Any) -> None:
    ctx = _ctx(request)
    ctx["state"] = "proposal-resolved"
    ctx["record"] = _make_record(id=_record_uuid("rec-1"), eval_gap=True, feedback_status="resolved", run_id=_RUN_ID)


@given("no feedback record exists")
def _given_no_record(request: Any) -> None:
    ctx = _ctx(request)
    ctx["state"] = "missing"
    ctx["record"] = None


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.re(r"I request GET /api/v1/feedback/inbox(?P<query>[^ ]*)"))
def _when_get_inbox(request: Any, query: str = "") -> None:
    ctx = _ctx(request)
    inbox = AsyncMock(
        return_value={
            "items": [ctx.get("record") or _make_record(eval_gap=False, run_id=_RUN_ID)],
            "pipeline_map": {str(_RUN_ID): _RECORD_PIPELINE_NAME},
            "total": 1,
            "page": 1,
            "page_size": 20,
        }
    )
    ctx["session"] = _make_session()
    ctx["patchers"] = [
        *_standard_patches(),
        patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_records_inbox", new=inbox),
    ]
    ctx["inbox_mock"] = inbox
    _dispatch(request, "GET", "/api/v1/feedback/inbox" + query)


@when(parsers.parse("I request GET /api/v1/feedback/inbox/{record_id}"))
def _when_get_inbox_item(request: Any, record_id: str) -> None:
    ctx = _ctx(request)
    record = ctx.get("record")
    ctx["session"] = _make_session()
    ctx["patchers"] = [
        *_standard_patches(),
        patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record", new=AsyncMock(return_value=record)),
    ]
    _dispatch(request, "GET", f"/api/v1/feedback/inbox/{_record_uuid(record_id)}")


@when(parsers.parse('I review the feedback record "{record_id}" with action "{action}"'))
def _when_review(request: Any, record_id: str, action: str) -> None:
    ctx = _ctx(request)
    ctx["session"] = _make_session()
    if ctx.get("state") == "missing":
        ctx["patchers"] = [
            patch("modulo.api.routes.feedback.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record", new=AsyncMock(return_value=None)),
        ]
    else:
        record = ctx.get("record")
        patches = [
            *_standard_patches(),
            patch(
                "modulo.api.routes.feedback.FeedbackManager.get_feedback_record",
                new=AsyncMock(return_value=record),
            ),
        ]
        if action == "mark_reviewed":
            patches.append(
                patch(
                    "modulo.api.routes.feedback.FeedbackManager.update_status",
                    new=AsyncMock(return_value=_make_record(id=record.id, feedback_status="resolved")),
                )
            )
        elif action == "dismiss":
            patches.append(
                patch(
                    "modulo.api.routes.feedback.FeedbackManager.update_status",
                    new=AsyncMock(return_value=_make_record(id=record.id, feedback_status="dismissed")),
                )
            )
        elif action == "create_correction_run":
            spawn = AsyncMock(return_value=uuid.uuid4())
            patches.append(patch("modulo.api.routes.feedback.FeedbackManager.spawn_correction_run", new=spawn))
            ctx["spawn_mock"] = spawn
        ctx["patchers"] = patches
    _dispatch(request, "POST", f"/api/v1/feedback/inbox/{_record_uuid(record_id)}/review", json={"action": action})


@when(parsers.parse('I run eval-gap detection on the feedback record "{record_id}"'))
def _when_detect_gap(request: Any, record_id: str) -> None:
    ctx = _ctx(request)
    record = ctx.get("record")
    ctx["session"] = _make_session()
    ctx["patchers"] = [
        *_standard_patches(),
        patch(
            "modulo.api.routes.feedback.FeedbackManager.get_feedback_record",
            new=AsyncMock(return_value=record),
        ),
        patch("modulo.api.routes.feedback.FeedbackManager.detect_eval_gap", new=AsyncMock(return_value=True)),
    ]
    _dispatch(request, "POST", f"/api/v1/feedback/{_record_uuid(record_id)}/detect-gap")


@when("I request GET /api/v1/feedback/proposals")
def _when_get_proposals(request: Any) -> None:
    ctx = _ctx(request)
    record = ctx.get("record")
    proposals = AsyncMock(
        return_value={
            "items": [record],
            "total": 1,
            "page": 1,
            "page_size": 20,
        }
    )
    ctx["session"] = _make_session()
    ctx["patchers"] = [
        *_standard_patches(),
        patch("modulo.api.routes.feedback.FeedbackManager.get_eval_proposals", new=proposals),
    ]
    _dispatch(request, "GET", "/api/v1/feedback/proposals")


@when(parsers.parse('I publish the proposal "{record_id}" as eval "{name}"'))
def _when_publish(request: Any, record_id: str, name: str) -> None:
    ctx = _ctx(request)
    state = ctx.get("state")
    record = ctx.get("record")

    if state == "missing":
        ctx["session"] = _make_session()
        ctx["patchers"] = [
            patch("modulo.api.routes.feedback.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.feedback.FeedbackManager.get_feedback_record", new=AsyncMock(return_value=None)),
        ]
    elif state == "proposal-gap":
        run_row = MagicMock()
        run_row.pipeline_id = _PIPELINE_ID
        ctx["session"] = _make_session(run_row=run_row)
        ctx["patchers"] = [
            *_standard_patches(),
            patch(
                "modulo.api.routes.feedback.FeedbackManager.get_feedback_record",
                new=AsyncMock(return_value=record),
            ),
            patch(
                "modulo.api.routes.feedback.FeedbackManager.update_status",
                new=AsyncMock(return_value=_make_record(id=record.id, feedback_status="resolved")),
            ),
        ]
    else:
        ctx["session"] = _make_session()
        ctx["patchers"] = [
            *_standard_patches(),
            patch(
                "modulo.api.routes.feedback.FeedbackManager.get_feedback_record",
                new=AsyncMock(return_value=record),
            ),
        ]

    _dispatch(
        request,
        "POST",
        f"/api/v1/feedback/proposals/{_record_uuid(record_id)}/publish",
        json={
            "name": name,
            "eval_type": "regex",
            "config": {"pattern": "42", "field": "answer"},
            "node_id": str(_NODE_ID),
        },
    )


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the inbox response contains the feedback record with its pipeline name")
def _then_inbox_item(request: Any) -> None:
    body = _body(request)
    assert body["total"] == 1
    item = body["items"][0]
    assert item["feedback_status"] == "pending"
    assert item["pipeline_name"] == _RECORD_PIPELINE_NAME


@then("the inbox filter is passed to the feedback manager")
def _then_inbox_filter(request: Any) -> None:
    inbox = _ctx(request)["inbox_mock"]
    inbox.assert_awaited_once()
    _call_kwargs = inbox.await_args.kwargs
    assert _call_kwargs.get("handler_type") == "human"
    assert _call_kwargs.get("status") == "pending"


@then(parsers.parse('the response record id matches "{record_id}"'))
def _then_response_record_id(request: Any, record_id: str) -> None:
    assert _body(request)["id"] == str(_record_uuid(record_id))


@then("the response record carries the rejected output")
def _then_response_rejected_output(request: Any) -> None:
    body = _body(request)
    assert body["rejected_output"] == {"result": "bad"}
    assert body["rejection_reason"] == "Wrong output"


@then(parsers.parse('the reviewed record status is "{status}"'))
def _then_reviewed_status(request: Any, status: str) -> None:
    assert _body(request)["feedback_status"] == status


@then("the review response carries a correction run id")
def _then_correction_run_id(request: Any) -> None:
    assert _body(request)["correction_run_id"]


@then("the correction run was spawned for the record")
def _then_correction_spawned(request: Any) -> None:
    spawn = _ctx(request).get("spawn_mock")
    assert spawn is not None
    spawn.assert_awaited_once()


@then("the detection response reports eval_gap true")
def _then_detect_gap(request: Any) -> None:
    assert _body(request)["eval_gap"] is True


@then("the proposals response contains the proposal record")
def _then_proposals(request: Any) -> None:
    body = _body(request)
    assert body["total"] == 1
    item = body["items"][0]
    assert item["eval_gap"] is True
    assert item["feedback_status"] == "pending"


@then("the published eval is scoped to the record's pipeline and node")
def _then_published_scoped(request: Any) -> None:
    body = _body(request)
    assert body["pipeline_id"] == str(_PIPELINE_ID)
    assert body["node_id"] == str(_NODE_ID)
    assert body["feedback_status"] == "resolved"


@then(parsers.parse('the published eval has type "{eval_type}"'))
def _then_published_type(request: Any, eval_type: str) -> None:
    assert _body(request)["eval_type"] == eval_type
