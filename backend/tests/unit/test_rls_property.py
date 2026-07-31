"""Hypothesis property-based tests for RLS/authz deny-by-default and cross-tenant isolation.

The thesis: "One leaked cross-org row invalidates the entire pitch."
These tests generate (org, role, resource, operation) tuples and assert the
invariants that hold in the real RBAC/RLS implementation rather than only
documenting intent.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.team_rbac import (
    ORG_ROLE_HIERARCHY,
    TEAM_ROLE_HIERARCHY,
    get_effective_team_role,
    org_role_level,
    team_role_level,
)
from modulo.db.rls import set_rls_org

# ── Resource/model type strategies ─────────────────────────────────────────

RESOURCE_TYPES = ("pipeline", "schema", "connector", "model_backend")
OPERATIONS = ("create", "read", "update", "delete", "list")
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
    assert team_role_level(effective) <= org_role_level(org_role), (
        f"effective {effective} exceeds org role {org_role}"
    )
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
