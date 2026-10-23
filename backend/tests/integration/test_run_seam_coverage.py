"""FAR-2054: seam-scoped guardrail coverage integration (real Postgres)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.db.crud.run import create_run
from modulo.db.rls import set_rls_org
from tests.integration.conftest import EvalFixture, insert_eval

pytestmark = pytest.mark.integration


async def _create_test_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    graph: dict[str, Any],
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, (:graph)::json)",
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": f"FAR-2054 Test Pipeline {pipeline_id.hex[:8]}",
                "uid": str(user_id),
                "graph": json.dumps(graph),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, (:graph)::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id), "graph": json.dumps(graph)},
        )
    return pipeline_id, snapshot_id


async def _seed_guardrail(
    conn: Any,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID | None,
    interception_point: str,
    action: str = "block",
    account_id: uuid.UUID | None = None,
) -> uuid.UUID:
    guardrail_id = uuid.uuid4()
    config = {
        "action": action,
        "interception_point": interception_point,
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    await insert_eval(
        conn,
        EvalFixture(
            id=guardrail_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            node_id=node_id,
            name="no-secrets",
            eval_type="guardrail",
            account_id=account_id if account_id is not None else org_id,
            config=config,
        ),
    )
    return guardrail_id


async def _create_run_and_load_seam_coverage(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    payload: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Create a run via the real crud path and return its parsed seam_coverage."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="manual",
            input_payload=payload,
            account_id=user_id,
        )
        run_id = run.id

    # Read back via a fresh connection (no session, just the column value)
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT seam_coverage_json FROM runs WHERE id = :rid"),
            {"rid": str(run_id)},
        )
        raw = row.scalar_one_or_none()
        if raw is None:
            return None
        return raw


async def _read_run_status(db_engine: AsyncEngine, run_id: uuid.UUID) -> tuple[str, str | None]:
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT status, error_code FROM runs WHERE id = :rid"),
            {"rid": str(run_id)},
        )
        result = row.first()
        return (result[0], result[1])


class TestSeamCoverageNoGuardrailsBound:
    async def test_all_three_seams_not_applicable(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """No guardrails bound → ingestion + node_output + runtime_loop all n/a."""
        pipeline_id, snapshot_id = await _create_test_pipeline(
            db_engine,
            test_org,
            test_user,
            {"nodes": [{"id": "node_a"}], "edges": []},
        )

        coverage = await _create_run_and_load_seam_coverage(
            db_engine,
            test_org,
            test_user,
            pipeline_id,
            snapshot_id,
            {"body": "hello"},
        )

        assert coverage is not None
        assert [statement["seam"] for statement in coverage] == [
            "ingestion_edge",
            "node_output_edge",
            "runtime_loop",
        ]
        assert all(statement["state"] == "not_applicable" for statement in coverage)
        assert all(statement["reason"] for statement in coverage)


class TestSeamCoverageInputGuardrailPass:
    async def test_ingestion_edge_enforced(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """An input guardrail that evaluates → enforced/attested."""
        graph = {"nodes": [{"id": "node_a"}], "edges": []}
        pipeline_id, snapshot_id = await _create_test_pipeline(db_engine, test_org, test_user, graph)

        # Seed the input guardrail
        async with db_engine.connect() as conn, conn.begin():
            await _seed_guardrail(conn, test_org, pipeline_id, None, "input", account_id=test_user)

        coverage = await _create_run_and_load_seam_coverage(
            db_engine,
            test_org,
            test_user,
            pipeline_id,
            snapshot_id,
            {"body": "hello"},
        )

        assert coverage is not None
        by_seam = {statement["seam"]: statement for statement in coverage}
        ingestion = by_seam["ingestion_edge"]
        assert ingestion["state"] == "enforced"
        assert ingestion["provenance"] == "attested"
        assert ingestion["reason"].startswith("1 ingestion-edge guardrail(s) evaluated")
        # runtime_loop stays n/a
        assert by_seam["runtime_loop"]["state"] == "not_applicable"
        assert by_seam["runtime_loop"]["reason"] == "runtime does not expose a tool-event contract Modulo can verify"


class TestSeamCoverageInputGuardrailBlock:
    async def test_blocked_run_never_reaches_runtime(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """An ingestion guardrail block → all seams not_applicable.

        This matches the policy-gate block path in create_run (both surface as
        interception.blocked): the run never reaches the runtime.
        """
        graph = {"nodes": [{"id": "node_a"}], "edges": []}
        pipeline_id, snapshot_id = await _create_test_pipeline(db_engine, test_org, test_user, graph)

        async with db_engine.connect() as conn, conn.begin():
            await _seed_guardrail(conn, test_org, pipeline_id, None, "input", account_id=test_user)

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            run = await create_run(
                session,
                org_id=test_org,
                pipeline_id=pipeline_id,
                snapshot_id=snapshot_id,
                trigger_type="manual",
                input_payload={"body": "leak SECRET_ABC12345"},
                account_id=test_user,
            )
            run_id = run.id

        async with db_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT seam_coverage_json FROM runs WHERE id = :rid"),
                {"rid": str(run_id)},
            )
            raw = row.scalar_one_or_none()
            coverage = raw

        assert coverage is not None
        assert all(statement["state"] == "not_applicable" for statement in coverage)
        assert all(statement["reason"] for statement in coverage)
        # The blocked run should be terminal eval_failed
        status, error_code = await _read_run_status(db_engine, run_id)
        assert status == "eval_failed"
        assert error_code == "eval_blocked"


class TestSeamCoverageOutputGuardrailOnly:
    async def test_node_output_edge_enforced(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
    ) -> None:
        """An output-point guardrail bound → node_output_edge enforced/attested."""
        graph = {"nodes": [{"id": "node_a"}], "edges": []}
        _pipeline_id, _snapshot_id = await _create_test_pipeline(db_engine, test_org, test_user, graph)

        node_id = uuid.uuid4()
        graph_with_node = {"nodes": [{"id": "node_a"}, {"id": str(node_id)}], "edges": []}
        pipeline_id2, snapshot_id2 = await _create_test_pipeline(db_engine, test_org, test_user, graph_with_node)

        async with db_engine.connect() as conn, conn.begin():
            await _seed_guardrail(conn, test_org, pipeline_id2, node_id, "output", account_id=test_user)

        coverage = await _create_run_and_load_seam_coverage(
            db_engine,
            test_org,
            test_user,
            pipeline_id2,
            snapshot_id2,
            {"body": "hello"},
        )

        assert coverage is not None
        by_seam = {statement["seam"]: statement for statement in coverage}
        node_output = by_seam["node_output_edge"]
        assert node_output["state"] == "enforced"
        assert node_output["provenance"] == "attested"
        assert node_output["reason"].startswith("1 node-output guardrail(s) bound")
        # ingestion has no input guardrails → n/a
        assert by_seam["ingestion_edge"]["state"] == "not_applicable"
        # runtime_loop always n/a
        assert by_seam["runtime_loop"]["state"] == "not_applicable"
