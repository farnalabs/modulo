"""Integration tests for FAR-213 run-termination compensation (real Postgres).

Exercises the REAL ``create_run`` guardrail-blocked seam, the compensation
orchestrator, the ``blocked_partial_summary`` column, the dependent-trigger
suppression guard, and the webhook ack-after-validate semantics against
testcontainers Postgres:

  1. a block-action guardrail → ``create_run`` writes the terminal
     ``eval_failed``/``eval_blocked`` run AND the ``blocked_partial_summary``
     with the blocking eval name;
  2. ``compensate_blocked_run`` with a stub connector hub compensates a
     connector write node (close-PR) and writes per-node publish status +
     compensation outcomes + summary-only audit events;
  3. the dependent-trigger suppression predicate matches a real guardrail-
     blocked run and the agent_signal fire is suppressed (no child run, no
     audit-suppression failure);
  4. webhook ack-after-validate: a guardrail-blocked run (run-creation-seam
     block) is acked 422, never ``accepted``.

Each test uses its OWN pipeline (fresh UUIDs) so bound guardrails never leak
into the shared conftest pipeline consumed by other integration tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.connectors.base import CompensationOutcome, CompensationResult
from modulo.core.guardrails.compensation import compensate_blocked_run
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.trigger_engine import is_guardrail_blocked_run
from modulo.db.crud.run import create_run
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


def _hmac_sign(body: bytes, secret: str, ts: int) -> str:
    payload = f"{ts}.".encode() + body
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _valid_timestamp() -> str:
    return str(int(time.time()))


class _StubConnector:
    """Stub connector exercising the compensate contract against real DB runs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def compensate(self, operation, *, context, error):
        self.calls.append((operation.resource, operation))
        if operation.resource == "pr":
            return CompensationResult(outcome=CompensationOutcome.COMPENSATED, detail="closed", resource_id="42")
        return CompensationResult(outcome=CompensationOutcome.NOT_SUPPORTED, detail="nope")


class _FakeHub:
    def __init__(self, connector: Any) -> None:
        self._connector = connector

    def get(self, _connector_id: Any) -> Any:
        return self._connector

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        # The executor's compensation window tears the hub down after the
        # summary write; a no-op teardown keeps the stub faithful to the real
        # ConnectorHub's async-context-manager contract.
        return None


@pytest_asyncio.fixture
async def comp_rig(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> dict[str, uuid.UUID | str]:
    """A dedicated pipeline + snapshot + guardrail row for compensation tests.

    The snapshot graph carries a connector write node bound to a stub connector
    id so ``compensate_blocked_run`` can resolve it to a compensatable node.
    """
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    connector_id = uuid.uuid4()
    graph = {
        "nodes": [
            {
                "id": "node_create_pr",
                "node_type": "connector",
                "connector_binding": {
                    "instance_id": str(connector_id),
                    "resource": "pr",
                    "operation": "write",
                    "data": {"repo": "acme/thing", "head": "b", "base": "main"},
                },
            }
        ]
    }
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
                "oid": str(test_org),
                "name": f"FAR-213 Compensation Pipeline {pipeline_id.hex[:8]}",
                "uid": str(test_user),
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
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(test_org), "graph": json.dumps(graph)},
        )
        await conn.execute(
            text(
                "INSERT INTO eval_definitions (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, failure_behaviour, account_id) "
                "VALUES (:id, :oid, :pid, NULL, 'no-secrets', 'guardrail', (:cfg)::json, 'warn', :aid)",
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(test_org),
                "pid": str(pipeline_id),
                "cfg": json.dumps(
                    {
                        "action": "block",
                        "interception_point": "input",
                        "type": "regex",
                        "field": "body",
                        "pattern": r"SECRET_[A-Z0-9]{8}",
                    }
                ),
                "aid": str(test_user),
            },
        )
    return {
        "org_id": test_org,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "connector_id": connector_id,
    }


async def _create_blocked_run(
    db_engine: AsyncEngine,
    rig: dict[str, uuid.UUID | str],
    *,
    payload: dict[str, Any],
    account_id: uuid.UUID,
) -> uuid.UUID:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, rig["org_id"])
        run = await create_run(
            session,
            org_id=rig["org_id"],
            pipeline_id=rig["pipeline_id"],
            snapshot_id=rig["snapshot_id"],
            trigger_type="manual",
            input_payload=payload,
            account_id=account_id,
        )
        return uuid.UUID(str(run.id))


async def _load_summary(db_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, Any] | None:
    async with db_engine.connect() as conn:
        row = await conn.execute(text("SELECT blocked_partial_summary FROM runs WHERE id = :rid"), {"rid": str(run_id)})
        return row.scalar_one_or_none()


async def _load_run_status(db_engine: AsyncEngine, run_id: uuid.UUID) -> tuple[str, str | None]:
    async with db_engine.connect() as conn:
        row = await conn.execute(text("SELECT status, error_code FROM runs WHERE id = :rid"), {"rid": str(run_id)})
        result = row.first()
        return (result[0], result[1])


class TestBlockedPartialSummaryWrittenAtCreate:
    async def test_blocked_run_writes_summary(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        run_id = await _create_blocked_run(
            db_engine,
            comp_rig,
            payload={"body": "leak SECRET_ABC12345"},
            account_id=test_user,
        )
        status, error_code = await _load_run_status(db_engine, run_id)
        assert status == "eval_failed"
        assert error_code == "eval_blocked"
        summary = await _load_summary(db_engine, run_id)
        assert summary is not None
        assert summary["blocked"] is True
        assert summary["blocking_eval_name"] == "no-secrets"
        assert not summary["executed_nodes"]
        assert not summary["nodes"]


class TestCompensateBlockedRunAgainstRealDB:
    async def test_compensates_connector_node_and_writes_summary(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        run_id = await _create_blocked_run(
            db_engine,
            comp_rig,
            payload={"body": "leak SECRET_ABC12345"},
            account_id=test_user,
        )

        # Simulate a mid-run terminalization: the run executed node_create_pr
        # (which created a PR) and then got guardrail-blocked. Its outputs are
        # set on the run row; compensation walks them.
        connector = _StubConnector()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            await session.execute(
                text("UPDATE runs SET outputs_json = (:out)::json WHERE id = :rid"),
                {
                    "rid": str(run_id),
                    "out": json.dumps({"node_create_pr": {"output": {"number": 42}}}),
                },
            )
            # Reload the run row and drive compensation through the real path.
            from modulo.db.crud.run import get_run

            run = await get_run(session, run_id)
            assert run is not None
            summary = await compensate_blocked_run(
                session,
                run,
                guardrail_block="no-secrets blocked",
                connector_hub=_FakeHub(connector),
                blocking_eval_name="no-secrets",
            )
            await session.flush()

        # The connector's close-PR compensation fired with the run's output.
        assert connector.calls
        assert connector.calls[0][0] == "pr"
        assert connector.calls[0][1].output == {"number": 42}

        persisted = await _load_summary(db_engine, run_id)
        assert persisted is not None
        assert persisted["blocking_eval_name"] == "no-secrets"
        assert persisted["executed_nodes"] == ["node_create_pr"]
        assert persisted["nodes"][0]["publish_status"] == "compensated"
        assert persisted["nodes"][0]["compensation"]["outcome"] == "compensated"
        assert persisted["nodes"][0]["output_ref"] == {"run_id": str(run_id), "node_id": "node_create_pr"}
        assert summary["nodes"][0]["publish_status"] == "compensated"

        # Summary-only audit events were appended (attempted + written).
        async with db_engine.connect() as conn:
            audit_types = (
                (
                    await conn.execute(
                        text(
                            "SELECT event_type FROM audit_events WHERE resource_id = :rid AND "
                            "event_type IN ('guardrail.compensation_attempted', 'guardrail.blocked_partial_written')"
                        ),
                        {"rid": str(run_id)},
                    )
                )
                .scalars()
                .all()
            )
        assert "guardrail.compensation_attempted" in audit_types
        assert "guardrail.blocked_partial_written" in audit_types

    async def test_no_hub_writes_summary_only(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """The create_run seam (no hub) writes only the summary + audit — the
        ingestion-edge contract where no nodes have executed."""
        run_id = await _create_blocked_run(
            db_engine,
            comp_rig,
            payload={"body": "leak SECRET_ABC12345"},
            account_id=test_user,
        )
        summary = await _load_summary(db_engine, run_id)
        assert summary is not None
        assert summary["blocked"] is True
        assert not summary["executed_nodes"]


class TestExecutorMidRunCompensationWiring:
    """FAR-291 — the executor's mid-run guardrail-block terminalization seam.

    The existing tests call ``compensate_blocked_run`` directly, which cannot
    catch a MISSING wiring line. These drive the executor's shared finalization
    tail (``_finalize_run_after_stream`` — used by BOTH ``execute()`` and
    ``resume()``) through a real ``eval_failed``/``eval_blocked`` outcome with
    executed node outputs, and assert the compensating callback fired. If the
    wiring line is removed, the callback is never invoked and the test fails.
    """

    async def _seed_midrun_run(
        self,
        db_engine: AsyncEngine,
        comp_rig: dict[str, uuid.UUID | str],
        org_id: uuid.UUID,
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        run_number = int(run_id.int % 10**9) + 1
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                    "trigger_type, input_hash, input_payload, langgraph_thread_id, "
                    "run_number, status, claim_token) "
                    "VALUES (:id, :oid, :pid, :sid, 'manual', :ih, '{}'::json, :thread, "
                    ":rn, 'running', :tok)"
                ),
                {
                    "id": str(run_id),
                    "oid": str(org_id),
                    "pid": str(comp_rig["pipeline_id"]),
                    "sid": str(comp_rig["snapshot_id"]),
                    "ih": uuid.uuid4().hex,
                    "thread": f"{org_id}:{run_id}",
                    "rn": run_number,
                    "tok": "claim-1",
                },
            )
        return run_id

    async def test_mid_run_block_compensates_executed_connector_node(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """A real mid-run guardrail block through the executor fires compensation.

        The run executed ``node_create_pr`` (pushed a PR) and then a later
        node's output was guardrail-blocked (terminal ``eval_failed`` /
        ``eval_blocked``). The executor's finalization tail must compensate the
        PR side effect via a compensating connector callback.
        """
        run_id = await self._seed_midrun_run(db_engine, comp_rig, test_org)

        connector = _StubConnector()
        fake_hub = _FakeHub(connector)

        executor = PipelineExecutor(db_engine)
        # The compensation window uses a FRESH hub (the run's own hub was torn
        # down before finalization). Substitute the real hub factory with a stub
        # hub so the test observes the compensating callback.
        with patch.object(executor, "_init_connector_hub", new=AsyncMock(return_value=fake_hub)):
            await executor._finalize_run_after_stream(
                run_id=run_id,
                org_id=test_org,
                pipeline_id=comp_rig["pipeline_id"],
                node_type_map={"node_create_pr": "connector"},
                final_status="eval_failed",
                error_code="eval_blocked",
                error_detail="no-secrets blocked",
                node_token_usage=None,
                completed_node_outputs={"node_create_pr": {"output": {"number": 42}}},
                node_ids={"node_create_pr"},
            )

        # The compensating callback fired through the real executor seam.
        assert connector.calls
        assert connector.calls[0][0] == "pr"
        assert connector.calls[0][1].output == {"number": 42}

        # The blocked_partial summary was written with the executed node marked
        # compensated.
        persisted = await _load_summary(db_engine, run_id)
        assert persisted is not None
        assert persisted["blocked"] is True
        assert persisted["executed_nodes"] == ["node_create_pr"]
        assert persisted["nodes"][0]["publish_status"] == "compensated"
        assert persisted["nodes"][0]["output_ref"] == {"run_id": str(run_id), "node_id": "node_create_pr"}

        # Summary-only audit events were appended.
        async with db_engine.connect() as conn:
            audit_types = (
                (
                    await conn.execute(
                        text(
                            "SELECT event_type FROM audit_events WHERE resource_id = :rid AND "
                            "event_type IN ('guardrail.compensation_attempted', 'guardrail.blocked_partial_written')"
                        ),
                        {"rid": str(run_id)},
                    )
                )
                .scalars()
                .all()
            )
        assert "guardrail.compensation_attempted" in audit_types
        assert "guardrail.blocked_partial_written" in audit_types

    async def test_mid_run_non_blocked_run_does_not_compensate(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """A non-blocked terminalization must NOT invoke compensation."""
        run_id = await self._seed_midrun_run(db_engine, comp_rig, test_org)

        connector = _StubConnector()
        fake_hub = _FakeHub(connector)

        executor = PipelineExecutor(db_engine)
        with patch.object(executor, "_init_connector_hub", new=AsyncMock(return_value=fake_hub)):
            await executor._finalize_run_after_stream(
                run_id=run_id,
                org_id=test_org,
                pipeline_id=comp_rig["pipeline_id"],
                node_type_map={"node_create_pr": "connector"},
                final_status="complete",
                error_code=None,
                error_detail=None,
                node_token_usage=None,
                completed_node_outputs={"node_create_pr": {"output": {"number": 42}}},
                node_ids={"node_create_pr"},
            )

        # No compensation fired for a complete run.
        assert not connector.calls


class TestDependentTriggerSuppression:
    async def test_is_guardrail_blocked_run_matches_real_block(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        run_id = await _create_blocked_run(
            db_engine,
            comp_rig,
            payload={"body": "leak SECRET_ABC12345"},
            account_id=test_user,
        )
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, comp_rig["org_id"])
            assert await is_guardrail_blocked_run(session, run_id) is True

    async def test_clean_run_not_guardrail_blocked(
        self,
        db_engine: AsyncEngine,
        test_user: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, comp_rig["org_id"])
            clean = await create_run(
                session,
                org_id=comp_rig["org_id"],
                pipeline_id=comp_rig["pipeline_id"],
                snapshot_id=comp_rig["snapshot_id"],
                trigger_type="manual",
                input_payload={"body": "clean text"},
                account_id=test_user,
            )
            assert await is_guardrail_blocked_run(session, uuid.UUID(str(clean.id))) is False

    async def test_fire_agent_signal_suppressed_for_blocked_source(
        self,
        db_engine: AsyncEngine,
        test_org: uuid.UUID,
        test_user: uuid.UUID,
        test_pipeline: uuid.UUID,
        test_snapshot: uuid.UUID,
        comp_rig: dict[str, uuid.UUID | str],
    ) -> None:
        """A guardrail-blocked source run must NOT fire an agent_signal child."""
        source_run_id = await _create_blocked_run(
            db_engine,
            comp_rig,
            payload={"body": "leak SECRET_ABC12345"},
            account_id=test_user,
        )

        # An agent_signal trigger watching comp_rig's pipeline + node.
        child_trigger_id = uuid.uuid4()
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                    "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                    "VALUES (:id, :oid, :pid, 'agent_signal', true, 5, (:cfg)::json, :uid)"
                ),
                {
                    "id": str(child_trigger_id),
                    "oid": str(test_org),
                    "pid": str(test_pipeline),
                    "cfg": json.dumps(
                        {
                            "source_pipeline_id": str(comp_rig["pipeline_id"]),
                            "source_node_id": "node_create_pr",
                            "snapshot_id": str(test_snapshot),
                        }
                    ),
                    "uid": str(test_user),
                },
            )

        from modulo.core.trigger_engine.agent_signal import fire_agent_signal

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, test_org)
            results = await fire_agent_signal(
                session,
                org_id=test_org,
                source_run_id=source_run_id,
                source_pipeline_id=comp_rig["pipeline_id"],
                completed_node_id="node_create_pr",
                node_output={"number": 42},
            )

        # The single matching trigger was suppressed, not fired.
        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "source_run_guardrail_blocked"
        assert results[0]["trigger_id"] == str(child_trigger_id)

        # No child run was created for the agent_signal trigger.
        async with db_engine.connect() as conn:
            child_count = (
                await conn.execute(
                    text("SELECT count(*) FROM runs WHERE trigger_id = :tid"),
                    {"tid": str(child_trigger_id)},
                )
            ).scalar_one()
        assert child_count == 0

        # The suppression was audited (summary-only).
        async with db_engine.connect() as conn:
            suppressed = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE resource_id = :rid AND "
                        "event_type = 'guardrail.dependent_suppressed'"
                    ),
                    {"rid": str(source_run_id)},
                )
            ).scalar_one()
        assert suppressed >= 1
