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
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event
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
    PROVENANCE_OBSERVED,
)
from modulo.compliance.evidence_index import (
    AUTONOMY_DEMOTION_EVENT,
    AUTONOMY_PROMOTION_EVENT,
    STATUS_ABSENT,
    STATUS_CUSTOMER_SUPPLIED,
    STATUS_PRESENT,
    ArtifactEntry,
    EvidenceIndex,
    EvidenceIndexIncompleteError,
    build_evidence_index,
)
from modulo.compliance.pack import load_mapping_pack
from modulo.db.crud.hitl_review_config import human_only_effective, make_review_id
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


async def _make_snapshot(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    graph_json: dict | None = None,
    snapshot_version: int = 1,
) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_version=snapshot_version,
            created_at=_BASE_TIME,
            graph_json={"nodes": []} if graph_json is None else graph_json,
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


#: Sentinel distinguishing "no gate config at all" (a NULL ``gate_config_json``,
#: which is still human-only under the fail-safe default) from ``{}``.
_NO_GATE_CONFIG: object = object()


async def _make_hitl(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    review_id: str,
    decision: str,
    gate_config: dict | object | None = _NO_GATE_CONFIG,
    decided_at: datetime | None,
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
            gate_config_json=None if gate_config is _NO_GATE_CONFIG else gate_config,
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
        gate_config={"human_only": True},
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
        gate_config={"human_only": True},
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
        gate_config={"human_only": True},
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
        gate_config={"human_only": False},
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
        assert entry.detail["human_only_gate_decisions"] == 3
        assert entry.detail["human_only_gate_approvals"] == 2
        assert entry.detail["decisions_by_outcome"] == {"approved": 3, "rejected": 1}
        assert entry.detail["human_only_scan_truncated"] is False
        assert entry.detail["human_only_counts_are_lower_bounds"] is False

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


class TestHumanOnlyCountingMatchesTheProductRule:
    """The index must not re-implement the ``human_only`` fail-safe default.

    Every pre-existing fixture seeded ``{"human_only": True}``, which is exactly
    why treating a missing/``null`` key as NOT human-only was invisible: the
    index silently under-reported human oversight for every legacy or raw-JSON
    gate. These cases are the ones that distinguish the two rules.
    """

    #: The config shapes a real instance holds. ``None`` is a NULL
    #: ``gate_config_json``; ``{"note": ...}`` is a gate that omits the key.
    CONFIGS: tuple[object, ...] = (
        None,
        {"human_only": None},
        {},
        {"note": "a legacy gate with no human_only key"},
        {"human_only": True},
        {"human_only": False},
    )

    async def _seed_one_per_config(self, session: AsyncSession) -> dict[str, uuid.UUID]:
        seeded = await _seed_full_org(session, slug="rule")
        for index, config in enumerate(self.CONFIGS):
            await _make_hitl(
                session,
                org_id=seeded["org_id"],
                run_id=seeded["run_id"],
                pipeline_id=seeded["pipeline_id"],
                account_id=seeded["account_id"],
                review_id=f"review-config-{index}",
                decision="approved",
                gate_config=config,
                decided_at=_BASE_TIME + timedelta(hours=10 + index),
            )
        await session.commit()
        return seeded

    def test_the_fixture_configs_span_both_outcomes(self) -> None:
        """Guard: if the product rule changed, this matrix would stop discriminating."""
        outcomes = {human_only_effective(config) for config in self.CONFIGS}  # type: ignore[arg-type]

        assert outcomes == {True, False}

    async def test_human_only_counts_match_the_product_rule_for_every_config(self, session: AsyncSession) -> None:
        seeded = await self._seed_one_per_config(session)
        expected_human_only = sum(1 for config in self.CONFIGS if human_only_effective(config))  # type: ignore[arg-type]

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        # 5 of the 6 shapes are human-only under the fail-safe default; only an
        # explicit ``false`` opts out.
        assert expected_human_only == 5
        # ``_seed_full_org`` contributes three human-only decisions (two
        # approved, one rejected) plus one machine-decidable approval.
        seeded_human_only, seeded_approvals = 3, 2
        assert entry.detail["human_only_gate_decisions"] == seeded_human_only + expected_human_only
        # All six new decisions are approvals, so each human-only one counts.
        assert entry.detail["human_only_gate_approvals"] == seeded_approvals + expected_human_only
        assert entry.record_count == 4 + len(self.CONFIGS)

    async def test_deliver_manual_counts_as_an_approval(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session, slug="deliver-manual")
        await _make_hitl(
            session,
            org_id=seeded["org_id"],
            run_id=seeded["run_id"],
            pipeline_id=seeded["pipeline_id"],
            account_id=seeded["account_id"],
            review_id="review-deliver-manual",
            decision="deliver_manual",
            gate_config={"human_only": True},
            decided_at=_BASE_TIME + timedelta(hours=11),
        )
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        assert entry.detail["human_only_gate_decisions"] == 4
        # The seeded org has two ``approved`` human-only decisions; the
        # deliver_manual one is the third approval, not a fourth non-approval.
        assert entry.detail["human_only_gate_approvals"] == 3
        assert entry.detail["human_only_approval_outcomes"] == ["approved", "deliver_manual"]


class TestHumanOnlyResolutionUsesTheFullPrecedenceChain:
    """The index must resolve ``human_only`` the way the pending surfaces do.

    ``hitl_claims.gate_config_json`` is nullable (migration 0195): a gate that
    fired before the column existed, or one whose fire-time stamp failed,
    carries NULL while its frozen run snapshot still names the gate's config.
    The pending surfaces resolve through the shared
    ``resolve_review_human_only_map`` chain (claim stamp -> run snapshot ->
    fail-safe default). Reading only the stamp makes the fail-safe
    ``human_only_effective(None)`` answer for a gate whose snapshot says
    ``human_only: false``, which over-reports human oversight in an audit
    artefact - the one direction the pack must never be wrong in.
    """

    async def _seed_snapshot_gate(
        self,
        session: AsyncSession,
        *,
        snapshot_config: dict,
        claim_config: dict | None,
        slug: str,
    ) -> dict[str, uuid.UUID]:
        seeded = await _seed_full_org(session, slug=slug)
        source, target = "node-gate-source", "node-gate-target"
        snapshot_id = await _make_snapshot(
            session,
            seeded["org_id"],
            seeded["pipeline_id"],
            snapshot_version=2,
            graph_json={
                "nodes": [{"id": source}, {"id": target}],
                "edges": [
                    {
                        "source": source,
                        "target": target,
                        "hitl_review_config": snapshot_config,
                    }
                ],
            },
        )
        run_id = await _make_run(session, seeded["org_id"], seeded["pipeline_id"], snapshot_id, run_number=2)
        await _make_hitl(
            session,
            org_id=seeded["org_id"],
            run_id=run_id,
            pipeline_id=seeded["pipeline_id"],
            account_id=seeded["account_id"],
            review_id=make_review_id(source, target),
            decision="approved",
            gate_config=claim_config,
            decided_at=_BASE_TIME + timedelta(hours=12),
        )
        await session.commit()
        return seeded

    async def test_unstamped_claim_uses_the_snapshot_config(self, session: AsyncSession) -> None:
        """A NULL stamp on a gate whose snapshot opted out is NOT human oversight."""
        seeded = await self._seed_snapshot_gate(
            session,
            snapshot_config={"human_only": False},
            claim_config=None,
            slug="precedence-optout",
        )

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        # The seeded org contributes 3 human-only decisions (2 approvals); the
        # unstamped snapshot-opted-out approval must not be added to either.
        assert entry.record_count == 5
        assert entry.detail["human_only_gate_decisions"] == 3
        assert entry.detail["human_only_gate_approvals"] == 2

    async def test_unstamped_claim_uses_a_human_only_snapshot_config(self, session: AsyncSession) -> None:
        """The snapshot fallback must count a gate the snapshot marks human-only."""
        seeded = await self._seed_snapshot_gate(
            session,
            snapshot_config={"human_only": True},
            claim_config=None,
            slug="precedence-optin",
        )

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        assert entry.detail["human_only_gate_decisions"] == 4
        assert entry.detail["human_only_gate_approvals"] == 3

    async def test_claim_stamp_still_wins_over_the_snapshot(self, session: AsyncSession) -> None:
        """The fire-time stamp is authoritative; the snapshot is only a fallback."""
        seeded = await self._seed_snapshot_gate(
            session,
            snapshot_config={"human_only": True},
            claim_config={"human_only": False},
            slug="precedence-stamp-wins",
        )

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        assert entry.detail["human_only_gate_decisions"] == 3
        assert entry.detail["human_only_gate_approvals"] == 2


class TestHitlScanCorrectness:
    async def test_truncated_scan_reports_lower_bounds(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evidence_index_module, "_HITL_SCAN_LIMIT", 2)
        seeded = await _seed_full_org(session, slug="truncated")
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        assert entry.detail["human_only_scan_truncated"] is True
        # The counts are derived from the 2-row window, so they must not be
        # presented as exact.
        assert entry.detail["human_only_counts_are_lower_bounds"] is True
        assert entry.detail["human_only_gate_decisions"] <= 2

    async def test_hitl_scan_orders_null_decision_times_last(self, session: AsyncSession) -> None:
        """Postgres sorts NULLs FIRST under DESC; the scan must pin them last.

        A NULL-at-the-head scan reports ``latest_decision_at: None`` and fills a
        truncated window with undated rows. SQLite already sorts NULLs last, so
        the ordering is pinned by inspecting the emitted SQL rather than by a
        result that would pass either way.
        """
        seeded = await _seed_full_org(session, slug="null-order")
        await _make_hitl(
            session,
            org_id=seeded["org_id"],
            run_id=seeded["run_id"],
            pipeline_id=seeded["pipeline_id"],
            account_id=seeded["account_id"],
            review_id="review-undated",
            decision="approved",
            gate_config={"human_only": True},
            decided_at=None,
        )
        await session.commit()

        statements: list[str] = []

        def _capture(conn: object, cursor: object, statement: str, *args: object) -> None:
            statements.append(statement)

        bind = session.get_bind()
        sync_engine = getattr(bind, "sync_engine", bind)
        event.listen(sync_engine, "before_cursor_execute", _capture)
        try:
            index = await build_evidence_index(session, seeded["org_id"])
        finally:
            event.remove(sync_engine, "before_cursor_execute", _capture)

        hitl_scans = [sql for sql in statements if "hitl_claims" in sql and "ORDER BY" in sql]

        assert hitl_scans, statements
        assert any("NULLS LAST" in sql for sql in hitl_scans), hitl_scans
        assert _by_type(index)[ARTIFACT_HITL_DECISION].detail["latest_decision_at"] is not None


class TestCollectorQueryScoping:
    async def test_run_snapshot_scan_filters_the_snapshot_organisation(self, session: AsyncSession) -> None:
        """A run may reference a snapshot row; only OUR snapshots may be counted.

        The join reaches the snapshot side independently of the run, so filtering
        only ``Run.organisation_id`` would let another tenant's snapshot into the
        count if a run ever pointed at one.
        """
        mine = await _seed_full_org(session, slug="snapshot-mine")
        other = await _seed_full_org(session, slug="snapshot-other")
        await _make_run(
            session,
            mine["org_id"],
            mine["pipeline_id"],
            other["snapshot_id"],
            run_number=2,
        )
        await session.commit()

        index = await build_evidence_index(session, mine["org_id"])
        entry = _by_type(index)[ARTIFACT_RUN_SNAPSHOT]

        assert entry.record_count == 1
        assert entry.identifiers == (f"pipeline-snapshot:{mine['snapshot_id']}@v1",)

    async def test_audit_events_without_a_chain_head_are_not_reported_as_no_events(self, session: AsyncSession) -> None:
        """A false absence reason is worse than no reason.

        Reporting "holds no audit events yet" next to ``event_count: 3`` is
        provably false and hides the real finding: the trail exists but its
        integrity cannot be evidenced.
        """
        org_id = await _make_org(session, "events-no-head")
        await _make_audit_event(session, org_id, event_type="pipeline_created", created_at=_BASE_TIME)
        await session.commit()

        index = await build_evidence_index(session, org_id)
        entry = _by_type(index)[ARTIFACT_AUDIT_CHAIN]

        assert entry.status == STATUS_ABSENT
        assert entry.detail["event_count"] == 1
        assert entry.detail["chain_head_present"] is False
        assert "no audit events" not in (entry.absence_reason or "")
        assert "chain head" in (entry.absence_reason or "")

    async def test_chain_head_without_a_last_event_id_names_only_the_head(self, session: AsyncSession) -> None:
        """``last_event_id`` is nullable (``ondelete=SET NULL``); the entry must not
        emit a bogus ``audit-chain-last-event:None`` identifier when it is unset.
        """
        org_id = await _make_org(session, "head-without-event")
        await _make_audit_event(session, org_id, event_type="pipeline_created", created_at=_BASE_TIME)
        session.add(
            AuditChainHead(
                id=uuid.uuid4(),
                organisation_id=org_id,
                last_event_hash="b" * 64,
                last_event_id=None,
                event_count=1,
            )
        )
        await session.commit()

        index = await build_evidence_index(session, org_id)
        entry = _by_type(index)[ARTIFACT_AUDIT_CHAIN]

        assert entry.status == STATUS_PRESENT
        assert entry.identifiers == ("audit-chain-head:" + "b" * 64,)

    async def test_a_collector_set_that_drifts_from_the_vocabulary_fails_loudly(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A collector added/removed/renamed without updating the vocabulary is a bug.

        Serving the index anyway would leave a silent hole in the audit
        inventory, which is the one thing the index exists to prevent.
        """
        org_id = await _make_org(session, "vocabulary-drift")
        await session.commit()
        monkeypatch.setattr(
            evidence_index_module,
            "_collect_build_identity",
            evidence_index_module._collect_release_sbom,
        )

        with pytest.raises(RuntimeError, match="does not match the indexed vocabulary"):
            await build_evidence_index(session, org_id)


class TestArtefactVocabulary:
    def test_every_indexed_type_has_a_description(self) -> None:
        for artifact_type in INDEXED_ARTIFACT_TYPES:
            assert ARTIFACT_TYPES[artifact_type].description


class TestArtifactEntryEnforcesItsOwnShape:
    """The entry model is the last line of defence for the index's claims.

    A collector that gets the status/provenance/identifier relationship wrong
    must fail loudly at construction rather than emit an entry an auditor would
    misread (``present`` with nothing named, ``absent`` with no reason, an
    artefact keyed to no mapping row at all).
    """

    @staticmethod
    def _kwargs() -> dict:
        return {
            "artifact_type": ARTIFACT_AUDIT_CHAIN,
            "status": STATUS_PRESENT,
            "provenance": PROVENANCE_OBSERVED,
            "description": "the organisation's hash-chained audit trail",
            "location": "GET /api/v1/admin/audit",
            "identifiers": ("audit-chain-head:abc",),
            "record_count": 1,
            "detail": {},
            "mapping_row_ids": ("soc2-1",),
        }

    def test_accepts_a_well_formed_present_entry(self) -> None:
        entry = ArtifactEntry.model_validate(self._kwargs())

        assert entry.status == STATUS_PRESENT
        assert entry.identifiers == ("audit-chain-head:abc",)

    def test_rejects_an_unknown_status(self) -> None:
        with pytest.raises(ValueError, match="invalid artefact status"):
            ArtifactEntry.model_validate({**self._kwargs(), "status": "probably-fine"})

    def test_rejects_an_unknown_provenance(self) -> None:
        with pytest.raises(ValueError, match="invalid artefact provenance"):
            ArtifactEntry.model_validate({**self._kwargs(), "provenance": "trust-me"})

    def test_rejects_an_entry_keyed_to_no_mapping_row(self) -> None:
        with pytest.raises(ValueError, match="not keyed to any mapping row"):
            ArtifactEntry.model_validate({**self._kwargs(), "mapping_row_ids": ()})

    def test_rejects_a_present_entry_that_names_no_identifier(self) -> None:
        with pytest.raises(ValueError, match="present but names no identifier"):
            ArtifactEntry.model_validate({**self._kwargs(), "identifiers": ()})

    def test_rejects_an_absent_entry_that_states_no_reason(self) -> None:
        with pytest.raises(ValueError, match="absent but states no reason"):
            ArtifactEntry.model_validate(
                {
                    **self._kwargs(),
                    "status": STATUS_ABSENT,
                    "identifiers": (),
                    "absence_reason": "   ",
                }
            )


class TestTimestampRendering:
    """Timestamps are rendered deterministically, including SQLite's naive ones."""

    def test_none_renders_as_none(self) -> None:
        assert evidence_index_module._iso(None) is None

    def test_a_naive_timestamp_is_normalised_to_utc(self) -> None:
        # A bare `.isoformat()` would emit no offset at all, which an auditor
        # tool would read as the wrong moment.
        assert evidence_index_module._iso(datetime(2026, 1, 15, 9, 0, 0)) == "2026-01-15T09:00:00+00:00"

    def test_an_aware_timestamp_keeps_its_own_offset(self) -> None:
        # Only naive values (SQLite) are coerced; a value that already carries
        # an offset must not be rewritten to UTC, which would misdate it.
        aware = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))

        assert evidence_index_module._iso(aware) == "2026-01-15T09:00:00+02:00"
