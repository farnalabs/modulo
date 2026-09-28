"""ITEM 4 (FAR-1163 follow-up): the team gate on convert-to-agent / revert-to-manual.

Before this change NEITHER layer of the team gate existed on the two
node-conversion endpoints:

* no ``require_team_membership_or_admin(resolve_pipeline_team_scope)``
  dependency (request-time check), and
* no ``_reapply_team_gate_inside_mutation_txn`` re-check inside the mutation
  transaction (the request-time -> mutation TOCTOU close),

unlike their siblings ``replace_pipeline_graph_endpoint`` (PATCH /graph) and
``update_pipeline_endpoint`` (PATCH /{id}). A non-member org operator could
therefore mutate a team-private pipeline through these two routes.

Tests (mocked session, real FastAPI dependency stack):

* non-member -> 403 from the request-time dependency (both routes),
* member / org-admin -> the request completes (200),
* TOCTOU half: the request-time gate PASSES (membership exists) while the
  in-txn re-check DENIES - the endpoint must still answer 403, proving the
  in-txn layer is wired and is not a no-op behind a passing dependency.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NODE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_PREFIX = "modulo.api.routes.pipelines."

_NON_MEMBER_DETAIL = "Not a member of the team that owns this resource"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _manual_node() -> dict[str, Any]:
    return {"id": str(_NODE_ID), "node_type": "manual", "position": {"x": 0, "y": 0}, "label": "qa"}


def _agent_node() -> dict[str, Any]:
    node = _manual_node()
    node["node_type"] = "agent"
    node["agent_id"] = str(uuid.uuid4())
    return node


def _convert_ready_manual_node() -> dict[str, Any]:
    node = _manual_node()
    node.pop("agent_id", None)
    return node


def _result(*, first: Any = None, scalar_one_or_none: Any = None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = first
    result.scalar_one_or_none.return_value = scalar_one_or_none
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def _make_team_session(*, is_member: bool) -> AsyncMock:
    """Session double whose queries answer the two-layer team gate.

    Dialect reports ``sqlite`` (so the Postgres-only ``set_config`` lock-timeout
    statement is not issued - that path has its own test), and every query the
    real dependency stack issues is dispatched on its SQL text rather than by
    call position, so statement ORDER changes cannot silently swap answers.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)

    pipeline_row = MagicMock()
    pipeline_row.visibility = "team"
    pipeline_row.owner_team_id = _TEAM_ID
    pipeline_row.deleted_at = None
    pipeline_row.graph_nodes_json = [_agent_node()]

    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.organisation_id = _ORG_ID
    connector = MagicMock()
    connector.connector_type_id = "github"
    connector.organisation_id = _ORG_ID
    backend = MagicMock()
    backend.organisation_id = _ORG_ID

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if "set_config" in sql:
            return _result()
        if "authz_enforce" in sql:
            return _result(scalar_one_or_none=None)
        if "team_memberships" in sql:
            return _result(first=uuid.uuid4() if is_member else None)
        if "FROM agents" in sql:
            return _result(scalar_one_or_none=agent)
        if "connector_instances" in sql:
            return _result(scalar_one_or_none=connector)
        if "model_backends" in sql:
            return _result(scalar_one_or_none=backend)
        if "FROM pipelines" in sql:
            if isinstance(stmt, Select) and "FOR UPDATE" in sql.upper():
                return _result(scalar_one_or_none=pipeline_row)
            return _result(first=(_TEAM_ID, "team"))
        return _result()

    session.execute = AsyncMock(side_effect=_execute)
    return session


@contextmanager
def _client_for(session: AsyncMock, role: str) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _patched(patches: list) -> Generator[None, None, None]:
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _convert_body() -> dict[str, Any]:
    return {
        "agent_id": str(uuid.uuid4()),
        "connector_binding": {"type": "github", "instance_id": str(uuid.uuid4())},
        "model_backend_id": str(uuid.uuid4()),
    }


def _convert_patches(nodes: list[dict[str, Any]], saved: object) -> list:
    return [
        patch(f"{_PREFIX}_load_locked_pipeline_graph", new=AsyncMock(return_value=(nodes, []))),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}_save_locked_graph", new=AsyncMock(return_value=saved)),
    ]


def _revert_patches(nodes: list[dict[str, Any]], snapshot: object, saved: object) -> list:
    return [
        patch(f"{_PREFIX}_load_locked_pipeline_graph", new=AsyncMock(return_value=(nodes, []))),
        patch(f"{_PREFIX}get_snapshot_detail", new=AsyncMock(return_value=snapshot)),
        patch(f"{_PREFIX}append_audit_event", new_callable=AsyncMock),
        patch(f"{_PREFIX}_save_locked_graph", new=AsyncMock(return_value=saved)),
    ]


def _snapshot_with_manual_node() -> MagicMock:
    snapshot = MagicMock()
    snapshot.id = uuid.uuid4()
    snapshot.graph_json = {
        "nodes": [
            {
                "id": str(_NODE_ID),
                "node_type": "manual",
                "position": {"x": 0, "y": 0},
                "label": "qa",
                "output_schema_id": str(uuid.uuid4()),
            }
        ],
        "edges": [],
    }
    return snapshot


# ---------------------------------------------------------------------------
# Request-time dependency: non-member denied, member / admin allowed
# ---------------------------------------------------------------------------


def test_convert_non_member_is_denied_403() -> None:
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/nodes/{_NODE_ID}/convert-to-agent",
            json=_convert_body(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_revert_non_member_is_denied_403() -> None:
    session = _make_team_session(is_member=False)
    with _client_for(session, role="operator") as http:
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/nodes/{_NODE_ID}/revert-to-manual",
            params={"snapshot_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL


def test_convert_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    nodes = [_convert_ready_manual_node()]
    with _client_for(session, role="operator") as http, _patched(_convert_patches(nodes, (nodes, []))):
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/nodes/{_NODE_ID}/convert-to-agent",
            json=_convert_body(),
        )
    assert resp.status_code == 200, resp.text


def test_revert_member_succeeds() -> None:
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        _patched(_revert_patches([_agent_node()], _snapshot_with_manual_node(), ([_manual_node()], []))),
    ):
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/nodes/{_NODE_ID}/revert-to-manual",
            params={"snapshot_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 200, resp.text


def test_convert_org_admin_bypasses_the_membership_gate() -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    session = _make_team_session(is_member=False)
    nodes = [_convert_ready_manual_node()]
    with _client_for(session, role="admin") as http, _patched(_convert_patches(nodes, (nodes, []))):
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/nodes/{_NODE_ID}/convert-to-agent",
            json=_convert_body(),
        )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# In-txn half: the request-time gate passing must NOT mean the mutation runs
# ---------------------------------------------------------------------------


def test_in_txn_team_gate_denial_overrides_a_passing_request_time_gate() -> None:
    """TOCTOU close: dependency says member, the locked row says otherwise.

    The request-time dependency resolves membership through
    ``modulo.api.dependencies``' own import (the session answers a membership
    row), while the in-txn re-check resolves it through the one imported into
    ``routes.pipelines`` - patched here to deny, exactly as an ownership flip
    inside the request -> mutation window would behave. The endpoint must
    still answer 403: a passing request-time gate must never be the only gate.
    """
    session = _make_team_session(is_member=True)
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)),
    ):
        resp = http.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/nodes/{_NODE_ID}/convert-to-agent",
            json=_convert_body(),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
