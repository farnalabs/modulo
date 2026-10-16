"""FAR-1257: the admin org HITL review-window endpoint contract.

Mirrors ``test_admin_sandbox_concurrency`` — same org ``settings_json``
substrate, same single-statement atomic settings patch (FAR-1832; previously a
``SELECT ... FOR UPDATE`` read-modify-write), same admin-only gate — but the
VALUE is the middle layer of the review-window chain:

    pipeline override > ORG DEFAULT (this endpoint) > instance/env default

so the interesting cases are the ones that shape that chain: an absent/malformed
key must read as "inherit", ``null`` must CLEAR (not store a meaningless
zero-window — the safety net has no "0 = disabled"), the envelope is enforced
at the boundary, and no other ``settings_json`` key may be clobbered.
"""

from unittest.mock import AsyncMock, MagicMock, patch
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
    # Real SQLAlchemy sessions expose ``info`` as a dict; the route binds RLS
    # via ``set_rls_org`` which stores the org id here on generic backends, and
    # the FAR-1830/FAR-1832 patch helper reads it back through its strict
    # fail-closed tenant guard.
    session.info = {}
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
    async def test_put_patches_only_its_own_key_without_prelatching_the_org_row(self, client_admin, mock_session):
        """FAR-1832: the writer delegates to the atomic settings patch with ONLY
        its own key and issues no ``FOR UPDATE`` org select of its own.

        Fails against the old shape two ways: the old code never called the
        patch helper (``captured`` stays empty), and it executed a
        ``SELECT ... FOR UPDATE`` on the org row before writing. The audit
        append (which legitimately locks the per-org audit-chain head) is
        stubbed out so the statement scan sees only the writer's own statements.
        """
        captured: dict[str, object] = {}

        async def _fake_patch(session, organisation_id, patch=None, delete_keys=None, **kwargs):
            captured["organisation_id"] = organisation_id
            captured["patch"] = patch
            captured["delete_keys"] = delete_keys
            return dict(patch or {})

        with (
            patch("modulo.api.routes.admin.update_org_settings_patch", new=_fake_patch),
            patch("modulo.api.routes.admin._record_org_audit", new=AsyncMock()),
        ):
            resp = await client_admin.put(_PATH, json={_KEY: 4500})
        assert resp.status_code == 200
        assert captured["organisation_id"] == ORG_ID
        assert captured["patch"] == {_KEY: 4500}
        for call in mock_session.execute.await_args_list:
            assert "FOR UPDATE" not in str(call.args[0]), f"writer must not pre-lock the org row: {call.args[0]}"

    @pytest.mark.anyio
    async def test_put_null_clears_via_delete_keys_not_a_stored_null(self, client_admin, mock_session):
        """FAR-1832: an explicit ``null`` must go through the patch helper's
        ``delete_keys`` form — the key is REMOVED (inherit the instance
        default), never stored as a null window."""
        captured: dict[str, object] = {}

        async def _fake_patch(session, organisation_id, patch=None, delete_keys=None, **kwargs):
            captured["patch"] = patch
            captured["delete_keys"] = delete_keys
            return dict(patch or {})

        with (
            patch("modulo.api.routes.admin.update_org_settings_patch", new=_fake_patch),
            patch("modulo.api.routes.admin._record_org_audit", new=AsyncMock()),
        ):
            resp = await client_admin.put(_PATH, json={_KEY: None})
        assert resp.status_code == 200
        assert captured["delete_keys"] == [_KEY]
        assert not captured["patch"]

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

        The patch helper refreshes the mock's org row with the merged mapping,
        so the assertion reads ``org.settings_json`` after the write."""
        org = mock_session.execute.return_value.scalar_one_or_none.return_value
        org.settings_json = {_KEY: 4500, "license_key": "license-abc"}
        resp = await client_admin.put(_PATH, json={_KEY: None})
        assert resp.status_code == 200
        assert resp.json() == {_KEY: None, "is_default": True}
        assert _KEY not in org.settings_json
        assert org.settings_json["license_key"] == "license-abc"

    @pytest.mark.anyio
    async def test_put_merge_preserves_other_settings(self, client_admin, mock_session):
        """Do not clobber other ``settings_json`` keys — the patch composes at
        the top level, writing or removing exactly this one key."""
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
    async def test_put_missing_org_row_is_404(self, client_admin, mock_session):
        """A PUT whose org row cannot be found (e.g. hard-deleted between auth
        and the row lock) must 404 rather than write onto a phantom row."""
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        resp = await client_admin.put(_PATH, json={_KEY: 4500})
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_viewer_forbidden_on_put(self, client_viewer):
        resp = await client_viewer.put(_PATH, json={_KEY: 4500})
        assert resp.status_code == 403
