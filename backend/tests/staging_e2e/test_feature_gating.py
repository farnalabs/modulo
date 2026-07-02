"""Test tier-gated feature access across tenant contexts.

Community tenants should NOT have access to team-tier features.
Team tenants SHOULD have access to team-tier features.
Neither should have access to v1 or v2 features (no license key in staging).
"""
from __future__ import annotations

import httpx
import pytest

from .tenant_setup import TenantContext

COMMUNITY_FLAGS = {
    "parallel_branches", "eval_system", "webhook_trigger", "cron_trigger",
    "mcp_server", "community_library", "saved_views", "polling_trigger",
    "agent_signal_trigger", "helm_deployment",
}
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

    resp = await tenant_client.get("/api/v1/viewmodel")
    assert resp.status_code == 200
    vm = resp.json()

    feature_flags = vm.get("feature_flags", vm.get("flags", {}))
    # Community tenants should not have team-tier features enabled
    for flag in TEAM_ONLY_FLAGS:
        assert not feature_flags.get(flag, False), f"Community tenant should not have {flag} enabled"


@pytest.mark.asyncio
async def test_team_can_access_team_features(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    if tenant.plan_id != "team":
        pytest.skip("Only relevant for team-tier tenants")

    resp = await tenant_client.get("/api/v1/viewmodel")
    assert resp.status_code == 200
    vm = resp.json()

    feature_flags = vm.get("feature_flags", vm.get("flags", {}))
    for flag in TEAM_ONLY_FLAGS:
        assert feature_flags.get(flag, False), f"Team tenant should have {flag} enabled"


@pytest.mark.asyncio
async def test_no_tenant_has_v1_features(tenant_client: httpx.AsyncClient) -> None:
    """No staging tenant has a license key, so v1+ should be locked."""
    resp = await tenant_client.get("/api/v1/viewmodel")
    assert resp.status_code == 200
    vm = resp.json()

    feature_flags = vm.get("feature_flags", vm.get("flags", {}))
    for flag in V1_FLAGS | V2_FLAGS:
        assert not feature_flags.get(flag, False), f"Feature {flag} should not be enabled without license"


@pytest.mark.asyncio
async def test_license_endpoint_reflects_tier(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    resp = await tenant_client.get("/api/v1/license")
    assert resp.status_code == 200
    data = resp.json()

    # Staging has no license key, so it falls back to community/team via org.plan_id
    tier_info = data.get("tier", data.get("plan_id", ""))
    assert tier_info == tenant.plan_id, f"Expected tier={tenant.plan_id}, got {tier_info}"


@pytest.mark.asyncio
async def test_community_feature_gate_shows_locked(tenant_client: httpx.AsyncClient, tenant: TenantContext) -> None:
    """Community-tier FeatureGate wrappers should show locked state for team features."""
    if tenant.plan_id != "community":
        pytest.skip("Only meaningful for community tier")

    resp = await tenant_client.get("/api/v1/viewmodel")
    assert resp.status_code == 200
    vm = resp.json()

    nav = vm.get("navigation", vm.get("nav", {}))
    admin_section = nav.get("admin", [])
    # Admin features like spend limits, observability should not appear
    admin_names = {item.get("name", "") for item in admin_section}
    assert "Spend Limits" not in admin_names
    assert "Observability" not in admin_names
