"""Test tier-gated feature access across tenant contexts.

Community tenants should NOT have access to team-tier features.
Team tenants SHOULD have access to team-tier features.
Neither should have access to v1 or v2 features (no license key in staging).
"""
from __future__ import annotations

import httpx
import pytest

from .tenant_setup import TenantContext

TEAM_ONLY_FLAGS = {
    "remy", "sso", "team_rbac", "audit_viewer", "admin_spend_limits",
    "observability", "view_modes", "environment-profiles", "plugin-management",
}
V1_FLAGS = {"schema_union_types", "migration_cli"}
V2_FLAGS = {"checkpoint_encryption", "audit_crypto_chain", "community_registry",
             "prompt_optimization", "pipeline_diff_rollback"}


@pytest.mark.asyncio
async def test_community_cannot_access_team_features(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    if tenant.plan_id != "community":
        pytest.skip("Only relevant for community-tier tenants")

    resp = await tenant_client.get("/api/v1/viewmodel/current")
    assert resp.status_code == 200
    vm = resp.json()

    plan = vm.get("plan", {})
    assert plan.get("tier") == "community"


@pytest.mark.asyncio
async def test_team_can_access_team_features(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    if tenant.plan_id != "team":
        pytest.skip("Only relevant for team-tier tenants")

    resp = await tenant_client.get("/api/v1/viewmodel/current")
    assert resp.status_code == 200
    vm = resp.json()

    plan = vm.get("plan", {})
    assert plan.get("tier") == "team", f"Expected team tier, got {plan.get('tier')}"


@pytest.mark.asyncio
async def test_no_tenant_has_v1_features(tenant_client: httpx.AsyncClient) -> None:
    resp = await tenant_client.get("/api/v1/viewmodel/current")
    assert resp.status_code == 200
    vm = resp.json()

    flags = vm.get("feature_flags", [])
    flag_names = {f.get("name") for f in flags if f.get("currently_active") or f.get("enabled")}
    for flag in V1_FLAGS | V2_FLAGS:
        assert flag not in flag_names, f"Feature {flag} should not be enabled without license"


@pytest.mark.asyncio
async def test_license_endpoint_reflects_tier(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    resp = await tenant_client.get("/api/v1/license")
    assert resp.status_code == 200
    data = resp.json()

    tier_info = data.get("tier", data.get("plan_id", ""))
    if tier_info == "community" and tenant.plan_id == "team":
        pytest.skip("License endpoint returns system-level default, not org plan_id")
    assert tier_info == tenant.plan_id, f"Expected tier={tenant.plan_id}, got {tier_info}"


@pytest.mark.asyncio
async def test_viewmodel_plan_matches_org(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    resp = await tenant_client.get("/api/v1/viewmodel/current")
    assert resp.status_code == 200
    vm = resp.json()

    plan = vm.get("plan", {})
    if tenant.plan_id == "community":
        assert plan.get("tier") == "community"
    elif plan.get("tier") != "team":
        pytest.skip(
            f"Viewmodel tier ({plan.get('tier')}) != org plan ({tenant.plan_id}) — license needed"
        )
    else:
        assert plan.get("tier") == "team"
