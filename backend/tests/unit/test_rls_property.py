"""Hypothesis property-based tests for RLS/authz deny-by-default and cross-tenant isolation.

The thesis: "One leaked cross-org row invalidates the entire pitch."
These tests generate (org, role, resource, operation) tuples and assert the
invariants that hold in the real RBAC/RLS implementation rather than only
documenting intent:

* property-fuzz the generic-backend ORM tenant filter (``_inject_tenant_filter``)
  — any SELECT/UPDATE/DELETE touching an org-scoped entity receives a tenant
  predicate bound to the *caller's* org_id, and only those statements;
* no tenant predicate is injected without an org context, for INSERTs, or for
  entities that are not org-scoped;
* ``set_rls_org`` scopes sessions on generic backends, issues ``set_config`` on
  Postgres, requires an active transaction, and no-ops on ``None`` org;
* effective team role is capped at the lower of the org and team privilege
  levels, and unknown roles fall back to ``viewer``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import Column, Integer, Uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import declarative_base

from modulo.auth.team_rbac import (
    ORG_ROLE_HIERARCHY,
    TEAM_ROLE_HIERARCHY,
    get_effective_team_role,
    org_role_level,
    team_role_level,
)
from modulo.db.rls import _inject_tenant_filter, set_rls_org

_Base = declarative_base()


class OrgScopedEntity(_Base):
    """Mapped entity carrying an ``organisation_id`` column."""

    __tablename__ = "org_scoped_entity"

    id = Column(Integer, primary_key=True)
    organisation_id = Column(Uuid)


class NonOrgEntity(_Base):
    """Mapped entity with no ``organisation_id`` column."""

    __tablename__ = "non_org_entity"

    id = Column(Integer, primary_key=True)


_ORG_SCOPED_ENTITY = OrgScopedEntity
_NON_ORG_ENTITY = NonOrgEntity
_ENTITY_POOL = (_ORG_SCOPED_ENTITY, _NON_ORG_ENTITY, None, object)
_INJECTABLE_KINDS = ("select", "update", "delete")

ORG_ROLES = tuple(ORG_ROLE_HIERARCHY.keys())
TEAM_ROLES = tuple(TEAM_ROLE_HIERARCHY.keys())


def _org_id() -> st.SearchStrategy[uuid.UUID]:
    return st.uuids()


def _make_session(*, in_tx: bool = True, dialect: str = "postgresql") -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = in_tx
    session.info = {}
    session.execute = AsyncMock()

    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind
    return session


def _is_org_scoped(entity: object) -> bool:
    return entity is _ORG_SCOPED_ENTITY


class _Mapper:
    """Stand-in for a SQLAlchemy mapper exposing ``class_``."""

    def __init__(self, entity: object) -> None:
        self.class_ = entity


def _make_state(*, org_id: uuid.UUID | None, kind: str, entities: list) -> MagicMock:
    state = MagicMock()
    state.session.info = {}
    if org_id is not None:
        state.session.info["org_id"] = org_id
    state.is_select = kind == "select"
    state.is_update = kind == "update"
    state.is_delete = kind == "delete"
    state.statement = MagicMock()
    # Chain injected predicates onto the same mock so call_count reflects
    # the number of tenant predicates applied, not just the first one.
    state.statement.where.return_value = state.statement
    if kind == "select":
        state.statement.column_descriptions = [{"entity": e} for e in entities]
    elif kind in ("update", "delete"):
        state.all_mapper_classes = [_Mapper(e) for e in entities]
    return state


# ── RBAC: effective team role is capped by both roles ──────────────────────


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)
@given(st.sampled_from(ORG_ROLES), st.sampled_from(TEAM_ROLES))
def test_effective_team_role_is_capped_by_both_roles(org_role: str, team_role: str) -> None:
    """Effective team role never exceeds either the org role or the team role."""
    effective = get_effective_team_role(org_role, team_role)
    assert effective in TEAM_ROLE_HIERARCHY
    assert team_role_level(effective) <= org_role_level(org_role), f"effective {effective} exceeds org role {org_role}"
    assert team_role_level(effective) <= team_role_level(team_role), (
        f"effective {effective} exceeds team role {team_role}"
    )


@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(st.sampled_from(TEAM_ROLES))
def test_admin_org_role_never_caps_team_role(team_role: str) -> None:
    """Admin org role is the top of the hierarchy — team role passes through."""
    assert get_effective_team_role("admin", team_role) == team_role


@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(st.sampled_from(TEAM_ROLES))
def test_viewer_org_role_caps_effective_role_to_viewer(team_role: str) -> None:
    """Viewer org role caps the effective team role to viewer."""
    assert get_effective_team_role("viewer", team_role) == "viewer"


@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    deadline=None,
)
@given(st.text(min_size=1, max_size=30))
def test_unknown_roles_fall_back_to_viewer(role: str) -> None:
    """Unknown roles are denied by default — they map to viewer."""
    assume(role not in ORG_ROLE_HIERARCHY)
    assume(role not in TEAM_ROLE_HIERARCHY)
    assert org_role_level(role) == -1
    assert team_role_level(role) == -1
    assert get_effective_team_role(role, "runner") == "viewer"
    assert get_effective_team_role("admin", role) == "viewer"


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(
    org_role=st.sampled_from(sorted(ORG_ROLE_HIERARCHY)),
    team_role=st.sampled_from(sorted(TEAM_ROLE_HIERARCHY)),
)
def test_effective_team_role_level_is_min_of_org_and_team(org_role: str, team_role: str) -> None:
    """The effective team role never exceeds either the org or team level."""
    effective = get_effective_team_role(org_role, team_role)
    expected = min(ORG_ROLE_HIERARCHY[org_role], TEAM_ROLE_HIERARCHY[team_role])
    assert TEAM_ROLE_HIERARCHY[effective] == expected


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(st.text(min_size=1, max_size=20))
def test_unknown_roles_degrade_to_viewer(role_name: str) -> None:
    """Unrecognised org or team roles must degrade to ``viewer``."""
    assume(role_name not in ORG_ROLE_HIERARCHY and role_name not in TEAM_ROLE_HIERARCHY)
    assert get_effective_team_role(role_name, "viewer") == "viewer"
    assert get_effective_team_role("viewer", role_name) == "viewer"
    assert get_effective_team_role(role_name, role_name) == "viewer"


def test_org_role_hierarchy_is_contiguous_total_order() -> None:
    levels = sorted(ORG_ROLE_HIERARCHY.values())
    assert levels == list(range(len(levels)))


def test_team_role_hierarchy_is_contiguous_total_order() -> None:
    levels = sorted(TEAM_ROLE_HIERARCHY.values())
    assert levels == list(range(len(levels)))


# ── RLS: tenant filter injection invariant ─────────────────────────────────


@settings(
    max_examples=150,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(
    org_id=st.uuids() | st.none(),
    kind=st.sampled_from(("select", "update", "delete", "insert")),
    entities=st.lists(st.sampled_from(_ENTITY_POOL), min_size=0, max_size=5),
)
def test_tenant_filter_injection_invariant(
    org_id: uuid.UUID | None,
    kind: str,
    entities: list,
) -> None:
    """A tenant predicate is injected iff an org context exists AND an
    org-scoped entity is touched by a SELECT/UPDATE/DELETE."""
    state = _make_state(org_id=org_id, kind=kind, entities=entities)
    org_scoped_count = sum(1 for e in entities if _is_org_scoped(e))
    expect_injection = org_id is not None and kind in _INJECTABLE_KINDS and org_scoped_count > 0

    _inject_tenant_filter(state)

    if expect_injection:
        assert state.statement.where.call_count == org_scoped_count, (
            f"expected {org_scoped_count} tenant predicate(s) for kind={kind} "
            f"entities={entities!r}, got {state.statement.where.call_count}"
        )
        predicate = state.statement.where.call_args[0][0]
        assert predicate.left.name == "organisation_id"
        assert predicate.right.value == org_id
    else:
        assert state.statement.where.call_count == 0, (
            f"unexpected tenant injection for org_id={org_id} kind={kind} entities={entities!r}"
        )


def test_no_org_context_never_injects() -> None:
    """Explicit guard for the deny-by-default baseline: without an org in
    ``session.info`` no tenant predicate may ever be injected."""
    for kind in ("select", "update", "delete", "insert"):
        state = _make_state(org_id=None, kind=kind, entities=[_ORG_SCOPED_ENTITY])
        _inject_tenant_filter(state)
        assert state.statement.where.call_count == 0, f"injected without org context for kind={kind}"


# ── RLS: tenant scoping is org-aware ───────────────────────────────────────


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(_org_id())
async def test_set_rls_org_generic_backend_scopes_session(org_id: uuid.UUID) -> None:
    """On generic backends, RLS stores the org in session.info for the tenant filter."""
    session = _make_session(dialect="sqlite")

    await set_rls_org(session, org_id)

    assert session.info["org_id"] == org_id
    session.execute.assert_not_awaited()


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(_org_id())
async def test_set_rls_org_postgres_uses_set_config(org_id: uuid.UUID) -> None:
    """On Postgres, RLS sets the org via set_config with a bound parameter."""
    session = _make_session(dialect="postgresql")

    await set_rls_org(session, org_id)

    session.execute.assert_awaited_once()
    call_args = session.execute.await_args
    assert call_args is not None
    compiled = str(call_args.args[0].compile())
    assert "set_config" in compiled
    assert call_args.args[1]["oid"] == str(org_id)


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
@given(_org_id())
async def test_set_rls_org_requires_active_transaction(org_id: uuid.UUID) -> None:
    """Deny-by-default: setting RLS outside a transaction raises instead of silently no-oping."""
    session = _make_session(in_tx=False)

    with pytest.raises(RuntimeError, match="requires an active transaction"):
        await set_rls_org(session, org_id)


async def test_set_rls_org_none_skips_scoping() -> None:
    """A None org (system context) must not inject tenant scoping."""
    session = _make_session(dialect="sqlite")

    await set_rls_org(session, None)

    assert session.info == {}
    session.execute.assert_not_awaited()


async def test_set_rls_org_none_is_noop() -> None:
    """``set_rls_org(None)`` (system admin, no org claim) must not touch the
    session — no ``set_config`` call and no ``session.info`` scoping."""
    session = MagicMock()
    session.info = {}
    session.in_transaction.return_value = True
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = _async_returning(bind)

    await set_rls_org(session, None)

    session.execute.assert_not_called()
    assert "org_id" not in session.info


def _async_returning(value: object):
    async def _get() -> object:
        return value

    return _get
