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
from modulo.db.rls import read_rls_org, set_rls_org
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
