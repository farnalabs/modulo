"""Unit tests for GET /api/v1/plan — the caller-scoped plan bootstrap (FAR-1806).

The endpoint exists so any authenticated member of their own org can read the
effective plan/tier/feature state without the instance-admin-only
``/api/v1/admin/feature-flags`` / ``/api/v1/admin/license`` endpoints: a
non-admin caller must get 200, the payload must carry the FULL flag set
(inactive flags present with ``currently_active: false``), and the org
``feature_overrides`` overlay must be applied — the two gaps that made the
obvious ``viewmodel/current`` shortcut unusable.

Covers the endpoint's failure ladder (ProgrammingError -> 501,
SQLAlchemyError -> 503, HTTPException re-raised, Exception -> 500), every
rung of ``_resolve_license_expiry``'s licence ladder, and the malformed-
settings branches of ``_org_feature_overrides``.
"""

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.viewmodel import _org_feature_overrides, _resolve_license_expiry
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlag
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    LicenseData,
    clear_license,
    set_public_key,
    store_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

# Known test keypair: signed payloads verify only while set_public_key(_TEST_PUB)
# is in effect (the autouse fixture below manages that per test).
_TEST_KP = generate_keypair()
_TEST_PRIV = _TEST_KP["private_key"]
_TEST_PUB = _TEST_KP["public_key"]

_VIEWER = AuthenticatedPrincipal(
    username="viewer",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="viewer",
    is_system_admin=False,
)
_ORGLESS_SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="sysadmin",
    organisation_id=None,
    account_id=_USER_ID,
    org_role="",
    is_system_admin=True,
)


@pytest.fixture(autouse=True)
def _reset_license_state() -> Generator[None, None, None]:
    """In-memory licence + configured public key are module globals; reset per test."""
    clear_license()
    set_public_key(_TEST_PUB)
    yield
    clear_license()
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


def _make_settings(modulo_dev_mode: bool = True, modulo_license_key: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_dev_mode=modulo_dev_mode,
        modulo_license_key=modulo_license_key,
    )


def _sign_license_payload(payload: dict[str, Any], private_key: str = _TEST_PRIV) -> str:
    sig_hex = sign_primitive(payload, private_key)
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig_hex)).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "team",
        "features": ["sso"],
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
        "org_id": "test-org",
    }
    payload.update(overrides)
    return payload


def _in_memory_license(**overrides: Any) -> LicenseData:
    payload: dict[str, Any] = {
        "tier": "team",
        "features": ["sso"],
        "expires_at": (datetime.now(UTC) + timedelta(days=180)).isoformat(),
        "org_id": "in-memory-org",
        "raw_payload": {},
        "raw_key": "in-memory-key",
    }
    payload.update(overrides)
    return LicenseData(**payload)


def _make_org(settings_json: dict[str, object] | None = None) -> MagicMock:
    org = MagicMock()
    org.id = _ORG_ID
    org.name = "Test Org"
    org.settings_json = settings_json if settings_json is not None else {}
    org.daily_spend_limit = None
    return org


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _flag(name: str, *, active: bool, tier: str = "community") -> FeatureFlag:
    return FeatureFlag(
        name=name,
        description=f"{name} description",
        tier=tier,
        currently_active=active,
    )


def _plan_context(flags: list[FeatureFlag], tier: str = "community") -> MagicMock:
    ctx = MagicMock()
    ctx.list_all_features = MagicMock(return_value=flags)
    ctx.tier = MagicMock(return_value=tier)
    return ctx


@contextmanager
def _plan_client_as(
    principal: AuthenticatedPrincipal,
    *,
    authenticated: bool = True,
) -> Generator[TestClient, None, None]:
    """A TestClient wired like the viewmodel unit tests, as ``principal``.

    ``authenticated=False`` leaves ``get_current_user`` un-overridden so the
    real (failing) auth dependency answers the request.
    """
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _patch_plan_deps(org: MagicMock | None, plan_ctx: MagicMock) -> object:
    """Patch the route's DB seams: org load, plan resolution, RLS setters."""
    return patch.multiple(
        "modulo.api.routes.viewmodel",
        get_organisation=AsyncMock(return_value=org),
        resolve_plan_context=AsyncMock(return_value=plan_ctx),
        set_rls_org=AsyncMock(),
        set_rls_user_context=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/plan
# ---------------------------------------------------------------------------


def test_plan_bootstrap_200_for_non_system_admin() -> None:
    """The whole point: a viewer (not a system admin) reads the plan, not a 403."""
    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True), _flag("mobile_sidebar_rail", active=False)])

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    body = resp.json()
    assert body["license"]["tier"] == "community"
    assert body["dev_mode"] is True
    names = [f["name"] for f in body["flags"]]
    assert "eval_system" in names
    assert "mobile_sidebar_rail" in names


def test_plan_bootstrap_includes_inactive_flags_with_currently_active_false() -> None:
    """Inactive flags must be PRESENT (an active-only payload is the bug being fixed)."""
    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True), _flag("mobile_sidebar_rail", active=False)])

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    mobile = by_name["mobile_sidebar_rail"]
    assert mobile["currently_active"] is False
    assert mobile["description"]
    assert mobile["tier"] == "community"
    assert by_name["eval_system"]["currently_active"] is True


def test_plan_bootstrap_applies_org_feature_overrides() -> None:
    """Org overrides flip BOTH directions: default-off -> on, default-on -> off."""
    org = _make_org(
        settings_json={
            "feature_overrides": {"mobile_sidebar_rail": True, "analytics_page": False},
        }
    )
    plan_ctx = _plan_context(
        [
            _flag("mobile_sidebar_rail", active=False),
            _flag("analytics_page", active=True),
        ]
    )

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    assert by_name["mobile_sidebar_rail"]["currently_active"] is True
    assert by_name["analytics_page"]["currently_active"] is False


def test_plan_bootstrap_ignores_non_bool_and_unknown_overrides() -> None:
    """Only bool entries naming KNOWN flags overlay — junk settings never flip a flag."""
    org = _make_org(
        settings_json={
            "feature_overrides": {"analytics_page": 1, "no_such_flag": True},
        }
    )
    plan_ctx = _plan_context([_flag("analytics_page", active=True)])

    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(org, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    # 1 is not a bool -> not an override; unknown names are ignored entirely.
    assert by_name["analytics_page"]["currently_active"] is True
    assert "no_such_flag" not in by_name


def test_plan_bootstrap_license_block_reports_expiry() -> None:
    """The licence view (expires_at/org_id) rides the same payload so the client
    can drop its admin-only /admin/license call."""
    from modulo.core.license import LicenseData

    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True)], tier="team")
    licence = LicenseData(
        tier="team",
        features=["sso"],
        expires_at="2026-12-31T23:59:59Z",
        org_id="Acme Corp",
        raw_payload={},
        raw_key="signed-key",
    )

    with (
        _plan_client_as(_VIEWER) as client,
        _patch_plan_deps(org, plan_ctx),
        patch("modulo.core.license.get_license", return_value=licence),
    ):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    license_block = resp.json()["license"]
    assert license_block["tier"] == "team"
    assert license_block["expires_at"] == "2026-12-31T23:59:59Z"
    assert license_block["org_id"] == "Acme Corp"


def test_plan_bootstrap_unauthenticated_returns_4xx() -> None:
    with _plan_client_as(_VIEWER, authenticated=False) as client:
        resp = client.get("/api/v1/plan")
    assert resp.status_code in (401, 403)


def test_plan_bootstrap_orgless_system_admin_still_resolves() -> None:
    """A system admin with no org still gets a payload — no overlay, no crash."""
    plan_ctx = _plan_context([_flag("mobile_sidebar_rail", active=False)])

    with _plan_client_as(_ORGLESS_SYSTEM_ADMIN) as client, _patch_plan_deps(None, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()["flags"]}
    assert by_name["mobile_sidebar_rail"]["currently_active"] is False


# ---------------------------------------------------------------------------
# Failure ladder (ProgrammingError -> 501, SQLAlchemyError -> 503,
# HTTPException re-raised, Exception -> 500) — review finding, head 4e203e3ba
# ---------------------------------------------------------------------------


def test_plan_bootstrap_programming_error_maps_to_501() -> None:
    """A schema-shape failure (ProgrammingError) answers 501 + migrations hint."""
    org = _make_org()
    plan_ctx = _plan_context([_flag("eval_system", active=True)])

    with (
        _plan_client_as(_VIEWER) as client,
        patch.multiple(
            "modulo.api.routes.viewmodel",
            get_organisation=AsyncMock(return_value=org),
            resolve_plan_context=AsyncMock(return_value=plan_ctx),
            set_rls_org=AsyncMock(side_effect=ProgrammingError("stmt", "params", "orig")),
            set_rls_user_context=AsyncMock(),
        ),
    ):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_plan_bootstrap_sqlalchemy_error_maps_to_503() -> None:
    """A transient DB failure (SQLAlchemyError) answers 503 with the DB-error detail."""
    org = _make_org()
    db_error = OperationalError("stmt", "params", Exception("connection refused"))

    with (
        _plan_client_as(_VIEWER) as client,
        patch.multiple(
            "modulo.api.routes.viewmodel",
            get_organisation=AsyncMock(return_value=org),
            resolve_plan_context=AsyncMock(side_effect=db_error),
            set_rls_org=AsyncMock(),
            set_rls_user_context=AsyncMock(),
        ),
    ):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Database error while loading plan data."


def test_plan_bootstrap_http_exception_reraised_unchanged() -> None:
    """A 404 raised by org resolution passes through the ladder untouched."""
    plan_ctx = _plan_context([_flag("eval_system", active=True)])

    # get_organisation -> None: the viewer HAS an org id, so _resolve_viewmodel_org
    # raises HTTPException(404) inside the try — the ladder must re-raise it.
    with _plan_client_as(_VIEWER) as client, _patch_plan_deps(None, plan_ctx):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Organisation not found"


def test_plan_bootstrap_unexpected_error_maps_to_500() -> None:
    """A generic exception answers 500 with the plan-data failure detail."""
    org = _make_org()

    with (
        _plan_client_as(_VIEWER) as client,
        patch.multiple(
            "modulo.api.routes.viewmodel",
            get_organisation=AsyncMock(return_value=org),
            resolve_plan_context=AsyncMock(side_effect=RuntimeError("boom")),
            set_rls_org=AsyncMock(),
            set_rls_user_context=AsyncMock(),
        ),
    ):
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to load plan data."


# ---------------------------------------------------------------------------
# _resolve_license_expiry — every licence-ladder rung (review finding)
# ---------------------------------------------------------------------------


class TestResolveLicenseExpiryLadder:
    """Each rung of the shared ladder, observed through ``_resolve_license_expiry``:

    1. org key valid -> its (expires_at, org_id)
    2. org key invalid -> falls through to the in-memory licence
    3. in-memory licence (no/absent org key) -> its view
    4. env-var key valid -> its view
    5. nothing valid -> (None, None)
    """

    def test_org_key_valid_returns_its_view(self) -> None:
        key = _sign_license_payload(_valid_payload(expires_at="2027-06-30T00:00:00+00:00", org_id="org-run"))
        org = _make_org(settings_json={"license_key": key})

        expires_at, org_id = _resolve_license_expiry(_make_settings(), org)

        assert expires_at == "2027-06-30T00:00:00+00:00"
        assert org_id == "org-run"

    def test_invalid_org_key_falls_through_to_in_memory_licence(self) -> None:
        org = _make_org(settings_json={"license_key": "not-a-valid-key"})
        store_license("in-memory-key", _in_memory_license())

        expires_at, org_id = _resolve_license_expiry(_make_settings(), org)

        assert expires_at is not None
        assert org_id == "in-memory-org"

    def test_non_dict_org_settings_skip_the_org_rung(self) -> None:
        org = MagicMock()
        org.settings_json = "junk-not-a-dict"
        store_license("in-memory-key", _in_memory_license())

        expires_at, org_id = _resolve_license_expiry(_make_settings(), org)

        assert expires_at is not None
        assert org_id == "in-memory-org"

    def test_org_without_key_falls_to_in_memory_licence(self) -> None:
        org = _make_org(settings_json={})
        store_license("in-memory-key", _in_memory_license())

        expires_at, org_id = _resolve_license_expiry(_make_settings(), org)

        assert expires_at is not None
        assert org_id == "in-memory-org"

    def test_env_var_key_used_when_no_org_and_no_in_memory_licence(self) -> None:
        key = _sign_license_payload(_valid_payload(expires_at="2027-01-01T00:00:00+00:00", org_id="env-run"))

        expires_at, org_id = _resolve_license_expiry(_make_settings(modulo_license_key=key), None)

        assert expires_at == "2027-01-01T00:00:00+00:00"
        assert org_id == "env-run"

    def test_invalid_env_key_resolves_to_none_none(self) -> None:
        org = _make_org(settings_json={})

        expires_at, org_id = _resolve_license_expiry(_make_settings(modulo_license_key="not-a-valid-key"), org)

        assert expires_at is None
        assert org_id is None

    def test_nothing_resolves_returns_none_none(self) -> None:
        assert _resolve_license_expiry(_make_settings(), None) == (None, None)

        expires_at, org_id = _resolve_license_expiry(_make_settings(), _make_org())
        assert expires_at is None
        assert org_id is None

    def test_empty_expiry_and_org_id_normalise_to_none(self) -> None:
        """A valid licence without expiry/org fields reports (None, None), not ("", "")."""
        key = _sign_license_payload({"tier": "community", "features": []})
        org = _make_org(settings_json={"license_key": key})

        expires_at, org_id = _resolve_license_expiry(_make_settings(), org)

        assert expires_at is None
        assert org_id is None

    def test_in_memory_licence_without_expiry_normalises_to_none(self) -> None:
        store_license(
            "in-memory-key",
            _in_memory_license(expires_at="", org_id=""),
        )

        expires_at, org_id = _resolve_license_expiry(_make_settings(), None)

        assert expires_at is None
        assert org_id is None


# ---------------------------------------------------------------------------
# _org_feature_overrides — malformed-settings branches (review finding)
# ---------------------------------------------------------------------------


def test_org_feature_overrides_without_org_is_empty() -> None:
    assert not _org_feature_overrides(None)


def test_org_feature_overrides_non_dict_settings_is_empty() -> None:
    org = MagicMock()
    org.settings_json = None
    assert not _org_feature_overrides(org)

    org.settings_json = "junk"
    assert not _org_feature_overrides(org)


def test_org_feature_overrides_non_dict_override_map_is_empty() -> None:
    """A malformed feature_overrides value (list, scalar) is junk, not an overlay."""
    org = _make_org(settings_json={"feature_overrides": ["eval_system"]})
    assert not _org_feature_overrides(org)

    org = _make_org(settings_json={"feature_overrides": "eval_system"})
    assert not _org_feature_overrides(org)


def test_org_feature_overrides_keeps_bool_entries_only() -> None:
    org = _make_org(
        settings_json={
            "feature_overrides": {"analytics_page": True, "zero": 0, "text": "yes", "one": 1},
        }
    )

    overrides = _org_feature_overrides(org)

    assert overrides == {"analytics_page": True}
    assert "one" not in overrides
