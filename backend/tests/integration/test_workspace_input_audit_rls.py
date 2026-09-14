"""Integration tests: FAR-801 workspace-input audit persistence under real RLS.

The audit-persistence blocks in ``node_runner`` open a fresh
``_audit_session`` and call ``record_resolved_inputs`` / ``record_drift``.
``run_node_outputs`` is ``RLS ENABLE + FORCE`` (migration 0192) and the
``rls_org_isolation`` policy scopes every command to the session's
``app.organisation_id`` GUC.  The write therefore REQUIRES
``set_rls_org(session, org_id)`` to have been called inside the same
transaction — without it the INSERT is rejected with ``42501`` and the
best-effort write is swallowed, so the audit row silently never lands.

These tests drive the real persistence functions against a NOBYPASSRLS
role (``modulo_app`` production scenario) and assert the rows actually
land when RLS is set, and do NOT land when it is not.  The unit tests run
on SQLite, which has no RLS, so they cannot catch a missing
``set_rls_org`` call — only a real Postgres path can.  This is exactly
the regression the PR Reviewer's blocking finding covered.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.pipeline_engine.workspace_input_audit import (
    AUDIT_NODE_ID,
    WorkspaceInputAuditRecord,
    record_drift,
    record_resolved_inputs,
)
from modulo.db.crud.run_node_outputs import FINAL_ATTEMPT_KEY
from modulo.db.models.run_node_outputs import RunNodeOutput
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


def _make_record(dest: str, resolved_sha: str) -> WorkspaceInputAuditRecord:
    return WorkspaceInputAuditRecord(
        input_name=dest,
        connector_instance_id=None,
        host="github.com",
        url_redacted="https://github.com/org/repo.git",
        requested_ref_kind="branch",
        requested_ref_value="main",
        resolved_sha=resolved_sha,
        final_sha=None,
        drift_detected=False,
        dest=dest,
        status="resolved",
    )


async def _seed_run(
    db_engine: object,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    """Commit a minimal run row so run_node_outputs FK checks pass."""
    run_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, run_number, input_hash, langgraph_thread_id) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'running', 1, :ih, :tid)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": "a" * 64,
                "tid": f"{org_id}:{run_id}",
            },
        )
    return run_id


async def _read_audit_row(db_engine: object, run_id: uuid.UUID) -> RunNodeOutput | None:
    """Read the audit row via a superuser connection (bypasses RLS)."""
    async with db_engine.connect() as conn:
        return (
            await conn.execute(
                select(RunNodeOutput).where(
                    RunNodeOutput.run_id == run_id,
                    RunNodeOutput.node_id == AUDIT_NODE_ID,
                    RunNodeOutput.attempt_key == FINAL_ATTEMPT_KEY,
                )
            )
        ).scalar_one_or_none()


@pytest.mark.anyio
async def test_record_resolved_inputs_persists_when_rls_org_set(
    app_engine: object,
    db_engine: object,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    run_id = await _seed_run(db_engine, test_org, test_pipeline, test_snapshot)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(app_engine, expire_on_commit=False)
    records = [
        _make_record("src", "a" * 40),
        _make_record("docs", "b" * 40),
    ]
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        await record_resolved_inputs(
            session,
            run_id=run_id,
            organisation_id=test_org,
            node_id="node-1",
            attempt_key=FINAL_ATTEMPT_KEY,
            records=records,
            status="resolved",
        )

    row = await _read_audit_row(db_engine, run_id)
    assert row is not None, "audit row must persist when RLS org context is set"
    payload = row.outputs_json
    assert payload["status"] == "resolved"
    names = {r["input_name"] for r in payload["workspace_inputs"]}
    assert names == {"src", "docs"}


@pytest.mark.anyio
async def test_record_resolved_inputs_dropped_without_rls_org(
    app_engine: object,
    db_engine: object,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """Regression guard: missing set_rls_org must NOT silently persist a row.

    Under FORCE RLS the INSERT is rejected (42501) and the best-effort write
    swallows it.  Without this guard the bug (audit never persisted) is
    invisible on SQLite.  Here we assert the row is absent when the RLS
    context is never established.
    """
    run_id = await _seed_run(db_engine, test_org, test_pipeline, test_snapshot)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(app_engine, expire_on_commit=False)
    records = [_make_record("src", "a" * 40)]
    async with factory() as session, session.begin():
        # Deliberately NOT calling set_rls_org — reproduces the pre-fix bug.
        await record_resolved_inputs(
            session,
            run_id=run_id,
            organisation_id=test_org,
            node_id="node-1",
            attempt_key=FINAL_ATTEMPT_KEY,
            records=records,
            status="resolved",
        )

    row = await _read_audit_row(db_engine, run_id)
    assert row is None, "audit row must NOT persist when RLS org context is missing"


@pytest.mark.anyio
async def test_record_drift_persists_when_rls_org_set(
    app_engine: object,
    db_engine: object,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    run_id = await _seed_run(db_engine, test_org, test_pipeline, test_snapshot)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(app_engine, expire_on_commit=False)
    records = [_make_record("src", "a" * 40)]
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        await record_resolved_inputs(
            session,
            run_id=run_id,
            organisation_id=test_org,
            node_id="node-1",
            attempt_key=FINAL_ATTEMPT_KEY,
            records=records,
            status="resolved",
        )
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        await record_drift(
            session,
            run_id=run_id,
            organisation_id=test_org,
            node_id="node-1",
            attempt_key=FINAL_ATTEMPT_KEY,
            final_shas={"src": "c" * 40},
        )

    row = await _read_audit_row(db_engine, run_id)
    assert row is not None, "audit row must persist for record_drift under RLS"
    payload = row.outputs_json
    src_entry = next(r for r in payload["workspace_inputs"] if r["input_name"] == "src")
    assert src_entry["final_sha"] == "c" * 40
    assert src_entry["drift_detected"] is True
