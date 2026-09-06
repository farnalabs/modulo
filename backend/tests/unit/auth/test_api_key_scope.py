"""FAR-620 Phase 1: API-key caller-scope minting + serialisation.

Covers the ``scope`` axis on ``OrgApiKey``:
1. ``create_api_key`` explicit mint-context parameter (default 'org',
   'user' stamps, unknown scopes raise).
2. ``mint_run_api_key`` pins scope='org' (run-scoped keys are org-level
   machine identities — denied caller-scoped tools).
3. ``_serialize_key`` round-trips the scope.
4. ``update_api_key`` accepts NO scope parameter (immutability post-mint).
5. The ORM model carries the column + CHECK constraint in lockstep with
   migration 0178.
6. The live-role clamp is INDEPENDENT of key_scope (ADR-017 ceiling is
   reused verbatim — a user-scoped key never widens it).
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.auth.api_key import (
    KEY_SCOPES,
    ApiKeyScopeError,
    create_api_key,
    mint_run_api_key,
    validate_api_key,
)
from modulo.auth.permissions import _clamp_role
from modulo.db.models.api_key import OrgApiKey

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


class TestCreateApiKeyScope:
    @pytest.mark.asyncio
    async def test_default_scope_is_org(self) -> None:
        session = _mock_session()
        key, full_key = await create_api_key(
            session,
            org_id=_ORG_ID,
            name="k",
            role="operator",
            account_id=_ACCOUNT_ID,
        )
        assert key.scope == "org"
        assert full_key.startswith("mk_")
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_user_scope_stamps(self) -> None:
        session = _mock_session()
        key, _ = await create_api_key(
            session,
            org_id=_ORG_ID,
            name="user:duncan",
            role="operator",
            account_id=_ACCOUNT_ID,
            scope="user",
        )
        assert key.scope == "user"

    @pytest.mark.asyncio
    async def test_invalid_scope_raises(self) -> None:
        session = _mock_session()
        with pytest.raises(ApiKeyScopeError, match="Invalid API key scope 'team'"):
            await create_api_key(
                session,
                org_id=_ORG_ID,
                name="k",
                role="operator",
                account_id=_ACCOUNT_ID,
                scope="team",
            )
        session.add.assert_not_called()

    def test_scope_column_constraint_vocabulary(self) -> None:
        """The model's CHECK mirrors migration 0178 (org/user only)."""
        constraints = {c.name: c for c in OrgApiKey.__table__.constraints if c.name}
        check = constraints["ck_org_api_keys_scope"]
        rendered = str(check.sqltext)
        # The rendered expression must enumerate exactly the two values.
        assert "'org'" in rendered
        assert "'user'" in rendered
        assert "team" not in rendered


class TestMintRunApiKeyPinnedOrg:
    @pytest.mark.asyncio
    async def test_run_scoped_key_is_pinned_org_scope(self) -> None:
        session = _mock_session()
        result = await mint_run_api_key(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            node_id="node-1",
            account_id=_ACCOUNT_ID,
            ttl_seconds=600,
        )
        assert result is not None
        key, _full_key = result
        assert key.scope == "org"
        assert key.run_id == _RUN_ID
        assert key.role == "runner"

    def test_run_scoped_key_denied_caller_scoped_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run-scoped (org-scope) key is denied caller-scoped (.self) tools
        through the pure resolver — the pinned FAR-620 matrix cell."""
        import types

        import modulo.core.mcp.scope_validator as sv

        patched = {**sv._TOOL_SCOPE_REQUIREMENTS, "notification_self": "notification.self"}
        monkeypatch.setattr(sv, "_TOOL_SCOPE_REQUIREMENTS", patched)
        monkeypatch.setattr(sv, "TOOL_SCOPE_REQUIREMENTS", types.MappingProxyType(patched))

        from modulo.core.mcp.scope_validator import resolve_tool_access

        allowed, _ = resolve_tool_access(
            tool="notification_self",
            action=None,
            role="runner",
            key_scope="org",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=True,
        )
        assert allowed is False


class TestSerializeKeyScope:
    def test_serialize_key_round_trips_scope(self) -> None:
        from modulo.auth.api_key import _serialize_key

        now = datetime.now(UTC)
        key = MagicMock()
        key.id = uuid.uuid4()
        key.name = "user key"
        key.role = "operator"
        key.scope = "user"
        key.team_id = None
        key.lookup_prefix = "abcd1234"
        key.last_used_at = None
        key.created_at = now
        key.expires_at = now + timedelta(days=365)
        key.revoked_at = None
        serialized = _serialize_key(key)
        assert serialized["scope"] == "user"
        assert serialized["role"] == "operator"

    def test_serialize_key_defaults_org_for_test_doubles(self) -> None:
        from modulo.auth.api_key import _serialize_key

        now = datetime.now(UTC)
        key = MagicMock()
        key.id = uuid.uuid4()
        key.name = "k"
        key.role = "runner"
        key.team_id = None
        key.lookup_prefix = "abcd1234"
        key.last_used_at = None
        key.created_at = now
        key.expires_at = now + timedelta(days=365)
        key.revoked_at = None
        # MagicMock scope attribute is not a str → defaults to 'org'.
        serialized = _serialize_key(key)
        assert serialized["scope"] == "org"


class TestScopeImmutability:
    def test_update_api_key_accepts_no_scope_parameter(self) -> None:
        """The shared update helper takes no scope argument — the scope is
        stamped at mint and never changed (the REST surface rejects a scope
        payload with 422 before reaching here)."""
        from modulo.auth.api_key import update_api_key

        params = inspect.signature(update_api_key).parameters
        assert "scope" not in params, (
            "update_api_key must not accept a scope parameter — the caller scope is immutable post-mint"
        )


class TestClampScopeIndependence:
    """The live-role ceiling is REUSED unchanged (FAR-620 3d): min(minted,
    live) per the role hierarchy, independent of the key's caller scope."""

    @pytest.mark.parametrize("scope", ["org", "user"])
    def test_clamp_independent_of_key_scope(self, scope: str) -> None:
        # The pure clamp has no scope input; pin that identical minted/live
        # roles clamp identically for both scope values (no scope-specific
        # branch exists — the assertion documents the invariant).
        assert _clamp_role("runner", "viewer") == "viewer"
        assert _clamp_role("operator", "admin") == "operator"
        assert frozenset({"org", "user"}) == KEY_SCOPES
        assert scope in KEY_SCOPES

    def test_degrade_cell_runner_key_live_viewer(self) -> None:
        """A runner key (org OR user scope) held by a member whose LIVE role
        degraded to viewer degrades to viewer — min(1, 0) = viewer, not death."""
        assert _clamp_role("runner", "viewer") == "viewer"

    def test_death_cell_missing_membership(self) -> None:
        """Membership None (owner removed/deactivated) → the empty-string
        denial marker; the caller denies (401) regardless of scope."""
        assert not _clamp_role("runner", None)
        assert not _clamp_role("operator", None)


class TestScopeColumnModel:
    def test_scope_column_exists_with_server_default(self) -> None:
        column = OrgApiKey.__table__.c.scope
        assert column.nullable is False
        assert column.server_default is not None
        assert str(column.server_default.arg) == "org"

    def test_scope_column_type(self) -> None:
        from sqlalchemy import String

        column = OrgApiKey.__table__.c.scope
        assert isinstance(column.type, String)
        assert column.type.length == 10

    def test_scope_check_constraint_named(self) -> None:
        names = {c.name for c in OrgApiKey.__table__.constraints if c.name}
        assert "ck_org_api_keys_scope" in names

    @pytest.mark.asyncio
    async def test_validate_api_key_does_not_filter_by_scope(self) -> None:
        """The lookup path is scope-agnostic — the flag gate (not the lookup)
        denies user keys, so stored rows stay enumerable for revocation."""
        from modulo.auth.api_key import _hash_key

        token = "mk_abcdefgh_secret"
        key = MagicMock()
        key.lookup_prefix = "abcdefgh"
        key.hashed_secret = _hash_key(token)
        key.role = "operator"
        key.scope = "user"
        key.expires_at = None
        key.revoked_at = None

        result = MagicMock()
        result.scalars.return_value = [key]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        session.flush = AsyncMock()

        found = await validate_api_key(session, token, uuid.uuid4())
        assert found is key
