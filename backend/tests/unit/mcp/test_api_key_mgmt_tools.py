"""Unit tests for the create_api_key / list_api_keys / revoke_api_key MCP tools.

Exercises the real MCP tool functions against a mocked DB session so the real
``modulo.auth.api_key`` CRUD path (key generation, hashing, serialisation,
org-scoped revoke) is covered — the tools are not mocked.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from tests.unit.mcp.helpers import ORG_ID, AuthContext, make_session_context

_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_KEY_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_key(**overrides: object) -> MagicMock:
    """A serialisable OrgApiKey stand-in with every field ``_serialize_key`` reads."""
    k = MagicMock()
    k.id = _KEY_ID
    k.name = "CI/CD Key"
    k.role = "operator"
    k.team_id = None
    k.lookup_prefix = "abcd1234"
    k.last_used_at = None
    k.created_at = _NOW
    k.expires_at = _NOW + timedelta(days=365)
    k.revoked_at = None
    for field, value in overrides.items():
        setattr(k, field, value)
    return k


def _make_account(*, is_break_glass: bool = False) -> MagicMock:
    acct = MagicMock()
    acct.is_break_glass = is_break_glass
    acct.break_glass_expires_at = None
    acct.break_glass_deactivated_at = None
    acct.active = True
    return acct


def _make_create_session(key: MagicMock, *, is_break_glass: bool = False) -> AsyncMock:
    """Session whose ``add`` assigns PK + created_at like a real flush.

    ``OrgApiKey.id`` and ``created_at`` are applied at flush time; a mocked
    flush never assigns them, so simulating the assignment on ``add`` lets the
    tool build a real ``OrgApiKey`` (real ``mk_`` key generation) and return
    a valid id/created_at.
    """
    session = AsyncMock()
    session.add = MagicMock(
        side_effect=lambda obj: (
            setattr(obj, "id", key.id),
            setattr(obj, "created_at", _NOW),
        )
    )
    session.get = AsyncMock(return_value=_make_account(is_break_glass=is_break_glass))
    return session


def _make_list_session(keys: list[MagicMock]) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value = keys
    session.execute.return_value = result
    session.get = AsyncMock(return_value=_make_account())
    return session


def _make_revoke_session(key: MagicMock | None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = key
    session.execute.return_value = result
    session.get = AsyncMock(return_value=_make_account())
    return session


@contextlib.contextmanager
def _patch_create_env(role: str | None = "admin"):
    """Patch auth-validity, DB session, and live-role read for a success path.

    Yields the mocked ``_session`` so tests can configure the DB session.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("modulo.api.mcp_server.validate_current_auth", return_value=True))
        mock_session = stack.enter_context(patch("modulo.api.mcp_server._session"))
        stack.enter_context(
            patch("modulo.api.mcp_server.resolve_role_from_membership", new=AsyncMock(return_value=role))
        )
        yield mock_session


class TestCreateApiKey(AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        from modulo.api.mcp_server import create_api_key

        result = await create_api_key(name="CI")
        assert result["error"] == "auth_expired"

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_insufficient_scope(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        from modulo.api.mcp_server import create_api_key
        from modulo.core.mcp.scope_validator import MCPAuthorizationError

        mock_session.return_value = make_session_context(AsyncMock())
        with patch(
            "modulo.api.mcp_server.check_tool_scope",
            side_effect=MCPAuthorizationError("Insufficient scope"),
        ):
            result = await create_api_key(name="CI")
        assert result["error"] == "insufficient_scope"

    async def test_invalid_role_rejected(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            result = await create_api_key(name="CI", role="admin")
        assert result["error"] == "validation_failed"
        assert result["field"] == "role"

    async def test_blank_name_rejected(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            result = await create_api_key(name="   ")
        assert result["error"] == "validation_failed"
        assert result["field"] == "name"

    async def test_past_expires_at_rejected(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            result = await create_api_key(name="CI", expires_at="2020-01-01T00:00:00Z")
        assert result["error"] == "validation_failed"
        assert result["field"] == "expires_at"

    async def test_invalid_expires_at_format_rejected(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            result = await create_api_key(name="CI", expires_at="not-a-date")
        assert result["error"] == "validation_failed"
        assert result["field"] == "expires_at"

    async def test_runner_cannot_mint_operator(self) -> None:
        from modulo.api.mcp_server import _ctx_role, create_api_key

        _ctx_role.set("runner")
        with _patch_create_env(role="runner") as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            result = await create_api_key(name="Escalate", role="operator")
        assert result["error"] == "insufficient_scope"
        assert "live role" in result["detail"]

    async def test_runner_can_mint_runner(self) -> None:
        from modulo.api.mcp_server import _ctx_role, create_api_key

        _ctx_role.set("runner")
        with _patch_create_env(role="runner") as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            result = await create_api_key(name="CI", role="runner")
        assert "key_value" in result
        assert result["role"] == "runner"

    async def test_removed_membership_denied(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with _patch_create_env(role=None) as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            result = await create_api_key(name="CI")
        assert result["error"] == "insufficient_scope"
        assert "membership" in result["detail"]

    async def test_break_glass_account_cannot_mint(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key(), is_break_glass=True))
            with (
                patch("modulo.db.crud.break_glass_deny.is_break_glass_live", return_value=True),
                patch("modulo.db.crud.break_glass_deny.is_break_glass_denied", return_value=False),
            ):
                result = await create_api_key(name="CI")
        assert result["error"] == "insufficient_scope"
        assert "Break-glass" in result["detail"]

    async def test_team_key_requires_admin(self) -> None:
        from modulo.api.mcp_server import _ctx_role, create_api_key

        _ctx_role.set("operator")
        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            with (
                patch("modulo.api.mcp_server.get_settings", return_value=MagicMock()),
                patch(
                    "modulo.api.mcp_server.resolve_plan_context",
                    new=AsyncMock(return_value=_Features(enabled=True)),
                ),
            ):
                result = await create_api_key(name="CI", team_id=str(_TEAM_ID))
        assert result["error"] == "insufficient_scope"
        assert "admin" in result["detail"]

    async def test_team_key_requires_feature(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            with (
                patch("modulo.api.mcp_server.get_settings", return_value=MagicMock()),
                patch(
                    "modulo.api.mcp_server.resolve_plan_context",
                    new=AsyncMock(return_value=_Features(enabled=False)),
                ),
            ):
                result = await create_api_key(name="CI", team_id=str(_TEAM_ID))
        assert result["error"] == "insufficient_scope"
        assert "upgraded plan" in result["detail"]

    async def test_admin_can_create_team_key(self) -> None:
        from modulo.api.mcp_server import create_api_key

        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            with (
                patch("modulo.api.mcp_server.get_settings", return_value=MagicMock()),
                patch(
                    "modulo.api.mcp_server.resolve_plan_context",
                    new=AsyncMock(return_value=_Features(enabled=True)),
                ),
            ):
                result = await create_api_key(name="CI", team_id=str(_TEAM_ID))
        assert result["team_id"] == str(_TEAM_ID)

    async def test_expires_at_parsed_and_passed_to_crud(self) -> None:
        from modulo.api.mcp_server import create_api_key

        mock_crud = AsyncMock(return_value=(_make_key(), "mk_fullkeyvalue12345678901234567890"))
        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(_make_key()))
            with patch("modulo.api.mcp_server.auth_create_api_key", new=mock_crud) as _mock_crud:
                result = await create_api_key(name="CI", expires_at="2030-06-01T00:00:00")
        assert "key_value" in result
        assert _mock_crud.await_args.kwargs["expires_at"] == datetime(2030, 6, 1, tzinfo=UTC)
        assert _mock_crud.await_args.kwargs["role"] == "operator"
        assert _mock_crud.await_args.kwargs["team_id"] is None


class _Features:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    def feature_enabled(self, name: str) -> bool:
        return self._enabled


class TestCreateApiKeySuccess(AuthContext):
    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")

    async def test_create_returns_full_key_once(self) -> None:
        from modulo.api.mcp_server import create_api_key

        key = _make_key()
        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(key))
            result = await create_api_key(name="CI/CD Key", role="operator")

        assert result["id"] == str(key.id)
        assert result["name"] == "CI/CD Key"
        assert result["role"] == "operator"
        assert result["key_value"].startswith("mk_")
        assert len(result["key_value"]) == 35
        assert result["lookup_prefix"] == f"mk_{result['key_value'][3:11]}****"
        assert result["created_at"] == _NOW.isoformat()
        assert result["team_id"] is None


class TestListApiKeys(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        from modulo.api.mcp_server import list_api_keys

        result = await list_api_keys()
        assert result["error"] == "auth_expired"

    async def test_list_never_returns_full_key_values(self) -> None:
        from modulo.api.mcp_server import list_api_keys

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value = make_session_context(_make_list_session([_make_key()]))
            result = await list_api_keys()

        assert result["total"] == 1
        entry = result["api_keys"][0]
        assert "key_value" not in entry
        assert "hashed_secret" not in entry
        assert entry["lookup_prefix"] == "mk_abcd1234****"
        assert entry["id"] == str(_KEY_ID)
        assert entry["role"] == "operator"
        assert entry["created_at"] == _NOW.isoformat()

    async def test_list_scoped_to_caller_org(self) -> None:
        from modulo.api.mcp_server import list_api_keys

        mock_list = AsyncMock(return_value=[{"id": str(_KEY_ID), "name": "CI"}])
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.auth_list_api_keys", new=mock_list) as _mock_list,
        ):
            mock_session.return_value = make_session_context(AsyncMock())
            result = await list_api_keys()

        assert result["api_keys"][0]["id"] == str(_KEY_ID)
        assert _mock_list.await_args.args[1] == ORG_ID


class TestRevokeApiKey(AuthContext):
    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        from modulo.api.mcp_server import revoke_api_key

        result = await revoke_api_key(str(_KEY_ID))
        assert result["error"] == "auth_expired"

    async def test_revoke_invalid_id(self) -> None:
        from modulo.api.mcp_server import revoke_api_key

        with patch("modulo.api.mcp_server.validate_current_auth", return_value=True):
            result = await revoke_api_key("not-a-uuid")
        assert result["error"] == "invalid_id"
        assert result["field"] == "key_id"

    async def test_revoke_success(self) -> None:
        from modulo.api.mcp_server import revoke_api_key

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value = make_session_context(_make_revoke_session(_make_key()))
            result = await revoke_api_key(str(_KEY_ID))
        assert result == {"id": str(_KEY_ID), "revoked": True}

    async def test_revoke_not_found(self) -> None:
        from modulo.api.mcp_server import revoke_api_key

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value = make_session_context(_make_revoke_session(None))
            result = await revoke_api_key(str(_KEY_ID))
        assert result["error"] == "not_found"

    async def test_revoke_scoped_to_caller_org(self) -> None:
        from modulo.api.mcp_server import revoke_api_key

        mock_revoke = AsyncMock(return_value=True)
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.api.mcp_server.auth_revoke_api_key", new=mock_revoke) as _mock_revoke,
        ):
            mock_session.return_value = make_session_context(_make_revoke_session(_make_key()))
            result = await revoke_api_key(str(_KEY_ID))

        assert result == {"id": str(_KEY_ID), "revoked": True}
        assert _mock_revoke.await_args.args[2] == ORG_ID

    async def test_break_glass_account_cannot_revoke(self) -> None:
        from modulo.api.mcp_server import revoke_api_key

        session = _make_revoke_session(_make_key())
        session.get = AsyncMock(return_value=_make_account(is_break_glass=True))
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
            patch("modulo.db.crud.break_glass_deny.is_break_glass_live", return_value=True),
            patch("modulo.db.crud.break_glass_deny.is_break_glass_denied", return_value=False),
        ):
            mock_session.return_value = make_session_context(session)
            result = await revoke_api_key(str(_KEY_ID))
        assert result["error"] == "insufficient_scope"
        assert "Break-glass" in result["detail"]


class TestRoundTrip(AuthContext):
    """create -> list -> revoke through the real MCP tools and real CRUD path."""

    def setup_method(self) -> None:
        super().setup_method()
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")

    async def test_round_trip_create_list_revoke(self) -> None:
        from modulo.api.mcp_server import create_api_key, list_api_keys, revoke_api_key

        key = _make_key(name="Roundtrip", role="runner")
        with _patch_create_env() as mock_session:
            mock_session.return_value = make_session_context(_make_create_session(key))
            created = await create_api_key(name="Roundtrip", role="runner")

        assert created["key_value"].startswith("mk_")
        key_id = created["id"]

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value = make_session_context(_make_list_session([key]))
            listed = await list_api_keys()

        entry = next(e for e in listed["api_keys"] if e["id"] == key_id)
        assert "key_value" not in entry
        assert entry["name"] == "Roundtrip"
        assert entry["role"] == "runner"

        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session") as mock_session,
        ):
            mock_session.return_value = make_session_context(_make_revoke_session(key))
            revoked = await revoke_api_key(key_id)

        assert revoked == {"id": key_id, "revoked": True}
