"""Integration tests for the org-independent durable audit ledger (FAR-1517).

``audit_events.organisation_id`` FKs ``organisations.id`` with ``ON DELETE
CASCADE``, so hard-deleting an organisation destroyed its entire audit trail —
including the ``org_deletion_requested`` row written moments earlier — and a
post-commit append could never satisfy the FK afterwards.

These tests run against a real Postgres (testcontainers, real migrations) and
pin the fix:

* the durable ``system_audit_events`` records written by the org-deletion
  lifecycle SURVIVE the hard delete;
* a normal org-scoped ``audit_events`` row is still cascade-deleted (the
  CASCADE itself is untouched);
* the table carries no FK and no ``organisation_id`` tenant column, so no
  cascade and no RLS scope can ever reach it;
* the append-only triggers reject UPDATE/DELETE;
* a failed durable append aborts the destructive act (fail-closed).
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = [
    pytest.mark.integration,
]


# ── Helpers ──────────────────────────────────────────────────────────


async def _create_org(db_engine: AsyncEngine, suffix: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": f"System Audit {suffix}", "slug": f"sys-audit-{suffix}-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_user(db_engine: AsyncEngine, org_id: uuid.UUID, name: str) -> uuid.UUID:
    """Create the org's admin. ``accounts.email`` is globally unique, so the
    address is derived from the (always unique) org id — a shared literal such
    as ``cancel@test.com`` collides with another test file's fixture in the
    same database session."""
    account_id = uuid.uuid4()
    email = f"{name}-{org_id.hex[:8]}@test.com"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {"id": str(account_id), "email": email, "name": name},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _count(db_engine: AsyncEngine, sql: str, **params: str) -> int:
    async with db_engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return int(result.scalar_one())


async def _create_error_event(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    """An ordinary append-only child row (error_events shares the CASCADE +
    append-only guard conflict that 0285 also had to unblock)."""
    error_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO error_events (id, organisation_id, fingerprint, level, message, source) "
                "VALUES (:id, :oid, :fp, 'error', 'probe', 'backend')",
            ),
            {"id": str(error_id), "oid": str(org_id), "fp": f"fp-{error_id.hex[:16]}"},
        )
    return error_id


async def _durable_rows(db_engine: AsyncEngine, org_id: uuid.UUID | None = None) -> list[tuple[Any, ...]]:
    """Read durable rows through the ORM (driver-independent JSON decoding)."""
    from modulo.db.models.system_audit_event import SystemAuditEvent

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        stmt = select(SystemAuditEvent).order_by(SystemAuditEvent.event_type)
        if org_id is not None:
            stmt = stmt.where(SystemAuditEvent.org_id == org_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [(r.event_type, r.org_id, r.actor_user_id, r.payload_json, r.created_at) for r in rows]


async def _set_org_ctx(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.organisation_id', :oid, true)"),
        {"oid": str(org_id)},
    )


def _lifecycle_types(rows: list[tuple[Any, ...]]) -> list[str]:
    return sorted(row[0] for row in rows)


# ── The gap: durable records survive, org-scoped records cascade away ──


class TestLifecycleRecordsSurviveHardDelete:
    async def test_requested_and_completed_survive_while_chained_events_cascade(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """The whole point of FAR-1517, end to end on real Postgres.

        Sequence mirrors the two routes: request (org-scoped chained event +
        durable copy) then confirm (durable copy written in-transaction BEFORE
        the org row is removed, then the hard delete).
        """
        from modulo.core.audit_logger import append_audit_event
        from modulo.core.system_audit_logger import append_system_audit_event
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "survive")
        user_id = await _create_user(db_engine, org_id, "survive")
        error_id = await _create_error_event(db_engine, org_id)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            result = await request_org_deletion(session, org_id, user_id)
            # The org-scoped chained event the route writes today…
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="org_deletion_requested",
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"note": "org-scoped copy — must cascade away"},
            )
            # …and its durable twin.
            await append_system_audit_event(
                session,
                event_type="org_deletion_requested",
                org_id=org_id,
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"flow": "deletion_request", "immediate": False, "organisation_name": "survive"},
            )
            await session.commit()

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            # Durable record BEFORE the org row is removed, same transaction.
            await append_system_audit_event(
                session,
                event_type="org_deletion_completed",
                org_id=org_id,
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"flow": "deletion_confirm", "immediate": False, "force": False},
            )
            outcome = await confirm_org_deletion(session, org_id=org_id, token=result["token"])
            await session.commit()
        assert outcome["deleted_organisation_id"] == str(org_id)

        # The org is gone…
        assert await _count(db_engine, "SELECT COUNT(*) FROM organisations WHERE id = :id", id=str(org_id)) == 0

        # …its org-scoped audit trail went with it (CASCADE untouched)…
        chained = await _count(
            db_engine,
            "SELECT COUNT(*) FROM audit_events WHERE organisation_id = :oid",
            oid=str(org_id),
        )
        assert chained == 0

        # …and the append-only error_events row went with it too — 0285 lets
        # the RI cascade through BOTH guarded tables (it used to raise).
        error_rows = await _count(db_engine, "SELECT COUNT(*) FROM error_events WHERE id = :id", id=str(error_id))
        assert error_rows == 0

        # …but BOTH durable lifecycle records survived, carrying the plain
        # org id, the actor, and a timestamp.
        rows = await _durable_rows(db_engine, org_id)
        assert _lifecycle_types(rows) == ["org_deletion_completed", "org_deletion_requested"]
        for _event_type, row_org, actor, payload, created_at in rows:
            assert row_org == org_id
            assert actor == user_id
            assert payload["organisation_id"] == str(org_id)
            assert payload["actor_user_id"] == str(user_id)
            assert created_at is not None
        requested = next(row for row in rows if row[0] == "org_deletion_requested")
        assert requested[3]["flow"] == "deletion_request"
        assert requested[3]["organisation_name"] == "survive"

    async def test_normal_org_scoped_event_still_cascades(self, db_engine: AsyncEngine) -> None:
        """The CASCADE itself is unchanged: a normal org-scoped event dies,
        and the delete fabricates no durable rows of its own."""
        from modulo.core.audit_logger import append_audit_event
        from modulo.core.system_audit_logger import append_system_audit_event
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "cascade-only")
        user_id = await _create_user(db_engine, org_id, "cascade-only")
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            result = await request_org_deletion(session, org_id, user_id)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="pipeline_created",
                actor_user_id=user_id,
                resource_type="pipeline",
                payload_json={"note": "ordinary org-scoped event"},
            )
            await session.commit()

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            # Durable write of the COMPLETED act only — nothing requested.
            await append_system_audit_event(
                session,
                event_type="org_deletion_completed",
                org_id=org_id,
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"flow": "deletion_confirm"},
            )
            await confirm_org_deletion(session, org_id=org_id, token=result["token"])
            await session.commit()

        chained = await _count(
            db_engine,
            "SELECT COUNT(*) FROM audit_events WHERE organisation_id = :oid",
            oid=str(org_id),
        )
        assert chained == 0

        rows = await _durable_rows(db_engine, org_id)
        assert len(rows) == 1
        assert rows[0][0] == "org_deletion_completed"

    async def test_admin_delete_org_flow_survives(self, db_engine: AsyncEngine) -> None:
        """The system-admin path (``admin_orgs:admin_delete_org``) — durable
        write first, then the same ``delete_organisation`` hard delete."""
        from modulo.core.system_audit_logger import append_system_audit_event
        from modulo.db.crud.organisation import delete_organisation

        org_id = await _create_org(db_engine, "admin-path")
        user_id = await _create_user(db_engine, org_id, "admin-path")
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async with factory() as session:
            await append_system_audit_event(
                session,
                event_type="org_deletion_completed",
                org_id=org_id,
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"flow": "admin_delete_org", "immediate": True},
            )
            deleted = await delete_organisation(session, org_id)
            await session.commit()
        assert deleted

        assert await _count(db_engine, "SELECT COUNT(*) FROM organisations WHERE id = :id", id=str(org_id)) == 0

        rows = await _durable_rows(db_engine, org_id)
        assert len(rows) == 1
        assert rows[0][3]["flow"] == "admin_delete_org"

    async def test_request_and_cancel_records_persist_while_org_lives(self, db_engine: AsyncEngine) -> None:
        """Request + cancel are lifecycle evidence too, recorded durably even
        though no delete happens (the pending deletion may be confirmed later)."""
        from modulo.core.system_audit_logger import append_system_audit_event
        from modulo.db.crud.org_deletion import cancel_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "cancel")
        user_id = await _create_user(db_engine, org_id, "cancel")
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            await request_org_deletion(session, org_id, user_id)
            await append_system_audit_event(
                session,
                event_type="org_deletion_requested",
                org_id=org_id,
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"flow": "deletion_request"},
            )
            await session.commit()

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            result = await cancel_org_deletion(session, org_id)
            await append_system_audit_event(
                session,
                event_type="org_deletion_cancelled",
                org_id=org_id,
                actor_user_id=user_id,
                resource_type="organisation",
                resource_id=org_id,
                payload_json={"flow": "deletion_cancel"},
            )
            await session.commit()
        assert result["status"] == "active"

        status = await _count(
            db_engine, "SELECT COUNT(*) FROM organisations WHERE id = :id AND status = 'active'", id=str(org_id)
        )
        assert status == 1

        rows = await _durable_rows(db_engine, org_id)
        assert _lifecycle_types(rows) == ["org_deletion_cancelled", "org_deletion_requested"]


# ── Structural guarantees ────────────────────────────────────────────


class TestDurableLedgerStructure:
    async def test_no_foreign_keys_and_no_tenant_column(self, db_engine: AsyncEngine) -> None:
        """No FK means no cascade can reach the ledger; no ``organisation_id``
        column means the tenancy regime (RLS policy + ORM tenant filter) never
        scopes it out — both are the mechanism, not an accident."""
        async with db_engine.connect() as conn:
            cols = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'system_audit_events'"
                ),
            )
            names = {row[0] for row in cols.all()}
            assert "organisation_id" not in names
            assert "org_id" in names

            fks = await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE contype = 'f' AND conrelid = 'public.system_audit_events'::regclass"
                ),
            )
            assert not fks.all()

            table = await conn.execute(
                text("SELECT 1 FROM pg_class WHERE relname = 'system_audit_events'"),
            )
            assert table.first() is not None

    async def test_append_only_triggers_reject_update_and_delete(self, db_engine: AsyncEngine) -> None:
        """Evidence that can be edited is not evidence: the database refuses
        UPDATE and DELETE, while SELECT and INSERT keep working."""
        from modulo.core.system_audit_logger import append_system_audit_event

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await append_system_audit_event(
                session,
                event_type="append_only_probe",
                payload_json={"probe": True},
            )
            await session.commit()

        async with db_engine.connect() as conn:
            with pytest.raises(SQLAlchemyError):
                async with conn.begin():
                    await conn.execute(
                        text(
                            "UPDATE system_audit_events SET event_type = 'tampered' "
                            "WHERE event_type = 'append_only_probe'"
                        )
                    )

        async with db_engine.connect() as conn:
            with pytest.raises(SQLAlchemyError):
                async with conn.begin():
                    await conn.execute(text("DELETE FROM system_audit_events WHERE event_type = 'append_only_probe'"))

        # The row is untouched, and SELECT still works.
        probes = await _count(
            db_engine,
            "SELECT COUNT(*) FROM system_audit_events WHERE event_type = 'append_only_probe'",
        )
        assert probes == 1
        tampered = await _count(
            db_engine,
            "SELECT COUNT(*) FROM system_audit_events WHERE event_type = 'tampered'",
        )
        assert tampered == 0

    async def test_append_only_guards_allow_only_ri_cascades(self, db_engine: AsyncEngine) -> None:
        """Both shared guards carry the 0285 depth test: a direct statement
        (depth 1) still raises, an ON DELETE CASCADE from ``organisations``
        (wrapped by the RI system's own trigger) passes. Direct tampering is
        separately pinned by ``tests/integration/test_audit_append_only.py``.
        """
        async with db_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT proname, prosrc FROM pg_proc "
                    "WHERE proname IN ('audit_events_append_only', 'error_events_append_only')"
                ),
            )
            sources = {row[0]: row[1] for row in rows.all()}

        assert sorted(sources) == ["audit_events_append_only", "error_events_append_only"]
        for name, source in sources.items():
            assert "pg_trigger_depth()" in source, f"{name} must distinguish an RI cascade from a direct DELETE"
            assert "<= 1" in source, f"{name} must still block a direct (depth-1) DELETE"
            assert "RETURN OLD" in source, f"{name} must let the cascaded delete through, not skip it (NULL)"

    async def test_failed_durable_append_aborts_the_deletion(self, db_engine: AsyncEngine) -> None:
        """Fail-closed: the durable write happens BEFORE the org row is
        removed, inside the same transaction — when it fails, the whole
        destructive act rolls back and the org survives.

        The failure is induced with an ``event_type`` longer than the
        ``varchar(100)`` column, a real driver error rather than a mock.
        """
        from modulo.core.system_audit_logger import append_system_audit_event
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "fail-closed")
        user_id = await _create_user(db_engine, org_id, "fail-closed")
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async with factory() as session:
            await _set_org_ctx(session, org_id)
            await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async def _deletion_with_failing_append() -> None:
            async with factory() as session, session.begin():
                await _set_org_ctx(session, org_id)
                await append_system_audit_event(
                    session,
                    event_type="x" * 101,  # over varchar(100) → driver error
                    org_id=org_id,
                    actor_user_id=user_id,
                    resource_type="organisation",
                    resource_id=org_id,
                    payload_json={"flow": "deletion_confirm"},
                )
                # Never reached: the append raised first.
                await confirm_org_deletion(session, org_id=org_id, token="ignored", immediate=True)

        with pytest.raises(SQLAlchemyError):
            await _deletion_with_failing_append()

        # The org still exists — the deletion was aborted, not partially done.
        survivors = await _count(db_engine, "SELECT COUNT(*) FROM organisations WHERE id = :id", id=str(org_id))
        assert survivors == 1
        # And nothing durable was written for the failed attempt.
        assert not await _durable_rows(db_engine, org_id)
