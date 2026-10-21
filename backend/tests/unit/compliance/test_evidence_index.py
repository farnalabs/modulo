"""Tests for the per-instance compliance evidence index (FAR-2036).

These run against a real (hermetic, in-memory SQLite) async session with a
seeded organisation, because the index's job is to read real rows - a mocked
session would only prove the mock. The behaviours pinned here are the ones an
auditor depends on:

* every mapped artefact type appears, with its mapping rows;
* the counts are the organisation's own (tenant isolation);
* human-only approvals are counted separately from machine-decidable gates;
* a missing mapped artefact is surfaced as ``absent`` (and raises in strict
  mode) rather than being omitted;
* the pack version and content hash are recorded in the index.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.compliance import evidence_index as evidence_index_module
from modulo.compliance.artifacts import (
    ARTIFACT_AUDIT_CHAIN,
    ARTIFACT_AUTONOMY_CHANGE,
    ARTIFACT_BUILD_IDENTITY,
    ARTIFACT_EVAL_RESULT,
    ARTIFACT_HITL_DECISION,
    ARTIFACT_RELEASE_SBOM,
    ARTIFACT_RUN_SNAPSHOT,
    ARTIFACT_TYPES,
    INDEXED_ARTIFACT_TYPES,
    PROVENANCE_CUSTOMER_SUPPLIED,
)
from modulo.compliance.evidence_index import (
    AUTONOMY_DEMOTION_EVENT,
    AUTONOMY_PROMOTION_EVENT,
    STATUS_ABSENT,
    STATUS_CUSTOMER_SUPPLIED,
    STATUS_PRESENT,
    EvidenceIndex,
    EvidenceIndexIncompleteError,
    build_evidence_index,
)
from modulo.compliance.pack import load_mapping_pack
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_NEEDED_TABLES = {
    "accounts",
    "organisations",
    "pipelines",
    "pipeline_snapshots",
    "runs",
    "evals",
    "eval_results",
    "hitl_claims",
    "audit_events",
    "audit_chain_heads",
}

_BASE_TIME = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)


def _tables() -> list:
    """Only the tables this module touches.

    ``create_all`` is scoped because unrelated models use Postgres-only column
    types that SQLite cannot render (the house pattern in ``test_seed.py``).
    """
    return [table for table in Base.metadata.sorted_tables if table.name in _NEEDED_TABLES]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_tables()))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _make_org(session: AsyncSession, slug: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Organisation(id=org_id, name=f"Org {slug}", slug=slug))
    await session.flush()
    return org_id


async def _make_account(session: AsyncSession, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    session.add(Account(id=account_id, email=email, display_name="Reviewer"))
    await session.flush()
    return account_id


async def _make_pipeline(session: AsyncSession, org_id: uuid.UUID, name: str, account_id: uuid.UUID) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    session.add(Pipeline(id=pipeline_id, organisation_id=org_id, name=name, account_id=account_id))
    await session.flush()
    return pipeline_id


async def _make_snapshot(session: AsyncSession, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_version=1,
            created_at=_BASE_TIME,
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
    return snapshot_id


async def _make_run(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    run_number: int,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    session.add(
        Run(
            id=run_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            trigger_type="manual",
            status="complete",
            run_number=run_number,
            input_hash="a" * 64,
            langgraph_thread_id=f"thread-{uuid.uuid4().hex}",
            created_at=_BASE_TIME,
        )
    )
    await session.flush()
    return run_id


async def _make_hitl(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    review_id: str,
    decision: str,
    human_only: bool,
    decided_at: datetime,
) -> uuid.UUID:
    claim_id = uuid.uuid4()
    session.add(
        HitlClaim(
            id=claim_id,
            organisation_id=org_id,
            run_id=run_id,
            review_id=review_id,
            pipeline_id=pipeline_id,
            expires_at=_BASE_TIME + timedelta(days=1),
            decision=decision,
            decision_at=decided_at,
            decided_by=account_id,
            gate_config_json={"human_only": human_only},
        )
    )
    await session.flush()
    return claim_id


async def _make_eval(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
) -> uuid.UUID:
    eval_id = uuid.uuid4()
    session.add(
        Eval(
            id=eval_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            account_id=account_id,
            eval_type="llm_judge",
            config_json={},
        )
    )
    await session.flush()
    return eval_id


async def _make_eval_result(
    session: AsyncSession,
    org_id: uuid.UUID,
    eval_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    passed: bool,
    evaluated_at: datetime,
) -> uuid.UUID:
    result_id = uuid.uuid4()
    session.add(
        EvalResult(
            id=result_id,
            organisation_id=org_id,
            eval_id=eval_id,
            run_id=run_id,
            passed=passed,
            observed=True,
            evaluated_at=evaluated_at,
        )
    )
    await session.flush()
    return result_id


async def _make_audit_event(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    event_type: str,
    created_at: datetime,
) -> AuditEvent:
    event = AuditEvent(
        id=uuid.uuid4(),
        organisation_id=org_id,
        event_type=event_type,
        payload_json={},
        created_at=created_at,
    )
    session.add(event)
    await session.flush()
    return event


async def _seed_full_org(session: AsyncSession, slug: str = "acme") -> dict[str, uuid.UUID]:
    org_id = await _make_org(session, slug)
    account_id = await _make_account(session, f"reviewer-{slug}@example.com")
    pipeline_id = await _make_pipeline(session, org_id, f"{slug} pipeline", account_id)
    snapshot_id = await _make_snapshot(session, org_id, pipeline_id)
    run_id = await _make_run(session, org_id, pipeline_id, snapshot_id, run_number=1)

    await _make_hitl(
        session,
        org_id=org_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        account_id=account_id,
        review_id="review-human-approve",
        decision="approved",
        human_only=True,
        decided_at=_BASE_TIME + timedelta(hours=1),
    )
    await _make_hitl(
        session,
        org_id=org_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        account_id=account_id,
        review_id="review-human-approve-2",
        decision="approved",
        human_only=True,
        decided_at=_BASE_TIME + timedelta(hours=2),
    )
    await _make_hitl(
        session,
        org_id=org_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        account_id=account_id,
        review_id="review-human-reject",
        decision="rejected",
        human_only=True,
        decided_at=_BASE_TIME + timedelta(hours=3),
    )
    await _make_hitl(
        session,
        org_id=org_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        account_id=account_id,
        review_id="review-machine-approve",
        decision="approved",
        human_only=False,
        decided_at=_BASE_TIME + timedelta(hours=4),
    )

    eval_id = await _make_eval(session, org_id, pipeline_id, account_id)
    await _make_eval_result(session, org_id, eval_id, run_id, passed=True, evaluated_at=_BASE_TIME + timedelta(hours=5))
    await _make_eval_result(session, org_id, eval_id, run_id, passed=True, evaluated_at=_BASE_TIME + timedelta(hours=6))
    await _make_eval_result(
        session, org_id, eval_id, run_id, passed=False, evaluated_at=_BASE_TIME + timedelta(hours=7)
    )

    first_event = await _make_audit_event(session, org_id, event_type="pipeline_created", created_at=_BASE_TIME)
    await _make_audit_event(
        session,
        org_id,
        event_type=AUTONOMY_PROMOTION_EVENT,
        created_at=_BASE_TIME + timedelta(hours=8),
    )
    last_event = await _make_audit_event(
        session,
        org_id,
        event_type=AUTONOMY_DEMOTION_EVENT,
        created_at=_BASE_TIME + timedelta(hours=9),
    )
    session.add(
        AuditChainHead(
            id=uuid.uuid4(),
            organisation_id=org_id,
            last_event_hash="f" * 64,
            last_event_id=last_event.id,
            event_count=3,
        )
    )
    await session.flush()

    return {
        "org_id": org_id,
        "account_id": account_id,
        "pipeline_id": pipeline_id,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "first_event_id": first_event.id,
    }


def _by_type(index: EvidenceIndex) -> dict:
    return {entry.artifact_type: entry for entry in index.artifacts}


@pytest.fixture(autouse=True)
def _stable_build_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin build identity so the index does not depend on the test install."""
    monkeypatch.setattr(evidence_index_module, "get_version", lambda: "9.9.9")
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setenv("BUILD_TIMESTAMP", "2026-01-15T09:00:00Z")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSeededOrganisation:
    async def test_index_accounts_for_every_mapped_artefact_type(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])

        assert tuple(entry.artifact_type for entry in index.artifacts) == INDEXED_ARTIFACT_TYPES

    async def test_every_present_artefact_is_keyed_to_mapping_rows(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        pack = load_mapping_pack()

        for entry in index.artifacts:
            assert entry.mapping_row_ids
            assert entry.mapping_row_ids == pack.mapping_row_ids_for_artifact(entry.artifact_type)

    async def test_seeded_org_index_is_complete(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])

        assert index.complete
        assert not index.missing_artifact_types

    async def test_index_records_the_pack_version_and_hash(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()
        pack = load_mapping_pack()

        index = await build_evidence_index(session, seeded["org_id"])

        assert index.pack_version == pack.pack_version
        assert index.pack_content_hash == pack.content_hash()
        assert index.framework_ids == pack.framework_ids()

    async def test_audit_chain_entry_reports_the_chain_head(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_AUDIT_CHAIN]

        assert entry.status == STATUS_PRESENT
        assert entry.record_count == 3
        assert entry.detail["chain_head_hash"] == "f" * 64
        assert "audit-chain-head:" + "f" * 64 in entry.identifiers

    async def test_run_snapshot_entry_counts_the_frozen_snapshots(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_RUN_SNAPSHOT]

        assert entry.record_count == 1
        assert entry.identifiers == (f"pipeline-snapshot:{seeded['snapshot_id']}@v1",)

    async def test_hitl_entry_counts_human_only_approvals_separately(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        assert entry.record_count == 4
        assert entry.detail["human_only_decisions"] == 3
        assert entry.detail["human_only_approvals"] == 2
        assert entry.detail["decisions_by_outcome"] == {"approved": 3, "rejected": 1}
        assert entry.detail["human_only_scan_truncated"] is False

    async def test_eval_entry_reports_pass_and_fail_counts(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_EVAL_RESULT]

        assert entry.record_count == 3
        assert entry.detail["passed"] == 2
        assert entry.detail["failed"] == 1

    async def test_autonomy_entry_counts_promotions_and_demotions(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_AUTONOMY_CHANGE]

        assert entry.record_count == 2
        assert entry.detail["counts_by_event_type"] == {
            AUTONOMY_PROMOTION_EVENT: 1,
            AUTONOMY_DEMOTION_EVENT: 1,
        }

    async def test_build_identity_entry_reports_the_release(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_BUILD_IDENTITY]

        assert entry.status == STATUS_PRESENT
        assert "product-version:9.9.9" in entry.identifiers
        assert "git-sha:abc123" in entry.identifiers

    async def test_release_sbom_is_customer_supplied_not_observed(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_RELEASE_SBOM]

        assert entry.status == STATUS_CUSTOMER_SUPPLIED
        assert entry.provenance == PROVENANCE_CUSTOMER_SUPPLIED
        assert entry.detail["supplied_by"] == "customer/operator"


class TestMissingArtefactsAreLoud:
    async def test_empty_org_surfaces_every_instance_artefact_as_absent(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evidence_index_module, "get_version", lambda: "0.0.0-dev")
        org_id = await _make_org(session, "empty")
        await session.commit()

        index = await build_evidence_index(session, org_id)
        entries = _by_type(index)

        assert not index.complete
        assert ARTIFACT_AUDIT_CHAIN in index.missing_artifact_types
        assert ARTIFACT_RUN_SNAPSHOT in index.missing_artifact_types
        assert ARTIFACT_HITL_DECISION in index.missing_artifact_types
        assert ARTIFACT_EVAL_RESULT in index.missing_artifact_types
        assert ARTIFACT_AUTONOMY_CHANGE in index.missing_artifact_types
        assert ARTIFACT_BUILD_IDENTITY in index.missing_artifact_types
        # The SBOM is never "missing" - it is the operator's to supply.
        assert ARTIFACT_RELEASE_SBOM not in index.missing_artifact_types
        for artifact_type in index.missing_artifact_types:
            assert entries[artifact_type].status == STATUS_ABSENT

    async def test_every_absent_entry_states_a_reason(self, session: AsyncSession) -> None:
        org_id = await _make_org(session, "empty-reasons")
        await session.commit()

        index = await build_evidence_index(session, org_id)

        for entry in index.artifacts:
            if entry.status != STATUS_ABSENT:
                continue
            assert entry.absence_reason
            assert entry.absence_reason.strip()

    async def test_strict_mode_raises_and_names_the_missing_types(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evidence_index_module, "get_version", lambda: "0.0.0-dev")
        org_id = await _make_org(session, "empty-strict")
        await session.commit()

        with pytest.raises(EvidenceIndexIncompleteError) as excinfo:
            await build_evidence_index(session, org_id, strict=True)

        assert ARTIFACT_AUDIT_CHAIN in excinfo.value.missing_artifact_types
        assert ARTIFACT_HITL_DECISION in excinfo.value.missing_artifact_types


class TestTenantIsolation:
    async def test_another_orgs_artefacts_are_not_counted(self, session: AsyncSession) -> None:
        mine = await _seed_full_org(session, slug="mine")
        other = await _seed_full_org(session, slug="other")
        await session.commit()

        my_index = await build_evidence_index(session, mine["org_id"])
        entries = _by_type(my_index)

        assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 3
        assert entries[ARTIFACT_HITL_DECISION].record_count == 4
        assert entries[ARTIFACT_EVAL_RESULT].record_count == 3
        assert entries[ARTIFACT_RUN_SNAPSHOT].record_count == 1
        assert mine["snapshot_id"] != other["snapshot_id"]

    async def test_org_with_no_rows_sees_none_of_another_orgs_data(self, session: AsyncSession) -> None:
        await _seed_full_org(session, slug="populated")
        empty_org = await _make_org(session, "empty-tenant")
        await session.commit()

        index = await build_evidence_index(session, empty_org)

        assert not index.complete
        assert index.organisation_id == str(empty_org)
        # Only artefacts that are instance-wide or operator-supplied may be
        # non-absent for an org with no records of its own.
        org_scoped_presence = {
            entry.artifact_type
            for entry in index.artifacts
            if entry.status != STATUS_ABSENT
            and entry.artifact_type not in {ARTIFACT_RELEASE_SBOM, ARTIFACT_BUILD_IDENTITY}
        }
        assert not org_scoped_presence


class TestArtefactVocabulary:
    def test_every_indexed_type_has_a_description(self) -> None:
        for artifact_type in INDEXED_ARTIFACT_TYPES:
            assert ARTIFACT_TYPES[artifact_type].description
