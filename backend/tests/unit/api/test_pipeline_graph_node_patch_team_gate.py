"""FAR-1829: the two-layer team gate on ``PATCH /{pipeline_id}/graph/nodes/{node_id}``.

PARITY + defence-in-depth: the node-targeted patch carries the SAME two team
gate layers as its sibling graph writes, and BOTH layers must actually execute
here — the existing references were structural only (a route-introspection
path enumeration and a route-verb assertion), neither of which would fail if
the in-transaction re-check were deleted:

* no ``require_team_membership_or_admin(resolve_pipeline_team_scope)``
  dependency (request-time check), and
* no ``_reapply_team_gate_inside_mutation_txn`` re-check inside the mutation
  transaction (the request-time -> mutation TOCTOU close).

The in-txn re-check is the load-bearing layer: the request-time dependency
runs in its own transaction that COMMITs before the endpoint's mutation
transaction opens, so ownership/visibility can change between the two.

Tests (mocked session, real FastAPI dependency stack — the same fixture shape
as ``test_pipeline_node_conversion_team_gate.py``):

* non-member -> 403 from the request-time dependency;
* member / org-admin -> the request completes (200);
* TOCTOU half: the request-time gate PASSES (membership exists) while the
  in-txn re-check DENIES -> the endpoint must still answer 403, and the
  in-txn membership probe itself must have run (proof it is wired, not a
  no-op behind a passing dependency);
* in-txn denial because the pipeline MOVED to another team between request
  and mutation: the request-time resolver saw an org-visible pipeline (no
  membership check at all) while the locked row is team-private and the
  caller is not a member -> 403;
* a ``node_id`` that belongs to a DIFFERENT pipeline -> 404 (the node is
  matched only against the addressed pipeline's own stored graph).
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


def _stored_node(node_id: uuid.UUID = _NODE_ID) -> dict[str, Any]:
    """A minimal VALID llm-mode ``sandbox_agent`` stored node.

    Llm mode requires a non-empty ``agent_prompt`` and ``agent_commands``
    (``sandbox_mode._validate_sandbox_mode_config``), so both are seeded — the
    happy-path tests hand the MERGED node through the real
    ``PipelineGraphUpdate`` validation.
    """
    return {
        "id": str(node_id),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "agent_prompt": "do the thing",
        "agent_commands": ["opencode run --task"],
        "label": "Review",
        "timeout_seconds": 600,
    }


def _result(*, first: Any = None, scalar_one_or_none: Any = None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = first
    result.scalar_one_or_none.return_value = scalar_one_or_none
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def _make_session(
    *,
    member: bool,
    resolver_scope: tuple[uuid.UUID | None, str],
    locked: tuple[str, uuid.UUID | None],
    nodes: list[dict[str, Any]],
) -> AsyncMock:
    """Session double whose queries answer the two-layer team gate.

    Dispatches on SQL text rather than by call position (so a statement-order
    change cannot silently swap answers):

    * the request-time resolver's ``SELECT owner_team_id, visibility`` (no
      ``FOR UPDATE``) serves ``resolver_scope``;
    * the in-txn ``SELECT ... FOR UPDATE`` row serves ``locked`` +
      ``nodes`` (``graph_nodes_json``);
    * ``team_memberships`` serves ``member``.

    Dialect reports ``sqlite`` (so the Postgres-only ``set_config`` lock-timeout
    statement is not issued — that path has its own test).
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
    session.add = MagicMock()

    pipeline_row = MagicMock()
    visibility, owner_team_id = locked
    pipeline_row.visibility = visibility
    pipeline_row.owner_team_id = owner_team_id
    pipeline_row.deleted_at = None
    pipeline_row.graph_nodes_json = nodes

    #: How many ``team_memberships`` probes the session answered — lets a test
    #: prove WHICH layer queried (an org-visible request-time scope runs none,
    #: so a count there is the in-txn gate's probe).
    session.membership_query_count = 0

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if "set_config" in sql:
            return _result()
        if "authz_enforce" in sql:
            return _result(scalar_one_or_none=None)
        if "team_memberships" in sql:
            session.membership_query_count += 1
            return _result(first=uuid.uuid4() if member else None)
        if "FROM pipelines" in sql:
            if isinstance(stmt, Select) and "FOR UPDATE" in sql.upper():
                return _result(scalar_one_or_none=pipeline_row)
            return _result(first=resolver_scope)
        if "FROM pipeline_edges" in sql:
            result = _result()
            result.scalars.return_value = []
            return result
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


def _patch_url(node_id: uuid.UUID = _NODE_ID) -> str:
    return f"/api/v1/pipelines/{_PIPELINE_ID}/graph/nodes/{node_id}"


def _apply_write_double(nodes: list[dict[str, Any]] | None = None) -> Any:
    """Double the delegated shared write (the gate runs BEFORE it).

    Returns the ``(nodes, edges, issues)`` triple the handler unpacks.
    """
    return patch(
        f"{_PREFIX}_apply_graph_update",
        new=AsyncMock(return_value=(nodes or [], [], [])),
    )


def _org_scope() -> tuple[uuid.UUID | None, str]:
    return (None, "org")


def _team_scope() -> tuple[uuid.UUID | None, str]:
    return (_TEAM_ID, "team")


# ---------------------------------------------------------------------------
# Request-time dependency: non-member denied, member / org-admin allowed
# ---------------------------------------------------------------------------


def test_non_member_is_denied_403_by_the_request_time_gate() -> None:
    """Resolver says team-private, no membership row -> 403 from the
    request-time dependency — BEFORE the handler runs.

    The in-txn membership probe is spied to prove that ordering: it must NOT
    have executed (had it, the 403 could have come from the in-txn layer and
    this would prove nothing about the request-time gate), and the delegated
    write never runs."""
    session = _make_session(
        member=False,
        resolver_scope=_team_scope(),
        locked=("team", _TEAM_ID),
        nodes=[_stored_node()],
    )
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)) as in_txn_probe,
        _apply_write_double() as write,
    ):
        resp = http.patch(_patch_url(), json={"label": "patched"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    assert session.membership_query_count >= 1, "the request-time gate must have consulted membership"
    in_txn_probe.assert_not_awaited()
    write.assert_not_awaited()


def test_member_succeeds() -> None:
    """Membership row exists -> the request-time gate passes and the endpoint
    completes (200) through the real merge + delegated write double."""
    nodes = [_stored_node()]
    session = _make_session(
        member=True,
        resolver_scope=_team_scope(),
        locked=("team", _TEAM_ID),
        nodes=nodes,
    )
    with (
        _client_for(session, role="operator") as http,
        _apply_write_double(nodes) as write,
    ):
        resp = http.patch(_patch_url(), json={"label": "patched"})
        assert resp.status_code == 200, resp.text
        assert write.await_count == 1


def test_org_admin_bypasses_the_membership_gate() -> None:
    """Admin bypass (RLS parity): no membership row is required."""
    nodes = [_stored_node()]
    session = _make_session(
        member=False,
        resolver_scope=_team_scope(),
        locked=("team", _TEAM_ID),
        nodes=nodes,
    )
    with (
        _client_for(session, role="admin") as http,
        _apply_write_double(nodes) as write,
    ):
        resp = http.patch(_patch_url(), json={"label": "patched"})
        assert resp.status_code == 200, resp.text
        assert write.await_count == 1


# ---------------------------------------------------------------------------
# In-txn half: the request-time gate passing must NOT mean the mutation runs
# ---------------------------------------------------------------------------


def test_in_txn_team_gate_denial_overrides_a_passing_request_time_gate() -> None:
    """TOCTOU close: dependency says member, the in-txn re-check denies.

    The request-time dependency resolves membership through its own module's
    import (the session answers a membership row), while the in-txn re-check
    resolves it through the one imported into ``routes.pipelines`` — patched
    here to deny, exactly as an ownership flip inside the request -> mutation
    window would behave. The endpoint must still answer 403, AND the in-txn
    probe must have executed (otherwise the 403 could only have come from the
    request-time gate — the no-op case this test exists to rule out).
    """
    session = _make_session(
        member=True,
        resolver_scope=_team_scope(),
        locked=("team", _TEAM_ID),
        nodes=[_stored_node()],
    )
    with (
        _client_for(session, role="operator") as http,
        patch(f"{_PREFIX}team_membership_exists", new=AsyncMock(return_value=False)) as in_txn_probe,
        _apply_write_double() as write,
    ):
        resp = http.patch(_patch_url(), json={"label": "patched"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    in_txn_probe.assert_awaited()
    write.assert_not_awaited()


def test_in_txn_gate_denies_after_the_pipeline_moves_teams_mid_request() -> None:
    """The pipeline MOVED to another team between request and mutation.

    The request-time resolver saw an ORG-visible pipeline (so the dependency
    ran NO membership check at all), while the locked ``FOR UPDATE`` row — read
    inside the mutation transaction — is team-private and the caller is not a
    member. Only the in-txn re-check can refuse this: without it the endpoint
    would answer 200 and write a team-private pipeline's graph from outside
    the team. The real (unpatched) ``team_membership_exists`` runs against the
    session here, so the denial travels the production query path.
    """
    session = _make_session(
        member=False,
        resolver_scope=_org_scope(),
        locked=("team", _TEAM_ID),
        nodes=[_stored_node()],
    )
    with (
        _client_for(session, role="operator") as http,
        _apply_write_double() as write,
    ):
        resp = http.patch(_patch_url(), json={"label": "patched"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _NON_MEMBER_DETAIL
    # The request-time gate ran NO membership query (its org scope short-
    # circuits), so this probe can only be the in-txn re-check's — evidence
    # that the denial travelled the in-transaction layer.
    assert session.membership_query_count >= 1, "the in-txn gate must have consulted membership"
    write.assert_not_awaited()


# ---------------------------------------------------------------------------
# The addressed pipeline's OWN graph is the only place the node is matched
# ---------------------------------------------------------------------------


def test_node_id_from_a_different_pipeline_is_404() -> None:
    """A node that exists in ANOTHER pipeline must 404 here: ``node_id`` is
    matched only against the addressed pipeline's stored graph, so a foreign
    node can never be patched (or probed for existence) through this route."""
    session = _make_session(
        member=True,
        resolver_scope=_team_scope(),
        locked=("team", _TEAM_ID),
        nodes=[_stored_node()],  # the addressed pipeline's graph holds _NODE_ID only
    )
    foreign_node_id = uuid.uuid4()  # belongs to some other pipeline's graph
    with (
        _client_for(session, role="operator") as http,
        _apply_write_double() as write,
    ):
        resp = http.patch(_patch_url(foreign_node_id), json={"label": "patched"})
    assert resp.status_code == 404, resp.text
    assert "Node not found" in resp.json()["detail"]
    write.assert_not_awaited()
