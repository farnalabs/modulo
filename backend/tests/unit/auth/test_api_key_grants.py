"""FAR-1477 / ADR 058: grant-sets on API keys (tri-state, delegation flag, resolvers, mint cap)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api.dependencies import _assert_tenant_permission
from modulo.api.routes import api_keys as api_keys_routes
from modulo.api.routes.api_keys import ApiKeyUpdate, _enforce_grants_mint_cap, _resolve_grants_expiry
from modulo.auth import api_key as api_key_mod
from modulo.auth import permissions as perms
from modulo.auth.api_key import ApiKeyInvalidError, _serialize_key, resolve_key_grants
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.permissions import (
    PERMISSIONS,
    PermissionDenied,
    assert_grant,
    grants_permit,
    is_delegable,
    parse_grants,
    serialize_grants,
)
from modulo.core.mcp.scope_validator import resolve_tool_access


def _principal(role: str = "operator", grants: frozenset[str] | None = None) -> TenantPrincipal:
    return TenantPrincipal(
        username="k",
        organisation_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role=role,
        key_grants=grants,
    )


# ── tri-state: NULL vs empty vs list must never collapse ────────────────────


class TestTriState:
    def test_null_column_is_legacy_none(self) -> None:
        assert parse_grants(None) is None

    def test_empty_column_is_explicit_deny_all_not_none_and_has_no_members(self) -> None:
        parsed = parse_grants("")
        assert parsed is not None
        assert len(parsed) == 0

    def test_list_column_is_exact_set(self) -> None:
        assert parse_grants("run.trigger pipeline.list") == frozenset({"run.trigger", "pipeline.list"})

    def test_serialize_keeps_null_and_empty_distinct(self) -> None:
        assert serialize_grants(None) is None
        assert serialize_grants([]) == ""

    def test_serialize_roundtrip(self) -> None:
        assert parse_grants(serialize_grants(["b.x", "a.y"])) == frozenset({"a.y", "b.x"})

    def test_none_permits_everything(self) -> None:
        assert grants_permit(None, "run.trigger") is True

    def test_empty_denies_everything(self) -> None:
        assert grants_permit(frozenset(), "run.trigger") is False

    def test_list_permits_only_members(self) -> None:
        grants = frozenset({"run.trigger"})
        assert grants_permit(grants, "run.trigger") is True
        assert grants_permit(grants, "run.cancel") is False

    def test_assert_grant_raises_not_granted(self) -> None:
        with pytest.raises(PermissionDenied) as exc:
            assert_grant(frozenset(), "run.trigger")
        assert exc.value.reason == "not_granted"

    def test_assert_grant_none_is_noop(self) -> None:
        assert_grant(None, "run.trigger")


# ── registry-level exclusion flag ───────────────────────────────────────────


class TestDelegableFlag:
    @pytest.mark.parametrize(
        "key",
        ["hitl.approve", "hitl.reject", "hitl.review", "hitl.claim", "hitl.deliver_manual", "org.delete"],
    )
    def test_human_only_and_destructive_not_delegable(self, key: str) -> None:
        assert is_delegable(key) is False

    def test_break_glass_controls_not_delegable(self) -> None:
        assert is_delegable("org.authz_enforce.manage") is False
        assert is_delegable("org.guardrails.kill_switch.manage") is False

    def test_credential_lifecycle_and_system_namespaces_never_delegable(self) -> None:
        namespaced = [k for k in PERMISSIONS if k.startswith(("api_key.", "oauth.client.", "system."))]
        assert len(namespaced) > 0
        assert not any(is_delegable(k) for k in namespaced)

    def test_unknown_key_not_delegable(self) -> None:
        assert is_delegable("made.up") is False

    def test_ordinary_keys_delegable(self) -> None:
        assert is_delegable("run.trigger") is True
        assert is_delegable("pipeline.list") is True

    def test_delegable_set_excludes_every_non_delegable_key(self) -> None:
        delegable = {k for k in PERMISSIONS if is_delegable(k)}
        assert "hitl.approve" not in delegable
        assert "run.trigger" in delegable

    def test_flag_read_live_not_snapshotted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert is_delegable("run.trigger") is True
        monkeypatch.setattr(perms, "NON_DELEGABLE_PERMISSIONS", perms.NON_DELEGABLE_PERMISSIONS | {"run.trigger"})
        assert is_delegable("run.trigger") is False

    def test_tightening_revokes_already_granted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        grants = frozenset({"run.trigger"})
        assert grants_permit(grants, "run.trigger") is True
        monkeypatch.setattr(perms, "NON_DELEGABLE_PERMISSIONS", perms.NON_DELEGABLE_PERMISSIONS | {"run.trigger"})
        assert grants_permit(grants, "run.trigger") is False

    def test_excluded_key_in_stored_grants_still_denied(self) -> None:
        assert grants_permit(frozenset({"hitl.approve"}), "hitl.approve") is False


# ── resolvers: effective = grants INTERSECT bundle(live_role) ───────────────


class TestMcpResolver:
    def _call(self, role: str, grants: frozenset[str] | None) -> bool:
        allowed, _ = resolve_tool_access(
            tool="trigger_pipeline",
            action=None,
            role=role,
            key_scope="org",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=True,
            grants=grants,
        )
        return allowed

    def test_null_grants_legacy_role_bundle(self) -> None:
        assert self._call("runner", None) is True

    def test_empty_grants_deny_all(self) -> None:
        assert self._call("runner", frozenset()) is False

    def test_exact_grant_and_role_allows(self) -> None:
        assert self._call("runner", frozenset({"run.trigger"})) is True

    def test_grant_without_role_denied(self) -> None:
        assert self._call("viewer", frozenset({"run.trigger"})) is False

    def test_role_without_grant_denied(self) -> None:
        assert self._call("operator", frozenset({"pipeline.list"})) is False

    def test_kill_switch_off_does_not_lift_grant_leg(self) -> None:
        allowed, _ = resolve_tool_access(
            tool="trigger_pipeline",
            action=None,
            role="runner",
            key_scope="org",
            auth_type="api_key",
            allowed_tools=None,
            kill_switch=False,
            grants=frozenset(),
        )
        assert allowed is False


class TestRestResolver:
    @pytest.mark.asyncio
    async def _check(self, principal: TenantPrincipal, permission: str = "run.trigger") -> None:
        session = MagicMock()
        session.begin = MagicMock()
        with patch("modulo.api.dependencies.resolve_authz_enforce", AsyncMock(return_value=True)):
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=None)
            cm.__aexit__ = AsyncMock(return_value=False)
            session.begin.return_value = cm
            await _assert_tenant_permission(session, principal, permission, PERMISSIONS[permission])

    @pytest.mark.asyncio
    async def test_null_grants_passes_on_role(self) -> None:
        await self._check(_principal("runner", None))

    @pytest.mark.asyncio
    async def test_empty_grants_denied(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._check(_principal("runner", frozenset()))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_granted_passes(self) -> None:
        await self._check(_principal("runner", frozenset({"run.trigger"})))

    @pytest.mark.asyncio
    async def test_ungranted_denied(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._check(_principal("operator", frozenset({"run.cancel"})))
        assert exc.value.status_code == 403


# ── key resolution + flag ───────────────────────────────────────────────────


def _key(grants: object) -> MagicMock:
    key = MagicMock()
    key.id = uuid.uuid4()
    key.organisation_id = uuid.uuid4()
    key.grants = grants
    return key


class TestResolveKeyGrants:
    @pytest.mark.asyncio
    async def test_null_grants_never_reads_flag(self) -> None:
        with patch.object(api_key_mod, "api_key_grants_enabled", AsyncMock(side_effect=AssertionError)) as flag:
            assert await resolve_key_grants(_key(None)) is None
        flag.assert_not_called()

    @pytest.mark.asyncio
    async def test_grant_bearing_key_denied_when_flag_off(self) -> None:
        with (
            patch.object(api_key_mod, "api_key_grants_enabled", AsyncMock(return_value=False)),
            pytest.raises(ApiKeyInvalidError),
        ):
            await resolve_key_grants(_key("run.trigger"))

    @pytest.mark.asyncio
    async def test_empty_grants_denied_when_flag_off(self) -> None:
        with (
            patch.object(api_key_mod, "api_key_grants_enabled", AsyncMock(return_value=False)),
            pytest.raises(ApiKeyInvalidError),
        ):
            await resolve_key_grants(_key(""))

    @pytest.mark.asyncio
    async def test_flag_on_parses(self) -> None:
        with patch.object(api_key_mod, "api_key_grants_enabled", AsyncMock(return_value=True)):
            assert await resolve_key_grants(_key("run.trigger")) == frozenset({"run.trigger"})

    @pytest.mark.asyncio
    async def test_flag_on_empty_is_deny_all_set(self) -> None:
        with patch.object(api_key_mod, "api_key_grants_enabled", AsyncMock(return_value=True)):
            assert await resolve_key_grants(_key("")) == frozenset()

    @pytest.mark.asyncio
    async def test_flag_read_error_fails_closed(self) -> None:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(api_key_mod, "get_registry", return_value=registry):
            assert await api_key_mod.api_key_grants_enabled(uuid.uuid4()) is False


class TestSerialize:
    def _row(self, grants: object) -> MagicMock:
        row = MagicMock()
        row.id = uuid.uuid4()
        row.name = "n"
        row.role = "runner"
        row.scope = "org"
        row.team_id = None
        row.lookup_prefix = "abcd1234"
        row.last_used_at = None
        row.created_at = datetime.now(UTC)
        row.expires_at = None
        row.revoked_at = None
        row.grants = grants
        return row

    def test_legacy_key_has_no_grants_field(self) -> None:
        assert "grants" not in _serialize_key(self._row(None))

    def test_empty_grants_emitted_as_empty_list(self) -> None:
        assert _serialize_key(self._row(""))["grants"] == []

    def test_grants_emitted_sorted(self) -> None:
        assert _serialize_key(self._row("b.x a.y"))["grants"] == ["a.y", "b.x"]


# ── mint cap ────────────────────────────────────────────────────────────────


class TestMintCap:
    async def _cap(self, grants: list[str], live_role: str | None) -> None:
        with patch.object(api_keys_routes, "resolve_role_from_membership", AsyncMock(return_value=live_role)):
            await _enforce_grants_mint_cap(MagicMock(), _principal("operator"), grants)

    @pytest.mark.asyncio
    async def test_unknown_key_422(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._cap(["nope.nope"], "operator")
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key", ["hitl.approve", "api_key.create", "oauth.client.create", "system.config.manage", "org.delete"]
    )
    async def test_non_delegable_403_even_for_admin(self, key: str) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._cap([key], "admin")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_above_live_role_403(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._cap(["pipeline.create"], "runner")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_membership_403(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._cap(["run.trigger"], None)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_subset_of_live_capability_ok(self) -> None:
        await self._cap(["run.trigger", "pipeline.list"], "runner")

    @pytest.mark.asyncio
    async def test_empty_grants_ok(self) -> None:
        await self._cap([], "runner")


# ── TTL + immutability ──────────────────────────────────────────────────────


class TestTtl:
    def test_flag_off_untouched(self) -> None:
        assert _resolve_grants_expiry(None, "user", False) is None

    def test_org_scope_untouched(self) -> None:
        assert _resolve_grants_expiry(None, "org", True) is None

    def test_user_scope_defaults_to_90_days(self) -> None:
        got = _resolve_grants_expiry(None, "user", True)
        assert got is not None
        delta = got - datetime.now(UTC)
        assert timedelta(days=89, hours=23) < delta <= timedelta(days=90)

    def test_user_scope_over_90_days_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _resolve_grants_expiry(datetime.now(UTC) + timedelta(days=91), "user", True)
        assert exc.value.status_code == 422

    def test_user_scope_within_90_days_kept(self) -> None:
        wanted = datetime.now(UTC) + timedelta(days=30)
        assert _resolve_grants_expiry(wanted, "user", True) == wanted


class TestImmutable:
    def test_update_payload_with_grants_rejected(self) -> None:
        req = ApiKeyUpdate(grants=["run.trigger"])
        with pytest.raises(HTTPException) as exc:
            api_keys_routes._validate_update_payload(req)
        assert exc.value.status_code == 422
