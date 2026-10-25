"""Integration: the risk/assessment projection SQL over real Postgres (FAR-941).

The unit suite runs these helpers against a mocked execute, so it can pin the
route contract but never proves the SQL itself. This module round-trips the
new ``split_part`` node-subject extraction, the prefix ``LIKE`` and the
pipeline ``IN (subquery)`` against the production dialect, over seeded rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.routes.evidence import (
    _count_risk_assessment_by_action,
    _count_risk_assessment_rows,
    _load_risk_assessment_rows,
)
from modulo.db.models.account import Account
from modulo.db.models.evidence import Evidence
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

pytestmark = pytest.mark.integration

_NOW = datetime.now(UTC)


async def _seed(session: AsyncSession) -> dict[str, uuid.UUID]:
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    other_pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()

    session.add(Organisation(id=org_id, name=f"Risk {org_id.hex[:8]}", slug=f"risk-{org_id.hex[:8]}"))
    session.add(Account(id=account_id, email=f"risk-{org_id.hex[:8]}@example.com", display_name="Risk"))
    await session.flush()

    for pid, name in ((pipeline_id, "risk pipeline"), (other_pipeline_id, "other pipeline")):
        session.add(Pipeline(id=pid, organisation_id=org_id, name=name, account_id=account_id))
    await session.flush()

    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_version=1,
            graph_json={"nodes": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            config_json={},
            run_context_defaults={},
        )
    )
    await session.flush()

    for rid, pid, number in ((run_id, pipeline_id, 1), (other_run_id, other_pipeline_id, 2)):
        session.add(
            Run(
                id=rid,
                organisation_id=org_id,
                pipeline_id=pid,
                snapshot_id=snapshot_id,
                trigger_type="manual",
                status="complete",
                run_number=number,
                input_hash="a" * 64,
                langgraph_thread_id=f"thread-{rid.hex}",
            )
        )
    await session.flush()

    node_id = uuid.uuid4()
    now = datetime.now(UTC)
    rows = [
        ("risk.api_call", "run", str(run_id)),
        ("risk.node", "node_execution", f"{run_id}:{node_id}"),
        ("assessment.score", "run", str(run_id)),
        ("other.audit", "run", str(run_id)),
        ("risk.foreign", "run", str(other_run_id)),
    ]
    for key, subject_type, subject_id in rows:
        session.add(
            Evidence(
                id=uuid.uuid4(),
                organisation_id=org_id,
                key=key,
                subject_type=subject_type,
                subject_id=subject_id,
                value={"ok": True},
                observed_at=now,
                producer_type="run",
                producer_id=None,
            )
        )

    # One stale row outside the window — every query must exclude it.
    session.add(
        Evidence(
            id=uuid.uuid4(),
            organisation_id=org_id,
            key="risk.stale",
            subject_type="run",
            subject_id=str(run_id),
            value={"ok": True},
            observed_at=now - timedelta(days=30),
            producer_type="run",
            producer_id=None,
        )
    )
    await session.flush()

    return {
        "org_id": org_id,
        "account_id": account_id,
        "pipeline_id": pipeline_id,
        "other_pipeline_id": other_pipeline_id,
        "run_id": run_id,
        "other_run_id": other_run_id,
        "node_id": node_id,
    }


async def test_key_prefix_like_filters_on_postgres(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)

    rows, degraded = await _load_risk_assessment_rows(db_session, seeded["org_id"], key_prefix="risk.", days=7)

    assert degraded is False
    assert {row.key for row in rows} == {"risk.api_call", "risk.node", "risk.foreign"}
    total = await _count_risk_assessment_rows(db_session, seeded["org_id"], key_prefix="risk.", days=7)
    assert total == len(rows)


async def test_node_subject_split_part_and_pipeline_subquery(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)

    rows, degraded = await _load_risk_assessment_rows(
        db_session, seeded["org_id"], pipeline_id=seeded["pipeline_id"], days=7
    )

    assert degraded is False
    # split_part extracts the run id from "{run_id}:{node_id}", so the node row
    # (and only the node row) of the filtered pipeline is included.
    assert {row.key for row in rows} == {"risk.api_call", "risk.node", "assessment.score", "other.audit"}
    total = await _count_risk_assessment_rows(db_session, seeded["org_id"], pipeline_id=seeded["pipeline_id"], days=7)
    assert total == len(rows)


async def test_stale_rows_are_excluded_by_the_window(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)

    rows, _ = await _load_risk_assessment_rows(db_session, seeded["org_id"], days=7)

    assert "risk.stale" not in {row.key for row in rows}


async def test_action_counts_round_trip_on_postgres(db_session: AsyncSession) -> None:
    seeded = await _seed(db_session)

    counts, degraded = await _count_risk_assessment_by_action(db_session, seeded["org_id"], days=7)

    # No gate decisions seeded: the aggregate executes on real Postgres and
    # returns zeros rather than erroring.
    assert degraded is False
    assert counts == {"block": 0, "escalate": 0, "warn": 0, "continue": 0}
