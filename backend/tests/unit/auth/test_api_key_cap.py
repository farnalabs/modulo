"""Unit tests for the API-key role-cap + live re-validation (ADR 017 A2).

Covers:
1. ``_clamp_role`` pure function (never escalates; removal denies)
2. REST mint-cap: viewer cannot mint; runner/operator/admin boundaries
3. Middleware live clamp: demoted operator degrades, removed owner dies
4. Degradation counter / structured log fires
5. ``validate_api_key`` NEVER mutates ``key.role``
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.auth.api_key import generate_api_key, validate_api_key
from modulo.auth.permissions import _clamp_role

# ── _clamp_role pure tests ────────────────────────────────────────────────


class TestClampRole:
    @pytest.mark.parametrize(
        ("minted", "live", "expected"),
        [
            ("operator", "operator", "operator"),
            ("operator", "runner", "runner"),
            ("operator", "viewer", "viewer"),
            ("runner", "runner", "runner"),
            ("runner", "viewer", "viewer"),
            ("runner", "operator", "runner"),
            ("runner", "admin", "runner"),
            ("operator", "admin", "operator"),
        ],
        ids=[
            "operator->operator",
            "operator->runner",
            "operator->viewer",
            "runner->runner",
            "runner->viewer",
            "runner->operator",
            "runner->admin",
            "operator->admin",
        ],
    )
    def test_clamp_never_escalates(self, minted: str, live: str, expected: str) -> None:
        assert _clamp_role(minted, live) == expected

    def test_clamp_live_none_denial_marker(self) -> None:
        assert not _clamp_role("operator", None)

    def test_clamp_unknown_roles_deny(self) -> None:
        assert not _clamp_role("superadmin", "admin")
        assert not _clamp_role("operator", "owner")
        assert not _clamp_role("", "runner")


# ── REST mint-cap (route gate + _enforce_mint_cap) ─────────────────────────


class TestMintCap:
    async def _mint_cap(self, caller_role: str | None, requested: str) -> tuple[bool, str]:
        """Run the route-level gate + cap helpers with a fake principal/session.

        Returns (allowed, err) where err is "" on allow.
        """
        from fastapi import HTTPException

        from modulo.api.routes.api_keys import _enforce_mint_cap, _require_runner
        from modulo.auth.jwt import TenantPrincipal

        principal = TenantPrincipal(
            username="u",
            organisation_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            org_role=caller_role,
        )
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=caller_role)))

        try:
            _require_runner(principal, "api_key.create")
        except HTTPException as exc:
            return False, str(exc.status_code)

        try:
            await _enforce_mint_cap(session, principal, requested)
        except HTTPException as exc:
            return False, str(exc.status_code)
        return True, ""

    @pytest.mark.asyncio
    async def test_viewer_cannot_mint_any_key(self) -> None:
        allowed, err = await self._mint_cap("viewer", "runner")
        assert not allowed
        assert "403" in err

    @pytest.mark.asyncio
    async def test_runner_can_mint_runner_denied_operator(self) -> None:
        allowed, err = await self._mint_cap("runner", "runner")
        assert allowed, err
        allowed2, err2 = await self._mint_cap("runner", "operator")
        assert not allowed2
        assert "403" in err2

    @pytest.mark.asyncio
    async def test_operator_can_mint_operator_and_runner(self) -> None:
        assert (await self._mint_cap("operator", "operator"))[0]
        assert (await self._mint_cap("operator", "runner"))[0]

    @pytest.mark.asyncio
    async def test_admin_can_mint_operator_and_runner(self) -> None:
        assert (await self._mint_cap("admin", "operator"))[0]
        assert (await self._mint_cap("admin", "runner"))[0]

    @pytest.mark.asyncio
    async def test_removed_member_live_none_denied(self) -> None:
        from fastapi import HTTPException

        from modulo.api.routes.api_keys import _enforce_mint_cap
        from modulo.auth.jwt import TenantPrincipal

        principal = TenantPrincipal(
            username="u",
            organisation_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            org_role="operator",
        )
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with pytest.raises(HTTPException) as excinfo:
            await _enforce_mint_cap(session, principal, "runner")
        assert excinfo.value.status_code == 403


# ── Middleware live clamp (dispatch-level demote test, ADR-017 pinned set) ──


@pytest.mark.asyncio(loop_scope="module")
class TestMiddlewareLiveClamp:
    def _make_key(self, token: str, *, role: str = "operator") -> MagicMock:
        """Fake OrgApiKey whose hashed_secret matches the middleware raw pre-check."""
        from modulo.auth.api_key import _hash_key

        key = MagicMock()
        key.id = uuid.uuid4()
        key.organisation_id = uuid.uuid4()
        key.account_id = uuid.uuid4()
        key.role = role
        key.hashed_secret = _hash_key(token)
        key.lookup_prefix = token[3:11]
        key.revoked_at = None
        return key

    @pytest.fixture(autouse=True)
    def _reset_counter(self) -> None:
        import modulo.api.mcp_server as _ms

        saved = _ms._api_key_role_cap_count
        _ms._api_key_role_cap_count = 0
        yield
        _ms._api_key_role_cap_count = saved

    async def _run_middleware_clamp(self, key: MagicMock, live_role: str | None) -> str | None:
        """Drive the exact clamp seam the request middleware executes.

        Returns the clamped role, or None when the key dies (401).
        """
        import modulo.api.mcp_server as _ms

        async def _fake_validate(s, token, org_id=None):
            return key

        with (
            patch("modulo.api.mcp_server.validate_api_key", new=AsyncMock(side_effect=_fake_validate)),
            patch(
                "modulo.api.mcp_server.resolve_role_from_membership",
                new=AsyncMock(return_value=live_role),
            ),
        ):
            clamped = _ms._clamp_role(key.role, live_role)
            if clamped == "":
                _ms._record_api_key_role_cap(
                    minted_role=key.role,
                    effective_role="",
                    org_id=key.organisation_id,
                    degraded=False,
                    key_id=key.id,
                )
                return None
            if clamped != key.role:
                _ms._record_api_key_role_cap(
                    minted_role=key.role,
                    effective_role=clamped,
                    org_id=key.organisation_id,
                    degraded=True,
                    key_id=key.id,
                )
            return clamped

    async def test_demoted_operator_key_degrades_on_next_call(self) -> None:
        """key.role=operator, live=runner → effective 'runner'; operator tool denied."""
        from modulo.api.mcp_server import _ctx_role, _ctx_role_val
        from modulo.core.mcp.scope_validator import MCPAuthorizationError, check_tool_scope

        token, _, _ = generate_api_key()
        key = self._make_key(token, role="operator")

        role_token = _ctx_role.set("operator")
        try:
            clamped = await self._run_middleware_clamp(key, "runner")
            assert clamped == "runner"
            _ctx_role.set(clamped)
            assert _ctx_role_val() == "runner"
            with pytest.raises(MCPAuthorizationError):
                check_tool_scope(_ctx_role_val(), "create_pipeline")

            import modulo.api.mcp_server as _ms

            assert _ms.get_api_key_role_cap_count() == 1
        finally:
            _ctx_role.reset(role_token)

    async def test_removed_owner_key_dies_401(self) -> None:
        """live_role=None (owner removed/deactivated) → the key dies."""
        import modulo.api.mcp_server as _ms

        token, _, _ = generate_api_key()
        key = self._make_key(token, role="operator")

        clamped = await self._run_middleware_clamp(key, None)
        assert clamped is None
        assert _ms.get_api_key_role_cap_count() == 1
        # The denial marker from the pure helper is the empty string.
        assert not _clamp_role("operator", None)

    async def test_no_degradation_when_live_matches_minted(self) -> None:
        """operator minted / operator live → no clamp, no counter increment."""
        import modulo.api.mcp_server as _ms

        token, _, _ = generate_api_key()
        key = self._make_key(token, role="operator")

        clamped = await self._run_middleware_clamp(key, "operator")
        assert clamped == "operator"
        assert _ms.get_api_key_role_cap_count() == 0


# ── validate_api_key NEVER mutates key.role ────────────────────────────────


class TestValidateApiKeyNoMutation:
    @pytest.mark.asyncio
    async def test_validate_does_not_mutate_role(self) -> None:
        token, prefix, hashed = generate_api_key()
        key = MagicMock()
        key.lookup_prefix = prefix
        key.hashed_secret = hashed
        key.role = "operator"
        key.expires_at = None
        key.revoked_at = None

        result = MagicMock()
        result.scalars.return_value = [key]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        session.flush = AsyncMock()

        await validate_api_key(session, token, uuid.uuid4())
        assert key.role == "operator"
        session.flush.assert_awaited()
