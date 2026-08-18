"""Unit tests for API key generation and validation."""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.auth.api_key import (
    _UNSET,
    ApiKeyInvalidError,
    _hash_key,
    _serialize_key,
    _validate_team_key_role,
    create_api_key,
    generate_api_key,
    list_api_keys,
    mint_run_api_key,
    revoke_api_key,
    revoke_run_api_key,
    revoke_run_api_key_sweep,
    update_api_key,
    validate_api_key,
)
from modulo.db.models.api_key import OrgApiKey
from modulo.db.models.run import TERMINAL_STATUSES

# ---------------------------------------------------------------------------
# generate_api_key
# ---------------------------------------------------------------------------


def test_generate_api_key_has_mk_prefix() -> None:
    full_key, _prefix, _hashed = generate_api_key()
    assert full_key.startswith("mk_")


def test_generate_api_key_prefix_8_chars() -> None:
    _, prefix, _ = generate_api_key()
    assert len(prefix) == 8


def test_generate_api_key_hash_is_sha256_of_full_key() -> None:
    full_key, _, hashed = generate_api_key()
    expected = hashlib.sha256(full_key.encode()).hexdigest()
    assert hashed == expected


def test_generate_api_key_unique_each_call() -> None:
    k1, _, _ = generate_api_key()
    k2, _, _ = generate_api_key()
    assert k1 != k2


def test_hash_key_constant_time_verifiable() -> None:
    full_key, _, expected_hash = generate_api_key()
    computed = _hash_key(full_key)
    assert hmac.compare_digest(expected_hash, computed)


# ---------------------------------------------------------------------------
# validate_api_key
# ---------------------------------------------------------------------------


def _make_key_row(full_key: str) -> MagicMock:
    k = MagicMock()
    k.id = uuid.uuid4()
    k.lookup_prefix = full_key[3:11]
    k.hashed_secret = _hash_key(full_key)
    k.role = "operator"
    k.last_used_at = None
    k.expires_at = None
    return k


def _make_session(key_row: MagicMock | None) -> AsyncMock:
    session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value = [key_row] if key_row else []
    session.execute = AsyncMock(return_value=scalar_result)
    return session


@pytest.mark.asyncio
async def test_validate_api_key_success() -> None:
    full_key, _, _ = generate_api_key()
    key_row = _make_key_row(full_key)
    org_id = uuid.uuid4()
    session = _make_session(key_row)

    result = await validate_api_key(session, full_key, org_id)
    assert result is key_row


@pytest.mark.asyncio
async def test_validate_api_key_invalid_prefix_raises() -> None:
    session = _make_session(None)
    org_id = uuid.uuid4()
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, "bad_key", org_id)


@pytest.mark.asyncio
async def test_validate_api_key_not_found_raises() -> None:
    full_key, _, _ = generate_api_key()
    session = _make_session(None)
    org_id = uuid.uuid4()
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)


@pytest.mark.asyncio
async def test_validate_api_key_hash_mismatch_raises() -> None:
    full_key, _, _ = generate_api_key()
    key_row = _make_key_row(full_key)
    key_row.hashed_secret = "a" * 64  # wrong hash
    org_id = uuid.uuid4()
    session = _make_session(key_row)
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)


@pytest.mark.asyncio
async def test_validate_api_key_expired_raises() -> None:
    full_key, _, _ = generate_api_key()
    key_row = _make_key_row(full_key)
    key_row.expires_at = datetime.now(UTC) - timedelta(days=1)
    org_id = uuid.uuid4()
    session = _make_session(key_row)
    with pytest.raises(ApiKeyInvalidError):
        await validate_api_key(session, full_key, org_id)


# ---------------------------------------------------------------------------
# _validate_team_key_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "team_id", "should_raise"),
    [
        ("admin", uuid.uuid4(), True),
        ("operator", uuid.uuid4(), False),
        ("runner", uuid.uuid4(), False),
        ("admin", None, False),
    ],
)
def test_validate_team_key_role(role: str, team_id: uuid.UUID | None, should_raise: bool) -> None:
    key = MagicMock()
    key.team_id = team_id
    key.role = role

    if should_raise:
        with pytest.raises(ApiKeyInvalidError, match="team-scoped API keys cannot have admin role"):
            _validate_team_key_role(key)
    else:
        _validate_team_key_role(key)


# ---------------------------------------------------------------------------
# create_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_api_key_accepts_expires_at() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    future = datetime.now(UTC) + timedelta(days=30)

    key, full_key = await create_api_key(
        session,
        org_id=org_id,
        name="Test Key",
        role="runner",
        account_id=user_id,
        expires_at=future,
    )
    assert key is not None
    assert full_key.startswith("mk_")


@pytest.mark.asyncio
async def test_create_api_key_with_team_id() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    team_id = uuid.uuid4()

    key, full_key = await create_api_key(
        session,
        org_id=org_id,
        name="Team Key",
        role="operator",
        account_id=user_id,
        team_id=team_id,
    )
    assert key is not None
    assert key.team_id == team_id
    assert full_key.startswith("mk_")


@pytest.mark.asyncio
async def test_create_api_key_with_team_id_rejects_admin() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    team_id = uuid.uuid4()

    with pytest.raises(ApiKeyInvalidError):
        await create_api_key(
            session,
            org_id=org_id,
            name="Bad Key",
            role="admin",
            account_id=user_id,
            team_id=team_id,
        )


# ---------------------------------------------------------------------------
# _serialize_key
# ---------------------------------------------------------------------------


def _make_serializable_key(**overrides: object) -> MagicMock:
    k = MagicMock()
    k.id = uuid.uuid4()
    k.name = "Ops Key"
    k.role = "operator"
    k.team_id = None
    k.lookup_prefix = "abcdefgh"
    k.last_used_at = None
    k.created_at = datetime.now(UTC)
    k.expires_at = datetime.now(UTC) + timedelta(days=30)
    k.revoked_at = None
    for attr, value in overrides.items():
        setattr(k, attr, value)
    return k


def test_serialize_key_masks_secret_and_reports_active() -> None:
    key = _make_serializable_key()
    serialized = _serialize_key(key)

    assert serialized["id"] == str(key.id)
    assert serialized["name"] == "Ops Key"
    assert serialized["role"] == "operator"
    assert serialized["lookup_prefix"] == "mk_abcdefgh****"
    assert serialized["team_id"] is None
    assert serialized["last_used_at"] is None
    assert serialized["created_at"] == key.created_at.isoformat()
    assert serialized["expires_at"] == key.expires_at.isoformat()
    assert serialized["is_active"] is True


def test_serialize_key_expired_is_inactive() -> None:
    key = _make_serializable_key(expires_at=datetime.now(UTC) - timedelta(days=1))
    assert _serialize_key(key)["is_active"] is False


def test_serialize_key_revoked_is_inactive() -> None:
    key = _make_serializable_key(revoked_at=datetime.now(UTC))
    assert _serialize_key(key)["is_active"] is False


def test_serialize_key_with_team_and_last_used() -> None:
    team_id = uuid.uuid4()
    last_used = datetime.now(UTC) - timedelta(hours=1)
    key = _make_serializable_key(team_id=team_id, last_used_at=last_used)

    serialized = _serialize_key(key)
    assert serialized["team_id"] == str(team_id)
    assert serialized["last_used_at"] == last_used.isoformat()


# ---------------------------------------------------------------------------
# list_api_keys
# ---------------------------------------------------------------------------


def _make_list_session(rows: list[object]) -> AsyncMock:
    scalar_result = MagicMock()
    scalar_result.scalars.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar_result)
    return session


@pytest.mark.asyncio
async def test_list_api_keys_serializes_keys() -> None:
    org_id = uuid.uuid4()
    key = _make_serializable_key()
    session = _make_list_session([key])

    keys = await list_api_keys(session, org_id)

    assert keys == [_serialize_key(key)]


@pytest.mark.asyncio
async def test_list_api_keys_excludes_revoked_by_default() -> None:
    org_id = uuid.uuid4()
    session = _make_list_session([])

    await list_api_keys(session, org_id)

    stmt = session.execute.await_args.args[0]
    where = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
    assert "revoked_at IS NULL" in where


@pytest.mark.asyncio
async def test_list_api_keys_include_revoked_omits_filter() -> None:
    org_id = uuid.uuid4()
    session = _make_list_session([])

    await list_api_keys(session, org_id, include_revoked=True)

    stmt = session.execute.await_args.args[0]
    where = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
    assert "revoked_at" not in where


@pytest.mark.asyncio
async def test_list_api_keys_orders_by_created_desc() -> None:
    org_id = uuid.uuid4()
    session = _make_list_session([])

    await list_api_keys(session, org_id)

    stmt = session.execute.await_args.args[0]
    compiled = str(stmt)
    assert "created_at" in compiled
    assert "DESC" in compiled.upper()


# ---------------------------------------------------------------------------
# update_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_api_key_updates_name_and_role() -> None:
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, name="Updated", role="runner")
    assert updated is not None
    assert updated.name == "Updated"
    assert updated.role == "runner"


@pytest.mark.asyncio
async def test_update_api_key_not_found_returns_none() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    updated = await update_api_key(session, uuid.uuid4(), uuid.uuid4(), name="Nope")
    assert updated is None


@pytest.mark.asyncio
async def test_update_api_key_updates_team_id() -> None:
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    team_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"
    key.team_id = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, team_id=team_id)
    assert updated is not None
    assert updated.team_id == team_id


@pytest.mark.asyncio
async def test_update_api_key_with_team_id_rejects_admin() -> None:
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    team_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "admin"
    key.team_id = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    with pytest.raises(ApiKeyInvalidError):
        await update_api_key(session, key_id, org_id, team_id=team_id)


@pytest.mark.asyncio
async def test_update_api_key_updates_expires_at() -> None:
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    future = datetime.now(UTC) + timedelta(days=60)
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"
    key.expires_at = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, expires_at=future)
    assert updated is not None
    assert updated.expires_at == future


# ---------------------------------------------------------------------------
# validate_api_key — revoked (filtered by SQL query, not code)
# ---------------------------------------------------------------------------
# Revoked key detection is handled by the WHERE clause in validate_api_key's
# SQL query (OrgApiKey.revoked_at.is_(None)). When the key is revoked, the
# query returns no results and ApiKeyInvalidError is raised via the "not found"
# path. This is tested by test_validate_api_key_not_found_raises above.


# ---------------------------------------------------------------------------
# mint_run_api_key / revoke_run_api_key (FAR-296 Phase 3b per-run keys)
# ---------------------------------------------------------------------------


def _mint_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_mint_run_api_key_mints_runner_role_with_ttl() -> None:
    session = _mint_session()
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    account_id = uuid.uuid4()
    ttl = 1800

    key, full_key = await mint_run_api_key(
        session,
        org_id=org_id,
        run_id=run_id,
        node_id="n1",
        account_id=account_id,
        ttl_seconds=ttl,
    )
    assert key is not None
    assert full_key.startswith("mk_")
    assert key.role == "runner"
    assert key.run_id == run_id
    assert key.organisation_id == org_id
    assert key.account_id == account_id
    assert key.name == f"run:{run_id}:node:n1"
    expected_expiry = datetime.now(UTC) + timedelta(seconds=ttl)
    assert key.expires_at > expected_expiry - timedelta(seconds=5)
    assert key.expires_at <= expected_expiry + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_mint_run_api_key_clamps_ttl() -> None:
    session = _mint_session()
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()

    key, _full_key = await mint_run_api_key(
        session,
        org_id=org_id,
        run_id=run_id,
        node_id="n1",
        account_id=uuid.uuid4(),
        ttl_seconds=10**9,
    )
    expected_expiry = datetime.now(UTC) + timedelta(seconds=86400)
    assert key.expires_at > expected_expiry - timedelta(seconds=5)
    assert key.expires_at <= expected_expiry + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_mint_run_api_key_fails_open_on_error() -> None:
    """A mint failure returns None (fail-open) instead of raising."""
    session = _mint_session()
    session.flush = AsyncMock(side_effect=RuntimeError("flush boom"))

    result = await mint_run_api_key(
        session,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        node_id="n1",
        account_id=uuid.uuid4(),
        ttl_seconds=1800,
    )
    assert result is None


@pytest.mark.asyncio
async def test_revoke_run_api_key_revokes_linked_keys() -> None:
    """The revocation UPDATE targets ONLY keys linked to the run_id + org.

    The ``revoked_at IS NULL`` + ``run_id`` predicates are compiled into the
    statement, so keys minted for other runs are untouched.
    """
    org_id = uuid.uuid4()
    run_a = uuid.uuid4()
    result = MagicMock()
    result.rowcount = 2

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    revoked = await revoke_run_api_key(session, run_id=run_a, org_id=org_id)
    assert revoked == 2

    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert run_a.hex in compiled
    assert org_id.hex in compiled
    assert "revoked_at" in compiled
    assert "IS NULL" in compiled.upper()


@pytest.mark.asyncio
async def test_revoke_run_api_key_zero_rows_returns_zero() -> None:
    """No linked keys -> rowcount 0 -> returns 0 (not None)."""
    result = MagicMock()
    result.rowcount = 0

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    revoked = await revoke_run_api_key(session, run_id=uuid.uuid4(), org_id=uuid.uuid4())
    assert revoked == 0


# ---------------------------------------------------------------------------
# revoke_run_api_key_sweep (FAR-296 Phase 3b-2 compensating revocation)
# ---------------------------------------------------------------------------


class _SweepBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _SweepSession:
    """Minimal session double for ``revoke_run_api_key_sweep``.

    ``begin()`` returns a no-op async context manager; ``in_transaction()`` is
    True so ``set_rls_org``'s active-transaction guard passes; ``get_bind()``
    reports a non-Postgres dialect so RLS goes through ``session.info`` (no
    ``set_config`` SQL); ``execute`` pops canned results in order and records
    every statement for assertion.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.executed: list[tuple[Any, Any]] = []
        self.info: dict[str, Any] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _SweepBegin:
        return _SweepBegin()

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        return bind

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        if "set_config" in str(stmt):
            return MagicMock()
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


def _sweep_keys_result(rows: list[tuple[uuid.UUID, uuid.UUID]]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _sweep_revoke_result(rowcount: int) -> MagicMock:
    r = MagicMock()
    r.rowcount = rowcount
    return r


@pytest.mark.asyncio
async def test_revoke_run_api_key_sweep_revokes_terminal_run_keys() -> None:
    """A terminal run's un-revoked per-run key is revoked and counted."""
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    run_id = uuid.uuid4()
    session = _SweepSession([_sweep_keys_result([(key_id, run_id)]), _sweep_revoke_result(1)])
    session_factory = MagicMock(return_value=session)

    result = await revoke_run_api_key_sweep(session_factory, org_ids=[org_id])

    assert result == {"scanned": 1, "revoked": 1, "errors": 0}
    # set_rls_org ran under the per-org RLS context (non-Postgres path).
    assert session.info.get("org_id") == org_id
    keys_stmt = session.executed[0][0]
    compiled = str(keys_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "org_api_keys" in compiled
    assert "runs" in compiled
    # The sweep selects ALL un-revoked keys for terminal runs — the run_id
    # appears only in the JOIN (runs.id = org_api_keys.run_id), never as a
    # WHERE literal, because the sweep revokes every leaked key in one pass.
    assert "JOIN runs ON runs.id = org_api_keys.run_id" in compiled


@pytest.mark.asyncio
async def test_revoke_run_api_key_sweep_skips_non_terminal_runs() -> None:
    """The keys-driven query filters by TERMINAL_STATUSES — an ACTIVE run
    (e.g. ``running``) is never selected for revocation, so its key survives
    the sweep. The WHERE clause must contain every terminal status and no
    non-terminal status."""
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    session = _SweepSession([_sweep_keys_result([(uuid.uuid4(), run_id)]), _sweep_revoke_result(0)])
    session_factory = MagicMock(return_value=session)

    result = await revoke_run_api_key_sweep(session_factory, org_ids=[org_id])

    assert result == {"scanned": 1, "revoked": 0, "errors": 0}
    stmt = session.executed[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    for status in sorted(TERMINAL_STATUSES):
        assert f"'{status}'" in compiled
    for status in ("running", "pending", "awaiting_human", "claimed"):
        assert f"'{status}'" not in compiled


@pytest.mark.asyncio
async def test_revoke_run_api_key_sweep_never_raises() -> None:
    """A session failure on the keys-driven query is swallowed and counted in
    ``errors`` — the sweep never raises."""
    org_id = uuid.uuid4()
    session = _SweepSession([])
    session.execute = AsyncMock(side_effect=RuntimeError("db boom"))
    session_factory = MagicMock(return_value=session)

    result = await revoke_run_api_key_sweep(session_factory, org_ids=[org_id])

    assert result["scanned"] == 0
    assert result["revoked"] == 0
    assert result["errors"] == 1


# ---------------------------------------------------------------------------
# revoke_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_api_key_success() -> None:
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.revoked_at = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    ok = await revoke_api_key(session, key_id, org_id)
    assert ok is True
    assert key.revoked_at is not None
    assert isinstance(key.revoked_at, datetime)


@pytest.mark.asyncio
async def test_revoke_api_key_not_found_returns_false() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    ok = await revoke_api_key(session, uuid.uuid4(), uuid.uuid4())
    assert ok is False


@pytest.mark.asyncio
async def test_revoke_api_key_already_revoked_returns_false() -> None:
    """Already-revoked keys are filtered out by the ``revoked_at IS NULL``
    WHERE clause, so the query returns no row and the function returns False."""
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()

    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    ok = await revoke_api_key(session, key_id, org_id)
    assert ok is False


@pytest.mark.asyncio
async def test_revoke_api_key_is_idempotent_when_query_returns_revoked_key() -> None:
    """If the query unexpectedly returns an already-revoked key, revoking
    again refreshes ``revoked_at`` and still reports success."""
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.revoked_at = datetime.now(UTC)

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    ok = await revoke_api_key(session, key_id, org_id)
    assert ok is True
    assert key.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_api_key_wrong_org_returns_false() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    ok = await revoke_api_key(session, uuid.uuid4(), uuid.uuid4())
    assert ok is False


# ---------------------------------------------------------------------------
# revoke_api_key — FOR UPDATE row lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_api_key_select_locks_row_for_update() -> None:
    """Concurrent revocations serialise: the SELECT takes a FOR UPDATE row lock."""
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.revoked_at = None

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    await revoke_api_key(session, key_id, org_id)

    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in compiled.upper()


# ---------------------------------------------------------------------------
# update_api_key — team-scope transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_api_key_clears_team_id() -> None:
    """A team-scoped key can be moved back to org-wide by passing team_id=None."""
    org_id = uuid.uuid4()
    key_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = key_id
    key.name = "Original"
    key.role = "operator"
    key.team_id = uuid.uuid4()

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key_id, org_id, team_id=None)
    assert updated is not None
    assert updated.team_id is None


@pytest.mark.asyncio
async def test_update_api_key_team_id_unset_leaves_scope_unchanged() -> None:
    """An update that omits team_id must not clear an existing team scope."""
    team_id = uuid.uuid4()
    key = MagicMock(spec=OrgApiKey)
    key.id = uuid.uuid4()
    key.name = "Original"
    key.role = "operator"
    key.team_id = team_id

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key.id, uuid.uuid4(), name="Renamed")
    assert updated is not None
    assert updated.team_id == team_id


@pytest.mark.asyncio
async def test_update_api_key_clear_team_id_allowed_for_admin_role_key() -> None:
    """Clearing the team scope is valid even for a legacy admin-role key — the
    admin-role restriction applies to team-SCOPED keys only."""
    key = MagicMock(spec=OrgApiKey)
    key.id = uuid.uuid4()
    key.name = "Original"
    key.role = "admin"
    key.team_id = uuid.uuid4()

    result = MagicMock()
    result.scalar_one_or_none.return_value = key

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    updated = await update_api_key(session, key.id, uuid.uuid4(), team_id=None)
    assert updated is not None
    assert updated.team_id is None


def test_unset_sentinel_is_object() -> None:
    """The ``_UNSET`` sentinel is a single shared object, distinct from None."""
    assert _UNSET is not None
    assert _UNSET is not False


# ---------------------------------------------------------------------------
# OrgApiKey.lookup_prefix — exactly 8 chars at the column level
# ---------------------------------------------------------------------------


def test_lookup_prefix_column_is_string_8() -> None:
    """The DB column is String(8): shorter/longer prefixes never match the index."""
    column = OrgApiKey.__table__.c.lookup_prefix
    assert column.type.length == 8
