"""Unit tests for the shared last-admin guard (modulo.db.crud.last_admin_guard).

Covers the counting semantics with a fake session, the two-int4 MD5 advisory
lock key parity with the SQL expression used by the SECURITY DEFINER, and the
pgcode -> HTTP status mapping (pure logic) used by the REST + SCIM routes.
"""

import hashlib
import uuid
from typing import Any

import pytest

from modulo.api.routes.admin import _extract_bg_pgcode, _raise_bg_pgcode
from modulo.db.crud.last_admin_guard import (
    LastAdminLockoutError,
    assert_not_last_admin,
)
from modulo.db.repositories.locks import _str_to_lock_keys

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TARGET_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _FakeResult:
    def __init__(self, *, first_value: tuple[Any, Any] | None = None, scalar_value: int | None = None) -> None:
        self._first = first_value
        self._scalar = scalar_value

    def first(self) -> tuple[Any, Any] | None:
        return self._first

    def scalar_one(self) -> int | None:
        return self._scalar


class _FakeBind:
    dialect = type("Dialect", (), {"name": "sqlite"})()


class _FakeSession:
    """Deterministic fake AsyncSession for the guard's counting logic.

    ``dialect.name = 'sqlite'`` so the advisory-lock path is skipped (unit
    scope); the count query is identified by its compiled SQL text.
    """

    def __init__(self, *, target_active: bool = True, target_break_glass: bool = False, other_admins: int = 1) -> None:
        self.target_active = target_active
        self.target_break_glass = target_break_glass
        self.other_admins = other_admins
        self.executed: list[Any] = []

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> _FakeResult:
        self.executed.append(stmt)
        sql = str(stmt)
        if "count" in sql and "org_memberships" in sql:
            return _FakeResult(scalar_value=self.other_admins)
        return _FakeResult(first_value=(self.target_active, self.target_break_glass))


class TestAssertNotLastAdminCounting:
    @pytest.mark.asyncio
    async def test_allows_when_other_admin_remains(self) -> None:
        session = _FakeSession(other_admins=1)
        await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_TARGET_ID,
            target_role_after="operator",
            target_active_after=False,
        )

    @pytest.mark.asyncio
    async def test_allows_when_target_remains_active_admin(self) -> None:
        session = _FakeSession(other_admins=0, target_active=True)
        await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_TARGET_ID,
            target_role_after="admin",
            target_active_after=True,
        )

    @pytest.mark.asyncio
    async def test_blocks_when_no_active_admin_would_remain(self) -> None:
        session = _FakeSession(other_admins=0)
        with pytest.raises(LastAdminLockoutError) as exc:
            await assert_not_last_admin(
                session,
                org_id=_ORG_ID,
                target_account_id=_TARGET_ID,
                target_role_after=None,
                target_active_after=False,
            )
        assert exc.value.org_id == _ORG_ID
        assert "last admin" in exc.value.reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_self_demote_of_last_admin(self) -> None:
        session = _FakeSession(other_admins=0, target_active=True)
        with pytest.raises(LastAdminLockoutError):
            await assert_not_last_admin(
                session,
                org_id=_ORG_ID,
                target_account_id=_TARGET_ID,
                target_role_after="runner",
                target_active_after=None,
            )

    @pytest.mark.asyncio
    async def test_break_glass_target_never_counts_as_admin(self) -> None:
        session = _FakeSession(other_admins=0, target_break_glass=True, target_active=True)
        with pytest.raises(LastAdminLockoutError):
            await assert_not_last_admin(
                session,
                org_id=_ORG_ID,
                target_account_id=_TARGET_ID,
                target_role_after="admin",
                target_active_after=True,
            )

    @pytest.mark.asyncio
    async def test_count_query_excludes_break_glass_and_inactive(self) -> None:
        session = _FakeSession(other_admins=1)
        await assert_not_last_admin(
            session,
            org_id=_ORG_ID,
            target_account_id=_TARGET_ID,
            target_role_after="operator",
            target_active_after=False,
        )
        count_sql = next(str(s) for s in session.executed if "count(*)" in str(s))
        assert "is_break_glass" in count_sql
        assert "active" in count_sql
        assert "deactivated_at" in count_sql


class TestLockKeyParity:
    def test_str_to_lock_keys_matches_sql_expression(self) -> None:
        """_str_to_lock_keys(str(org)) must equal the SECURITY DEFINER's SQL output.

        The SQL uses ('x' || substr(md5(key), 1, 8))::bit(32)::int4 for k1 and
        chars 9-16 for k2 — i.e. the first/second 8 hex chars of the MD5 digest
        as a signed 32-bit integer. This pins the shared keyspace so the app
        guard and the in-function locks never diverge.
        """
        key = str(_ORG_ID)
        digest = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest()

        def _sql_int(hex8: str) -> int:
            value = int(hex8, 16)
            return value - (1 << 32) if value >= (1 << 31) else value

        assert _str_to_lock_keys(key) == (_sql_int(digest[:8]), _sql_int(digest[8:16]))
        assert all(isinstance(k, int) for k in _str_to_lock_keys(key))


class TestPgcodeMapping:
    def _exc_with_pgcode(self, pgcode: str) -> Exception:
        orig = type("Orig", (), {"pgcode": pgcode})()
        exc = type("Wrapped", (Exception,), {})("wrapped")
        exc.orig = orig  # type: ignore[attr-defined]
        return exc

    def test_extracts_pgcode(self) -> None:
        assert _extract_bg_pgcode(self._exc_with_pgcode("M2020")) == "M2020"
        assert _extract_bg_pgcode(Exception("no pgcode")) is None

    def test_rest_mapping(self) -> None:
        with pytest.raises(Exception) as exc_info:
            _raise_bg_pgcode(
                self._exc_with_pgcode("M2010"),
                unauthorized_status=403,
                conflict_status=422,
                not_found_status=404,
            )
        assert exc_info.value.status_code == 403

        with pytest.raises(Exception) as exc_info:
            _raise_bg_pgcode(
                self._exc_with_pgcode("M2020"),
                unauthorized_status=403,
                conflict_status=422,
                not_found_status=404,
            )
        assert exc_info.value.status_code == 422

        with pytest.raises(Exception) as exc_info:
            _raise_bg_pgcode(
                self._exc_with_pgcode("M2040"),
                unauthorized_status=403,
                conflict_status=422,
                not_found_status=404,
            )
        assert exc_info.value.status_code == 404

    def test_scim_mapping(self) -> None:
        with pytest.raises(Exception) as exc_info:
            _raise_bg_pgcode(
                self._exc_with_pgcode("M2020"),
                unauthorized_status=409,
                conflict_status=409,
                not_found_status=404,
            )
        assert exc_info.value.status_code == 409

    def test_unmatched_pgcode_returns_none(self) -> None:
        assert (
            _raise_bg_pgcode(
                self._exc_with_pgcode("23505"),
                unauthorized_status=403,
                conflict_status=422,
                not_found_status=404,
            )
            is None
        )
