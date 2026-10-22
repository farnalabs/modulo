"""FAR-2059: a lapsed licence is never a lockout.

INVARIANT — there is no licence state in which no administrator can reach the
licence screen:

* the pre-auth authentication gate must not deny the IdP hand-off on a lapsed
  licence (identity is not yet known there, so it cannot be role-scoped — it
  must allow every sign-in, admin or not); and
* the licence-recovery endpoint itself must stay reachable with an
  admin-level principal and must answer "Community" rather than failing.

These are the two halves of the recovery path. The real-session proof (login
then ``GET /api/v1/admin/license``) lives in
``tests/integration/test_sso_licence_degradation.py``.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import (
    _get_engine,
    get_db_session,
    get_plan_context,
    require_feature_anonymous,
    resolve_anonymous_plan_context,
)
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.core.license import (
    LICENCE_GRACE_DAYS,
    clear_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEST_KP = generate_keypair()


def _licence(*, days_from_now: int) -> str:
    """A real signed Team key whose ``expires_at`` is ``days_from_now`` away."""
    from modulo.core import license as license_mod

    license_mod._configured_public_key_hex = _TEST_KP["public_key"]
    payload = {
        "tier": "team",
        "features": ["sso"],
        "expires_at": (datetime.now(UTC) + timedelta(days=days_from_now)).isoformat(),
        "org_id": str(_ORG_ID),
    }
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = (
        base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, _TEST_KP["private_key"]))).decode().rstrip("=")
    )
    return f"{payload_b64}.{sig_b64}"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="",
        modulo_multi_org_enabled=True,
        # Empty URL makes the licence-status cache reads/writes fail fast and
        # be swallowed, so each test exercises the DB/resolution path.
        redis_url="",
    )


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Shape the reads the plan/tenant/authz paths issue so they see empty
    # results instead of un-awaited MagicMock values.
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture(autouse=True)
def _restore_license_globals() -> Generator[None, None, None]:
    """Save AND restore the licence globals (no key leak into later tests)."""
    from modulo.core import license as license_mod

    saved_key = license_mod._configured_public_key_hex
    yield
    license_mod._configured_public_key_hex = saved_key
    license_mod._current_license = None


@pytest.fixture(autouse=True)
def _reset_feature_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FeatureFlagRegistry, "_overrides", {})


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = _make_mock_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = False
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "days_from_now",
    [-(LICENCE_GRACE_DAYS + 1), -(LICENCE_GRACE_DAYS * 100)],
    ids=["past-grace", "long-lapsed"],
)
def test_admin_can_reach_the_licence_endpoint_when_licence_is_lapsed(
    admin_client: TestClient, days_from_now: int
) -> None:
    """The recovery endpoint answers 200 (Community), never a licence 402.

    This is the invariant: if the licence screen ever starts licence-gating
    itself, an expired instance has no way to install a new key and every
    admin is locked out permanently.
    """
    from modulo.core import license as license_mod

    clear_license()
    org_key = _licence(days_from_now=days_from_now)
    org = MagicMock()
    org.id = _ORG_ID
    org.settings_json = {"license_key": org_key}

    with patch("modulo.api.routes.admin_license.get_organisation", new=AsyncMock(return_value=org)):
        resp = admin_client.get("/api/v1/admin/license")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_license"] is False
    assert body["tier"] == "community"
    assert license_mod._configured_public_key_hex == _TEST_KP["public_key"]


@pytest.mark.parametrize(
    "days_from_now",
    [-(LICENCE_GRACE_DAYS + 1), -1],
    ids=["past-grace", "inside-grace"],
)
async def test_authentication_entry_point_is_never_gated_by_licence_state(days_from_now: int) -> None:
    """Identity exchange is never blocked — the gate is role-blind by design.

    A lapsed licence must not deny the IdP hand-off: there is no identity yet
    at that point, so denying it is exactly how a lapse becomes a lockout.
    """
    clear_license()
    settings = _settings().model_copy(update={"modulo_license_key": _licence(days_from_now=days_from_now)})
    ctx = await resolve_anonymous_plan_context(settings, _make_mock_session())

    gate = require_feature_anonymous("sso", allow_degraded_authentication=True)
    assert gate.dependency(ctx=ctx) is None


async def test_past_grace_plan_has_no_team_capability() -> None:
    """The other half: a lapsed key grants no Team capability post-auth."""
    clear_license()
    settings = _settings().model_copy(update={"modulo_license_key": _licence(days_from_now=-(LICENCE_GRACE_DAYS + 1))})
    ctx = await resolve_anonymous_plan_context(settings, _make_mock_session())

    assert ctx.has_license_key() is False
    assert ctx.tier() == "community"
    assert ctx.feature_enabled("sso") is False
