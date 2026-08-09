"""Unit tests for the admin billing overview ``plan_tier`` derivation.

Regression for the PR #854 review finding: ``plan_tier`` was derived from the
bare ``plan_id`` (a ``plan_id="team"`` org with no license was displayed as
team tier). It now uses ``resolve_plan_context`` so ``plan_tier`` reflects the
LICENSED tier — community when no valid signed license is present, even when
``plan_id`` says team.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.routes.admin import BillingOverviewResponse, admin_billing_overview
from modulo.auth.jwt import TenantPrincipal
from modulo.core.license import LicenseData, LicenseValidation


def _license_data(tier: str = "team") -> LicenseData:
    return LicenseData(
        tier=tier,
        features=["sso"],
        expires_at="",
        org_id="test-org",
        raw_payload={},
        raw_key="",
    )


def _valid_validation(tier: str = "team") -> LicenseValidation:
    return LicenseValidation(valid=True, license_data=_license_data(tier))


def _invalid_validation() -> LicenseValidation:
    return LicenseValidation(valid=False, error="bad key")


def _org(plan_id: str = "team") -> MagicMock:
    org = MagicMock()
    org.plan_id = plan_id
    org.settings_json = {}
    org.daily_spend_limit = None
    return org


def _principal(org_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@modulo.run",
        organisation_id=org_id,
        account_id=uuid.uuid4(),
        org_role="admin",
        is_system_admin=False,
    )


def _session() -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    result = MagicMock()
    result.scalar.return_value = None
    session.execute.return_value = result
    return session


async def _invoke(org: MagicMock, *, license_key: str = "") -> BillingOverviewResponse:
    org_id = uuid.uuid4()
    settings = MagicMock()
    settings.modulo_license_key = license_key
    with (
        patch("modulo.api.routes.admin.set_rls_org"),
        patch("modulo.api.routes.admin.get_organisation", new_callable=AsyncMock, return_value=org),
        patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
        patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
        patch("modulo.core.license.get_license", return_value=None),
        patch("modulo.core.license.parse_and_verify", return_value=_invalid_validation()),
    ):
        return await admin_billing_overview(
            current_user=_principal(org_id),
            session=_session(),
            settings=settings,
        )


class TestBillingOverviewPlanTier:
    async def test_team_plan_id_without_license_reports_community(self) -> None:
        """plan_id='team' + no license -> plan_tier 'community' (raw plan_id kept)."""
        resp = await _invoke(_org("team"))
        assert resp.plan_id == "team"
        assert resp.plan_tier == "community"

    async def test_custom_plan_id_without_license_reports_community(self) -> None:
        resp = await _invoke(_org("custom-plan"))
        assert resp.plan_id == "custom-plan"
        assert resp.plan_tier == "community"

    async def test_team_plan_id_with_env_license_reports_team(self) -> None:
        org = _org("team")
        org_id = uuid.uuid4()
        settings = MagicMock()
        settings.modulo_license_key = "env-key"
        session = _session()
        with (
            patch("modulo.api.routes.admin.set_rls_org"),
            patch("modulo.api.routes.admin.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=_valid_validation(tier="team")),
        ):
            resp = await admin_billing_overview(
                current_user=_principal(org_id),
                session=session,
                settings=settings,
            )
        assert resp.plan_id == "team"
        assert resp.plan_tier == "team"

    async def test_community_plan_id_reports_community(self) -> None:
        resp = await _invoke(_org("community"))
        assert resp.plan_id == "community"
        assert resp.plan_tier == "community"
