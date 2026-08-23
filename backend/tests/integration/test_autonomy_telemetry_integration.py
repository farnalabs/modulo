"""Integration test: emit_autonomy_telemetry actually persists the row under RLS.

The original implementation neither set RLS context nor committed, so the
``run.autonomy_level_applied`` event was silently rolled back / rejected by the
STRICT-RLS ``audit_events`` policy and never recorded in production. This test
round-trips the real payload through a real session + the real ``append_audit_event``
and asserts the row lands — it fails against the buggy implementation.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from modulo.core.run_context import autonomy_telemetry as at
from modulo.db.rls import set_rls_org


@pytest.mark.asyncio
async def test_emit_autonomy_telemetry_persists_row(app_engine: object, test_org: uuid.UUID) -> None:
    org_id = test_org
    run_id = uuid.uuid4()
    gate_id = "g-persist"
    pipeline_id = uuid.uuid4()

    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    # session_factory mimics the executor's async_sessionmaker() callable.
    session_factory = lambda: factory()  # noqa: E731

    await at.emit_autonomy_telemetry(
        session_factory,
        org_id=org_id,
        run_id=run_id,
        gate_id=gate_id,
        autonomy_level="fully_autonomous",
        gate_outcome="skipped",
        pipeline_id=pipeline_id,
        human_only=False,
    )

    # Read back with RLS scoped to the same org. Under the runtime (non-superuser)
    # role the audit_events policy filters by app.organisation_id, so the read
    # session must also set RLS context.
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        result = await session.execute(
            text(
                "SELECT event_type, resource_id, payload_json "
                "FROM audit_events "
                "WHERE event_type = :et AND organisation_id = :oid"
            ),
            {"et": at.AUTONOMY_LEVEL_APPLIED, "oid": str(org_id)},
        )
        rows = result.all()

    assert len(rows) == 1, "telemetry event was not persisted to audit_events"
    event_type, resource_id, payload = rows[0]
    assert event_type == at.AUTONOMY_LEVEL_APPLIED
    assert str(resource_id) == str(run_id)
    assert payload["gate_id"] == gate_id
    assert payload["gate_outcome"] == "skipped"
    assert payload["autonomy_level"] == "fully_autonomous"
    assert str(payload["pipeline_id"]) == str(pipeline_id)
    assert payload["human_only"] is False


@pytest.mark.asyncio
async def test_emit_autonomy_telemetry_is_fail_open(app_engine: object, test_org: uuid.UUID) -> None:
    """A telemetry failure (bad run_id type rejected by RLS/org checks) must not
    raise — emission is fail-open."""
    org_id = test_org
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session_factory = lambda: factory()  # noqa: E731

    # emit_autonomy_telemetry swallows all exceptions; this must not raise.
    await at.emit_autonomy_telemetry(
        session_factory,
        org_id=org_id,
        run_id="not-a-uuid",
        gate_id="g-failopen",
        autonomy_level="manual_approval",
        gate_outcome="fired",
    )

    # Fail-open means the broken call must neither raise (above) nor persist a
    # malformed row. Confirm the rejected event was swallowed, not stored.
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        result = await session.execute(
            text("SELECT COUNT(*) FROM audit_events WHERE event_type = :et AND payload_json->>'gate_id' = :gid"),
            {"et": at.AUTONOMY_LEVEL_APPLIED, "gid": "g-failopen"},
        )
        count = result.scalar()

    assert count == 0, "fail-open telemetry should not persist a malformed event"
