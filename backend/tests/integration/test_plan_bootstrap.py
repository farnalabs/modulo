"""Integration test: ``GET /api/v1/plan`` serves the effective plan to a NON-admin member (FAR-1806).

The plan/tier/feature bootstrap previously read the instance-admin-only
``/api/v1/admin/feature-flags`` endpoint, so every non-system-admin ran the
app on permanently-wrong defaults. This test drives the real route against a
real migrated Postgres with an ``org_role: viewer`` / ``is_system_admin: false``
principal and the org's ``feature_overrides`` set, asserting the three contract
points that made the obvious shortcuts wrong:

* a non-admin gets **200** (not 403),
* the org overlay is applied (a default-OFF flag the org enabled reads
  ``currently_active: true``),
* inactive flags are PRESENT with ``currently_active: false`` (the active-only
  payload dropped them entirely).

``/viewmodel/current`` is asserted with the same identity so the two
flag-reading surfaces cannot silently diverge again.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.jwt import create_access_token
from modulo.core.feature_flags import _KNOWN_FLAGS, RETIRED_INTERNAL_FLAGS
from modulo.db.soft_delete import register_soft_delete_filter

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32

# Org override set: a default-OFF flag turned ON (proves the overlay applies
# on a real row) and a default-ON community flag turned OFF (proves the overlay
# bites in both directions against the real catalog).
_ORG_SETTINGS = '{"feature_overrides": {"mobile_sidebar_rail": true, "analytics_page": false}}'


async def _seed_org(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) "
                "VALUES (:id, :name, :slug, CAST(:settings AS json))"
            ),
            {
                "id": str(org_id),
                "name": "PlanBootstrapOrg",
                "slug": f"plan-bootstrap-{org_id.hex[:8]}",
                "settings": _ORG_SETTINGS,
            },
        )
    return org_id


async def _seed_viewer(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": f"viewer-{org_id.hex[:8]}@test.local", "name": "viewer"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'viewer')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


def _viewer_token(org_id: uuid.UUID, account_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"viewer-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role="viewer",
        is_system_admin=False,
        client_kind="browser",
    )


async def test_plan_bootstrap_serves_effective_plan_to_non_admin(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A viewer reads their org's real tier/dev-mode/effective flags — no 403."""
    register_soft_delete_filter()

    org_id = await _seed_org(db_engine)
    account_id = await _seed_viewer(db_engine, org_id)
    headers = {"Authorization": f"Bearer {_viewer_token(org_id, account_id)}"}

    resp = await integration_client.get("/api/v1/plan", headers=headers)
    assert resp.status_code == 200, f"non-admin must read the plan, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert body["license"]["tier"] == "community"
    assert isinstance(body["dev_mode"], bool)

    flags = {f["name"]: f for f in body["flags"]}
    # Org overlay applied ON: default-OFF flag the org enabled reads active.
    assert flags["mobile_sidebar_rail"]["currently_active"] is True
    # Org overlay applied OFF: default-ON community flag the org disabled.
    assert flags["analytics_page"]["currently_active"] is False
    # Inactive flag PRESENT with false (the active-only payload dropped it).
    assert flags["user_scoped_mcp_keys"]["currently_active"] is False
    # Untouched default flows through: community flag, no override.
    assert flags["eval_system"]["currently_active"] is True
    # Full known set is served, not a filtered subset. Derive the expected set
    # from the registry single source of truth (_KNOWN_FLAGS -> catalog.FLAGS)
    # rather than a magic number, so retiring a flag (FAR-1819 dropped
    # pipeline_diff_rollback) does not silently turn this into a stale check.
    # Retired internal flags (FAR-2200 retired lifecycle_map_journeys) stay in
    # _KNOWN_FLAGS for persisted-data compatibility but are deliberately
    # filtered out of every customer-facing plan response, so the expected set
    # is the known set minus RETIRED_INTERNAL_FLAGS.
    known = {flag.name for flag in _KNOWN_FLAGS} - RETIRED_INTERNAL_FLAGS
    served = {f["name"] for f in body["flags"]}
    assert known <= served, f"plan bootstrap dropped known flags: {sorted(known - served)}"


async def test_viewmodel_feature_flags_agree_with_plan_bootstrap(
    integration_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Both flag-reading surfaces report the SAME effective state for one org."""
    register_soft_delete_filter()

    org_id = await _seed_org(db_engine)
    account_id = await _seed_viewer(db_engine, org_id)
    headers = {"Authorization": f"Bearer {_viewer_token(org_id, account_id)}"}

    plan_resp = await integration_client.get("/api/v1/plan", headers=headers)
    vm_resp = await integration_client.get("/api/v1/viewmodel/current", headers=headers)
    assert plan_resp.status_code == 200, plan_resp.text
    assert vm_resp.status_code == 200, vm_resp.text

    plan_flags = {f["name"]: f["currently_active"] for f in plan_resp.json()["flags"]}
    vm_flags = {f["name"]: f["active"] for f in vm_resp.json()["feature_flags"]}
    assert plan_flags == vm_flags
    # The disagreement this parity pins: pre-fix viewmodel omitted/overrode none.
    assert vm_flags["mobile_sidebar_rail"] is True
    assert vm_flags["analytics_page"] is False
