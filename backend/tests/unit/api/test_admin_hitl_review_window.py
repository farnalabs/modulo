"""FAR-1257: the admin org HITL review-window endpoint contract.

Mirrors ``test_admin_sandbox_concurrency`` — same org ``settings_json``
substrate, same ``SELECT ... FOR UPDATE`` read-modify-write, same admin-only
gate — but the VALUE is the middle layer of the review-window chain:

    pipeline override > ORG DEFAULT (this endpoint) > instance/env default

so the interesting cases are the ones that shape that chain: an absent/malformed
key must read as "inherit", ``null`` must CLEAR (not store a meaningless
zero-window — the safety net has no "0 = disabled"), the envelope is enforced
at the boundary, and no other ``settings_json`` key may be clobbered.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal

ORG_ID = uuid4()
USER_ID = uuid4()
_PATH = "/api/v1/admin/org/hitl-review-window"
_KEY = "hitl_review_window_seconds"


def _admin(role: str = "admin") -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@test",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )


@pytest.fixture
def org_settings():
    return {}


@pytest.fixture
def mock_session(org_settings):
    """Mock session whose org row exposes a mutable settings_json."""
    org = MagicMock()
    org.id = ORG_ID
    org.settings_json = org_settings

    result = MagicMock()
    result.scalar_one_or_none.return_value = org

    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.execute.return_value = result
    session.flush = AsyncMock()
    return session


def _make_client(mock_session, role: str):
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    async def _override_tenant() -> TenantPrincipal:
        return _admin(role)

    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant_user] = _override_tenant
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def client_admin(mock_session):
    client = _make_client(mock_session, role="admin")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    client = _make_client(mock_session, role="viewer")
    yield client
    app.dependency_overrides.clear()


class TestRead:
    @pytest.mark.anyio
    async def test_absent_key_reads_as_inherit(self, client_admin):
        """No org default -> ``None`` + ``is_default=True``: the UI renders the
        EFFECTIVE instance default, never implies an org setting exists."""
        resp = await client_admin.get(_PATH)
        assert resp.status_code == 200
        assert resp.json() == {_KEY: None, "is_default": True}

    @pytest.mark.anyio
    async def test_explicit_value_reads_back(self, client_admin, org_settings):
        org_settings[_KEY] = 7200
        resp = await client_admin.get(_PATH)
        assert resp.status_code == 200
        assert resp.json() == {_KEY: 7200, "is_default": False}

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "malformed",
        ["120", True, None, ["120"], 3.5],
    )
    async def test_malformed_stored_value_reads_as_inherit(
        self,
        client_admin,
        org_settings,
        malformed,
    ):
        """A hand-edited/non-int value must degrade to "no org default" rather
        than raising or masquerading as a window (the resolution helper then
        falls through to the instance default)."""
        org_settings[_KEY] = malformed
        resp = await client_admin.get(_PATH)
        assert resp.status_code == 200
        assert resp.json() == {_KEY: None, "is_default": True}

    @pytest.mark.anyio
    async def test_other_settings_keys_are_irrelevant(self, client_admin, org_settings):
        org_settings["license_key"] = "abc"
        org_settings["sandbox_concurrency_limit"] = 4
        resp = await client_admin.get(_PATH)
        assert resp.status_code == 200
        assert resp.json() == {_KEY: None, "is_default": True}

    @pytest.mark.anyio
    async def test_viewer_forbidden_on_get(self, client_viewer):
        resp = await client_viewer.get(_PATH)
        assert resp.status_code == 403


class TestWrite:
    @pytest.mark.anyio
    async def test_put_sets_the_org_default(self, client_admin, mock_session):
        resp = await client_admin.put(_PATH, json={_KEY: 4500})
        assert resp.status_code == 200
        assert resp.json() == {_KEY: 4500, "is_default": False}
        org = mock_session.execute.return_value.scalar_one_or_none.return_value
        assert org.settings_json[_KEY] == 4500

    @pytest.mark.anyio
    async def test_put_uses_row_lock_against_concurrent_writers(self, client_admin, mock_session):
        """The settings read-modify-write must SELECT ... FOR UPDATE so a
        concurrent settings writer (sandbox_concurrency, retention, ...) cannot
        be dropped between our read and flush."""
        resp = await client_admin.put(_PATH, json={_KEY: 4500})
        assert resp.status_code == 200
        stmt = mock_session.execute.await_args_list[0].args[0]
        assert "FOR UPDATE" in str(stmt)

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", [60, 4500, 604800])
    async def test_put_accepts_the_shipped_envelope(self, client_admin, value):
        """1 min .. 7 days inclusive — the SAME bounds the Pydantic pipeline
        fields and the ``ck_pipelines_hitl_review_window`` CHECK enforce."""
        resp = await client_admin.put(_PATH, json={_KEY: value})
        assert resp.status_code == 200
        assert resp.json() == {_KEY: value, "is_default": False}

    @pytest.mark.anyio
    @pytest.mark.parametrize("bad", [0, 1, 59, 604801, -100])
    async def test_put_rejects_out_of_range(self, client_admin, bad):
        """``0`` is rejected, not stored: there is NO "0 = disabled" — the
        terminalizer safety net stays unconditional at every layer."""
        resp = await client_admin.put(_PATH, json={_KEY: bad})
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_put_omitted_field_is_422_but_explicit_null_clears(
        self,
        client_admin,
        mock_session,
    ):
        """An OMITTED ``hitl_review_window_seconds`` must NOT be mistaken for a
        clear: with ``default=None`` a partial PUT silently wiped the org
        default. The field is required, so an absent field 422s before the
        handler runs and the stored key is untouched; only an explicit
        ``null`` performs the destructive clear (the frontend always sends the
        field)."""
        org = mock_session.execute.return_value.scalar_one_or_none.return_value
        org.settings_json = {_KEY: 4500, "license_key": "license-abc"}

        resp = await client_admin.put(_PATH, json={})
        assert resp.status_code == 422
        assert org.settings_json == {_KEY: 4500, "license_key": "license-abc"}

        resp = await client_admin.put(_PATH, json={_KEY: None})
        assert resp.status_code == 200
        assert resp.json() == {_KEY: None, "is_default": True}
        assert _KEY not in org.settings_json
        assert org.settings_json["license_key"] == "license-abc"

    @pytest.mark.anyio
    async def test_put_null_clears_the_key(self, client_admin, mock_session):
        """``null`` REMOVES the key (inherit the instance default) — it must
        never be stored as a null window the resolver would have to guess at.

        The route writes a FRESH dict onto the org row (read-modify-write), so
        the assertion reads the mock's ``org.settings_json``, not the fixture's
        original dict."""
        org = mock_session.execute.return_value.scalar_one_or_none.return_value
        org.settings_json = {_KEY: 4500, "license_key": "license-abc"}
        resp = await client_admin.put(_PATH, json={_KEY: None})
        assert resp.status_code == 200
        assert resp.json() == {_KEY: None, "is_default": True}
        assert _KEY not in org.settings_json
        assert org.settings_json["license_key"] == "license-abc"

    @pytest.mark.anyio
    async def test_put_merge_preserves_other_settings(self, client_admin, mock_session):
        """Do not clobber other ``settings_json`` keys — the merge is a dict
        copy of the WHOLE blob, with one key written/removed."""
        org = mock_session.execute.return_value.scalar_one_or_none.return_value
        org.settings_json = {
            "license_key": "license-abc",
            "retention_days": 30,
            "sandbox_concurrency_limit": 4,
        }
        resp = await client_admin.put(_PATH, json={_KEY: 1800})
        assert resp.status_code == 200
        assert org.settings_json["license_key"] == "license-abc"
        assert org.settings_json["retention_days"] == 30
        assert org.settings_json["sandbox_concurrency_limit"] == 4
        assert org.settings_json[_KEY] == 1800

    @pytest.mark.anyio
    async def test_viewer_forbidden_on_put(self, client_viewer):
        resp = await client_viewer.put(_PATH, json={_KEY: 4500})
        assert resp.status_code == 403
