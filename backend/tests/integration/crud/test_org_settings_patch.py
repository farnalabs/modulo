"""Integration tests for the atomic org ``settings_json`` patch (FAR-1830).

Real Postgres via testcontainers: the JSONB ``||`` composition and the
``- text[]`` clear form are Postgres operators, so the lost-update guarantee can
only be proven against a real engine (a mocked session validates the mock).

Every test here fails with the OLD whole-column read-modify-write: the second
writer wrote the whole stale column back and dropped the first writer's key.
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.organisation import (
    OrgSettingsPatchContextAbsentError,
    OrgSettingsPatchContextUnreadableError,
    OrgSettingsPatchOrgMismatch,
    _apply_settings_patch_generic,
    get_organisation,
    update_org_settings_patch,
)
from modulo.db.rls import read_rls_org, set_rls_org, set_rls_user_context
from modulo.db.soft_delete import register_soft_delete_filter

# FAR-1830: the generic patch path relies on the global soft-delete filter,
# which is installed by an import side effect. Register it explicitly so this
# module's soft-delete assertions run against the production filter.
register_soft_delete_filter()

pytestmark = [
    pytest.mark.integration,
]


async def _create_org(db_engine: AsyncEngine, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::jsonb)",
            ),
            {"id": str(org_id), "name": f"Org {slug}", "slug": slug},
        )
    return org_id


async def _read_settings(db_engine: AsyncEngine, org_id: uuid.UUID) -> dict[str, Any]:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT settings_json FROM organisations WHERE id = :oid"),
            {"oid": str(org_id)},
        )
        raw = result.scalar_one()
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


async def test_two_writers_of_different_concerns_both_survive_k1_then_k2(db_engine: AsyncEngine) -> None:
    """A stale reader's later patch must not drop a concurrent writer's key."""
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session1 = factory()
    await session1.begin()
    await set_rls_org(session1, org_id)
    # Snapshot the row in this txn, then let another writer commit a DIFFERENT
    # concern before this session writes its own key.
    await get_organisation(session1, org_id)

    session2 = factory()
    await session2.begin()
    await set_rls_org(session2, org_id)
    await update_org_settings_patch(session2, org_id, {"k2": 2})
    await session2.commit()
    await session2.close()

    await update_org_settings_patch(session1, org_id, {"k1": 1})
    await session1.commit()
    await session1.close()

    settings = await _read_settings(db_engine, org_id)
    assert settings["k1"] == 1
    assert settings["k2"] == 2


async def test_two_writers_of_different_concerns_both_survive_k2_then_k1(db_engine: AsyncEngine) -> None:
    """The reverse composition order preserves both keys too."""
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session1 = factory()
    await session1.begin()
    await set_rls_org(session1, org_id)
    await get_organisation(session1, org_id)

    session2 = factory()
    await session2.begin()
    await set_rls_org(session2, org_id)
    await update_org_settings_patch(session2, org_id, {"k1": 1})
    await session2.commit()
    await session2.close()

    await update_org_settings_patch(session1, org_id, {"k2": 2})
    await session1.commit()
    await session1.close()

    settings = await _read_settings(db_engine, org_id)
    assert settings["k1"] == 1
    assert settings["k2"] == 2


async def test_email_cross_transaction_shape_does_not_lose_a_concurrent_flag(db_engine: AsyncEngine) -> None:
    """Replicates ``admin_update_email_settings``'s read-txn1 / write-txn2 shape.

    The email writer reads the org (for its sub-object merge) in one
    transaction and writes in a SECOND transaction. A concurrent settings write
    committed between the two must survive the email write.
    """
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session1 = factory()
    await session1.begin()
    await set_rls_org(session1, org_id)
    org = await get_organisation(session1, org_id)
    assert org is not None
    existing_email = dict((org.settings_json or {}).get("email", {}))
    merged_email = dict(existing_email)
    merged_email["smtp_host"] = "smtp.example.com"

    # A concurrent writer commits a different concern between the two txns.
    session2 = factory()
    await session2.begin()
    await set_rls_org(session2, org_id)
    await update_org_settings_patch(session2, org_id, {"feature_overrides": {"demo_flag": True}})
    await session2.commit()
    await session2.close()

    await update_org_settings_patch(session1, org_id, {"email": merged_email})
    await session1.commit()
    await session1.close()

    settings = await _read_settings(db_engine, org_id)
    assert settings["feature_overrides"] == {"demo_flag": True}
    assert settings["email"]["smtp_host"] == "smtp.example.com"


async def test_delete_keys_removes_only_named_top_level_keys(db_engine: AsyncEngine) -> None:
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    session = factory()
    await session.begin()
    await set_rls_org(session, org_id)
    await update_org_settings_patch(session, org_id, {"keep": 1, "drop": 2})
    await update_org_settings_patch(session, org_id, delete_keys=["drop"])
    await session.commit()
    await session.close()

    settings = await _read_settings(db_engine, org_id)
    assert settings["keep"] == 1
    assert "drop" not in settings


async def test_cross_org_patch_is_refused(db_engine: AsyncEngine) -> None:
    """A session bound to one org cannot patch another org's settings."""
    org_a = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    org_b = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session = factory()
    await session.begin()
    await set_rls_org(session, org_b)
    assert await read_rls_org(session) == org_b
    with pytest.raises(OrgSettingsPatchOrgMismatch):
        await update_org_settings_patch(session, org_a, {"logo_url": "https://x/l.png"})
    await session.rollback()
    await session.close()

    # Neither org was mutated.
    assert not await _read_settings(db_engine, org_a)
    assert not await _read_settings(db_engine, org_b)


async def test_org_lookup_returns_merged_value_and_syncs_identity_map(db_engine: AsyncEngine) -> None:
    """The helper returns the merged mapping and refreshes the loaded row."""
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    session = factory()
    await session.begin()
    await set_rls_org(session, org_id)
    org = await get_organisation(session, org_id)
    assert org is not None

    returned = await update_org_settings_patch(session, org_id, {"logo_url": "https://x/l.png"})
    assert returned["logo_url"] == "https://x/l.png"
    # The in-session row is refreshed WITHOUT a follow-up whole-column write.
    assert org.settings_json["logo_url"] == "https://x/l.png"
    await session.commit()
    await session.close()


async def _soft_delete_org(db_engine: AsyncEngine, org_id: uuid.UUID) -> None:
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE organisations SET deleted_at = now() WHERE id = :oid"),
            {"oid": str(org_id)},
        )


async def test_soft_deleted_org_is_refused_on_both_paths(db_engine: AsyncEngine) -> None:
    """A soft-deleted org is refused identically on the Postgres and generic paths.

    The raw Postgres statement carries an explicit ``deleted_at IS NULL``
    predicate; the generic path gets it via the global ORM filter. Before the
    fix the Postgres path patched the soft-deleted row while the generic path
    raised — silent prod-vs-test divergence.
    """
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    await _soft_delete_org(db_engine, org_id)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session = factory()
    await session.begin()
    await set_rls_org(session, org_id)
    # Postgres path (the default dialect on this engine).
    with pytest.raises(LookupError):
        await update_org_settings_patch(session, org_id, {"k": 1})
    # Generic path, driven directly against the same engine.
    with pytest.raises(LookupError):
        await _apply_settings_patch_generic(session, org_id, {"k": 1}, [])
    await session.rollback()
    await session.close()

    # Neither path mutated the soft-deleted row.
    assert not await _read_settings(db_engine, org_id)


async def test_unbound_context_is_refused_by_default(db_engine: AsyncEngine) -> None:
    """A session with no org context is refused — the guard fails CLOSED."""
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    session = factory()
    await session.begin()

    with pytest.raises(OrgSettingsPatchContextAbsentError):
        await update_org_settings_patch(session, org_id, {"k": 1})
    await session.rollback()
    await session.close()

    assert not await _read_settings(db_engine, org_id)


async def test_allow_unbound_context_permits_a_boot_seed_write(db_engine: AsyncEngine) -> None:
    """Only an explicit opt-in lets a genuinely-unbound context write."""
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    session = factory()
    await session.begin()

    returned = await update_org_settings_patch(session, org_id, {"k": 1}, allow_unbound_context=True)
    await session.commit()
    await session.close()

    assert returned["k"] == 1
    assert await _read_settings(db_engine, org_id) == {"k": 1}


async def test_context_read_failure_fails_closed(db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed READ of the bound context is never treated as 'unbound / allowed'."""
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    session = factory()
    await session.begin()

    real_execute = session.execute

    async def _boom(statement: object, *args: object, **kwargs: object) -> object:
        if "current_setting('app.organisation_id'" in str(statement):
            raise RuntimeError("guc read failed")
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", _boom)
    with pytest.raises(OrgSettingsPatchContextUnreadableError):
        await update_org_settings_patch(session, org_id, {"k": 1}, allow_unbound_context=True)
    await session.rollback()
    await session.close()

    assert not await _read_settings(db_engine, org_id)


# ---------------------------------------------------------------------------
# FAR-1832 (Land 1b): converted locked writers — same-concern composition,
# deletion-FSM exclusivity, and the licence path driven through the real route.
# ---------------------------------------------------------------------------


async def test_two_writers_of_the_same_concern_serialise_to_one_whole_value(db_engine: AsyncEngine) -> None:
    """Two writers of the SAME top-level key serialise to exactly one writer's
    whole value — never a blend of both.

    The jsonb ``||`` composition replaces the whole top-level value, so the
    final block is the last writer's dict verbatim (a hypothetical per-subkey
    merge would keep the other writer's ``extra`` key and fail this).
    """
    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session0 = factory()
    await session0.begin()
    await set_rls_org(session0, org_id)
    await update_org_settings_patch(session0, org_id, {"block": {"from": "seed"}, "sibling": 1})
    await session0.commit()
    await session0.close()

    # Writer 1 snapshots the row with a lockless read (a read-modify-write
    # reads WITHOUT a lock — a patch here would hold a row lock for the whole
    # txn and deadlock this very test against writer 2).
    session1 = factory()
    await session1.begin()
    await set_rls_org(session1, org_id)
    snapshot = await get_organisation(session1, org_id)
    assert snapshot is not None
    stale_block = dict((snapshot.settings_json or {}).get("block") or {})

    # Writer 2 writes the SAME key from its own state and commits in between.
    session2 = factory()
    await session2.begin()
    await set_rls_org(session2, org_id)
    await update_org_settings_patch(session2, org_id, {"block": {"writer": "two", "extra": "e2"}})
    await session2.commit()
    await session2.close()

    stale_block["writer"] = "one"
    await update_org_settings_patch(session1, org_id, {"block": stale_block})
    await session1.commit()
    await session1.close()

    settings = await _read_settings(db_engine, org_id)
    assert settings["block"] == {"from": "seed", "writer": "one"}, (
        f"same-concern writers must serialise to ONE whole value, got {settings['block']!r}"
    )
    assert "extra" not in settings["block"], "a per-subkey blend would keep the other writer's key"
    assert settings["sibling"] == 1


async def test_config_patch_wait_bounds_against_an_inflight_deletion(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings patch issued while a deletion FSM transaction holds the org
    row ``FOR UPDATE`` BLOCKS, then fails at the bounded lock wait — never an
    unbounded hang (R5) — and the blocked write never lands.

    This pins the FAR-1832 invariant: the converted config writers rely on the
    statement-level row lock (which still conflicts with the deletion FSM's
    ``FOR UPDATE``), so deletion exclusivity is preserved even though the
    config writers no longer pre-lock.
    """
    import time
    from types import SimpleNamespace

    from sqlalchemy.exc import DBAPIError

    from modulo.db.crud.org_deletion import request_org_deletion

    # Bound the wait at 300 ms so the test proves "wait, then bound" quickly.
    monkeypatch.setattr(
        "modulo.db.crud.row_lock.get_settings",
        lambda: SimpleNamespace(mutation_row_lock_timeout_ms=300),
    )

    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # In-flight deletion: the FSM crud holds the org row FOR UPDATE until A ends.
    session_a = factory()
    await session_a.begin()
    await request_org_deletion(session_a, org_id, uuid.uuid4())

    try:
        session_b = factory()
        await session_b.begin()
        await set_rls_org(session_b, org_id)

        started = time.monotonic()
        # asyncpg's lock-timeout surfaces as the dialect's base DBAPIError
        # ("canceling statement due to lock timeout", SQLSTATE 55P03).
        with pytest.raises(DBAPIError, match="lock timeout"):
            await update_org_settings_patch(session_b, org_id, {"k": 1})
        elapsed = time.monotonic() - started
        await session_b.rollback()
        await session_b.close()

        assert elapsed >= 0.1, f"the patch must WAIT on the deletion's lock, not fail for another reason: {elapsed}s"
        assert elapsed < 5.0, f"the lock wait must be bounded, not hang: {elapsed}s"
        # The blocked patch never wrote.
        assert not await _read_settings(db_engine, org_id)
    finally:
        await session_a.rollback()
        await session_a.close()


async def test_child_fk_insert_queues_behind_an_inflight_deletion(db_engine: AsyncEngine) -> None:
    """A child-row FK insert (which takes FOR KEY SHARE on the referenced org)
    still QUEUES behind a deletion transaction's ``FOR UPDATE`` — the FSM's
    exclusivity over child inserts is untouched by FAR-1832 (its own locks were
    deliberately kept)."""
    import asyncio

    from modulo.db.crud.org_deletion import request_org_deletion
    from modulo.db.models.audit_event import AuditEvent

    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    session_a = factory()
    await session_a.begin()
    await request_org_deletion(session_a, org_id, uuid.uuid4())

    session_b = factory()
    await session_b.begin()
    await set_rls_org(session_b, org_id)
    await set_rls_user_context(session_b, uuid.uuid4(), "admin")
    session_b.add(AuditEvent(organisation_id=org_id, event_type="far1832_fk_probe"))
    flush_task = asyncio.ensure_future(session_b.flush())

    await asyncio.sleep(0.5)
    assert not flush_task.done(), "the child FK insert must QUEUE behind the deletion's FOR UPDATE"

    await session_a.rollback()
    await session_a.close()

    await asyncio.wait_for(flush_task, timeout=10)
    # The probe row only existed to prove queueing — discard it.
    await session_b.rollback()
    await session_b.close()


async def test_licence_clear_path_uses_delete_keys_and_keeps_siblings(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL ``DELETE /{org}/license`` handler against Postgres and
    assert both halves of the contract: the writer calls the patch helper's
    ``delete_keys`` form (no whole-column blob), and on the real engine the key
    is ABSENT afterwards while sibling settings survive."""
    from unittest.mock import AsyncMock

    from modulo.api.routes import admin_orgs
    from modulo.api.routes.admin_orgs import admin_remove_org_license
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings

    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    seed = factory()
    await seed.begin()
    await set_rls_org(seed, org_id)
    await update_org_settings_patch(seed, org_id, {"license_key": "old.key", "retention_days": 30})
    await seed.commit()
    await seed.close()

    recorded: dict[str, Any] = {}
    real_patch = admin_orgs.update_org_settings_patch

    async def _spy(session_arg: Any, oid: Any, patch: Any = None, delete_keys: Any = None, **kwargs: Any) -> Any:
        recorded["patch"] = patch
        recorded["delete_keys"] = delete_keys
        return await real_patch(session_arg, oid, patch, delete_keys=delete_keys, **kwargs)

    monkeypatch.setattr(admin_orgs, "update_org_settings_patch", _spy)
    monkeypatch.setattr(admin_orgs, "invalidate_license_status_cache", AsyncMock())

    principal = AuthenticatedPrincipal(
        username="admin",
        organisation_id=org_id,
        account_id=uuid.uuid4(),
        org_role="admin",
    )
    handler_session = factory()
    await admin_remove_org_license(org_id, principal, handler_session, get_settings())

    settings = await _read_settings(db_engine, org_id)
    assert "license_key" not in settings, f"the clear must remove the key on a real engine: {settings!r}"
    assert settings.get("retention_days") == 30, f"sibling settings must survive the clear: {settings!r}"
    assert recorded.get("delete_keys") == ["license_key"], f"expected the delete_keys form, got {recorded!r}"
    assert not recorded.get("patch"), f"no whole-column blob may be passed, got {recorded!r}"


async def test_licence_set_path_patches_only_license_key(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL ``PUT /{org}/license`` handler against Postgres: the
    write emits ONLY ``license_key`` through the atomic patch, and sibling
    settings survive the real jsonb composition."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from modulo.api.routes import admin_orgs
    from modulo.api.routes.admin_orgs import SetOrgLicenseRequest, admin_set_org_license
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings

    org_id = await _create_org(db_engine, f"org-{uuid.uuid4().hex[:8]}")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    seed = factory()
    await seed.begin()
    await set_rls_org(seed, org_id)
    await update_org_settings_patch(seed, org_id, {"license_key": "old.key", "retention_days": 30})
    await seed.commit()
    await seed.close()

    monkeypatch.setattr(
        "modulo.core.license.parse_and_verify",
        MagicMock(
            return_value=SimpleNamespace(
                valid=True,
                error=None,
                license_data=SimpleNamespace(tier="team", features=["sso"], expires_at=None, org_id=None),
            )
        ),
    )
    monkeypatch.setattr(admin_orgs, "invalidate_license_status_cache", AsyncMock())

    recorded: dict[str, Any] = {}
    real_patch = admin_orgs.update_org_settings_patch

    async def _spy(session_arg: Any, oid: Any, patch: Any = None, delete_keys: Any = None, **kwargs: Any) -> Any:
        recorded["patch"] = patch
        recorded["delete_keys"] = delete_keys
        return await real_patch(session_arg, oid, patch, delete_keys=delete_keys, **kwargs)

    monkeypatch.setattr(admin_orgs, "update_org_settings_patch", _spy)

    principal = AuthenticatedPrincipal(
        username="admin",
        organisation_id=org_id,
        account_id=uuid.uuid4(),
        org_role="admin",
    )
    handler_session = factory()
    await admin_set_org_license(
        org_id, SetOrgLicenseRequest(license_key="new.key"), principal, handler_session, get_settings()
    )

    settings = await _read_settings(db_engine, org_id)
    assert settings.get("license_key") == "new.key", f"the set must land on a real engine: {settings!r}"
    assert settings.get("retention_days") == 30, f"sibling settings must survive the set: {settings!r}"
    assert recorded.get("patch") == {"license_key": "new.key"}, f"the write must emit only its own key: {recorded!r}"
    assert not recorded.get("delete_keys"), f"the set path must not delete keys: {recorded!r}"
