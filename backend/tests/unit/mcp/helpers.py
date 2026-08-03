"""Shared constants and helpers for MCP tool unit tests."""

import uuid
from unittest.mock import AsyncMock

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
API_KEY = "mk_testprefix_testsecretkey1234567890abc"
FERNET_KEY = "vK-xU7GqHLflg_GqzJ1FqWI7pHWoHSIyukf4wx-tMHI="


def make_session_context(session: AsyncMock) -> AsyncMock:
    """Wrap an AsyncMock session so async __aenter__/__aexit__ are usable."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class AuthContext:
    """Set/teardown the MCP ContextVars so tool handlers reach the DB layer."""

    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(ORG_ID)
        _ctx_role.set("operator")
        _ctx_auth_token.set(API_KEY)
        _ctx_auth_type.set("api_key")
        _ctx_user_id.set(USER_ID)

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_org_id,
            _ctx_role,
            _ctx_user_id,
        )

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)
        _ctx_user_id.set(None)
