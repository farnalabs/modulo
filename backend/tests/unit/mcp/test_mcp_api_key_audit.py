"""FAR-620 stage 2: MCP api-key mint/revoke audit parity with REST.

The ``create_api_key`` / ``revoke_api_key`` MCP tools now emit the PRD §8.12
``api_key_created`` / ``api_key_revoked`` events (exact REST event_type
strings) with the payload stamps on BOTH events on BOTH surfaces:
``auth_type`` + ``key_scope`` + masked prefix (``mk_<prefix>****``).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.api.mcp_server as ms

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_session_context(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock(is_break_glass=False))
    return session


def _set_credential(*, auth_type: str = "jwt") -> None:
    ms._ctx_org_id.set(_ORG_ID)
    ms._ctx_role.set("admin")
    ms._ctx_user_id.set(_USER_ID)
    ms._ctx_key_id.set(_USER_ID)
    ms._ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
    ms._ctx_auth_type.set(auth_type)
    ms._ctx_key_scope.set("org")
    ms._ctx_team_id.set(None)
    ms._ctx_node_allowed_tools.set(None)


@pytest.fixture(autouse=True)
def _ctx() -> Any:
    from modulo.auth.permissions import reset_authz_enforce, set_authz_enforce

    _set_credential()
    ms._ctx_user_keys_enabled.set(False)
    yield
    for attr in (
        "_ctx_org_id",
        "_ctx_role",
        "_ctx_user_id",
        "_ctx_key_id",
        "_ctx_auth_token",
        "_ctx_auth_type",
        "_ctx_team_id",
    ):
        getattr(ms, attr).set(None)
    ms._ctx_key_scope.set(None)
    ms._ctx_user_keys_enabled.set(False)
    reset_authz_enforce(set_authz_enforce(True))


def _key_row() -> MagicMock:
    key = MagicMock()
    key.id = uuid.uuid4()
    key.name = "CI Key"
    key.role = "runner"
    key.scope = "org"
    key.team_id = None
    key.lookup_prefix = "abcd1234"
    key.created_at = None
    key.expires_at = None
    return key


class TestMcpMintAudit:
    @pytest.mark.asyncio
    async def test_mint_emits_api_key_created_with_stamps(self) -> None:
        audit = AsyncMock()
        key = _key_row()
        session = _mock_session()
        session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", key.id))
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=_make_session_context(session)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="admin")),
            patch.object(ms, "auth_create_api_key", new=AsyncMock(return_value=(key, "mk_full"))),
            patch("modulo.core.audit_logger.append_audit_event", new=audit) as append,
        ):
            result = await ms.create_api_key(name="CI Key", role="runner")
        assert "error" not in result
        append.assert_awaited_once()
        kwargs = append.await_args.kwargs
        assert kwargs["event_type"] == "api_key_created"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["resource_type"] == "api_key"
        assert kwargs["resource_id"] == key.id
        assert kwargs["payload_json"] == {
            "name": "CI Key",
            "role": "runner",
            "team_id": None,
            "auth_type": "jwt",
            "key_scope": "org",
            "lookup_prefix": "mk_abcd1234****",
        }

    @pytest.mark.asyncio
    async def test_mint_audit_failure_does_not_fail_mint(self) -> None:
        key = _key_row()
        session = _mock_session()
        session.add = MagicMock(side_effect=lambda obj: setattr(obj, "id", key.id))
        # The audit append opens its own fresh _session — give it a session too.
        audit_session = _mock_session()
        factory_sessions = iter([session, audit_session])

        def _factory(*_a: Any, **_k: Any) -> MagicMock:
            return _make_session_context(next(factory_sessions))

        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", new=MagicMock(side_effect=_factory)),
            patch.object(ms, "resolve_role_from_membership", new=AsyncMock(return_value="admin")),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new=AsyncMock(side_effect=RuntimeError("audit boom")),
            ),
        ):
            result = await ms.create_api_key(name="CI Key", role="runner")
        assert "error" not in result


class TestMcpRevokeAudit:
    @pytest.mark.asyncio
    async def test_revoke_emits_api_key_revoked_with_stamps(self) -> None:
        audit = AsyncMock()
        key = _key_row()
        key.scope = "user"
        revoke = AsyncMock(return_value=key)
        mint_session = _mock_session()
        audit_session = _mock_session()
        factory_sessions = iter([mint_session, audit_session])

        def _factory(*_a: Any, **_k: Any) -> MagicMock:
            return _make_session_context(next(factory_sessions))

        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", new=MagicMock(side_effect=_factory)),
            patch.object(ms, "auth_revoke_api_key", new=revoke),
            patch("modulo.core.audit_logger.append_audit_event", new=audit) as append,
        ):
            kid = key.id
            result = await ms.revoke_api_key(key_id=str(kid))
        assert result["revoked"] is True
        append.assert_awaited_once()
        kwargs = append.await_args.kwargs
        assert kwargs["event_type"] == "api_key_revoked"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["resource_id"] == kid
        assert kwargs["payload_json"] == {
            "revoked_by": str(_USER_ID),
            "auth_type": "jwt",
            "key_scope": "user",
            "lookup_prefix": "mk_abcd1234****",
        }

    @pytest.mark.asyncio
    async def test_revoke_not_found_emits_no_audit(self) -> None:
        audit = AsyncMock()
        mint_session = _mock_session()
        audit_session = _mock_session()
        factory_sessions = iter([mint_session, audit_session])

        def _factory(*_a: Any, **_k: Any) -> MagicMock:
            return _make_session_context(next(factory_sessions))

        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", new=MagicMock(side_effect=_factory)),
            patch.object(ms, "auth_revoke_api_key", new=AsyncMock(return_value=None)),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            result = await ms.revoke_api_key(key_id=str(uuid.uuid4()))
        assert result["error"] == "not_found"
        audit.assert_not_awaited()
