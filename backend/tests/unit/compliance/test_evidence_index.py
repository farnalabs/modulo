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
from modulo.core.eval_engine import EvalType
from modulo.db.crud.hitl_review_config import human_only_effective
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
            eval_type=EvalType.MODEL_JUDGE,
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
            outcome="pass" if passed else "fail",
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

        # The comparison above only proves the index stores what the pack's
        # OWN lookup returns - it cannot catch a lookup that is wrong for both
        # sides. Pin one artefact type's ids to values read from the shipped
        # YAML (mappings/soc2.yaml, mappings/iso-42001.yaml, in pack order) so
        # the lookup itself is grounded in the pack content.
        run_snapshot = _by_type(index)[ARTIFACT_RUN_SNAPSHOT]
        assert run_snapshot.mapping_row_ids == ("soc2-cc8.1b", "iso-42001-8.1", "iso-42001-8.3")

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
        await _seed_full_org(session, slug="other")
        await session.commit()

        my_index = await build_evidence_index(session, mine["org_id"])
        entries = _by_type(my_index)

        assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 3
        assert entries[ARTIFACT_HITL_DECISION].record_count == 4
        assert entries[ARTIFACT_EVAL_RESULT].record_count == 3
        assert entries[ARTIFACT_RUN_SNAPSHOT].record_count == 1

        # Both orgs were fully seeded, so the "other" org's run snapshot is in
        # the very table this scan reads. A random-UUID inequality ("mine is
        # not theirs") proves nothing; the real isolation claim is that my
        # index's run_snapshot identifiers name ONLY my snapshot.
        run_snapshot = entries[ARTIFACT_RUN_SNAPSHOT]
        assert run_snapshot.identifiers == (f"pipeline-snapshot:{mine['snapshot_id']}@v1",)

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


class TestHitlScanBoundary:
    """The scan limit is an inclusive cap: ``total <= limit`` is exact; one past it truncates.

    Seeding 10,000 real rows would exercise the same code path at prohibitive
    cost, so the limit is patched down to 4 and the boundary is walked from
    both sides. ``_seed_full_org`` seeds exactly 4 decided gates, which is the
    exact-boundary side.
    """

    _LIMIT = 4

    async def test_exactly_at_the_limit_the_counts_are_exact_and_untagged(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evidence_index_module, "_HITL_SCAN_LIMIT", self._LIMIT)
        seeded = await _seed_full_org(session, slug="boundary-exact")
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        assert entry.record_count == self._LIMIT
        assert entry.detail["human_only_scan_truncated"] is False
        assert entry.detail["human_only_counts_are_lower_bounds"] is False
        assert entry.detail["human_only_gate_decisions"] == 3
        assert entry.detail["human_only_gate_approvals"] == 2

    async def test_one_past_the_limit_truncates_and_reports_lower_bounds(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evidence_index_module, "_HITL_SCAN_LIMIT", self._LIMIT)
        seeded = await _seed_full_org(session, slug="boundary-plus-one")
        await _make_hitl(
            session,
            org_id=seeded["org_id"],
            run_id=seeded["run_id"],
            pipeline_id=seeded["pipeline_id"],
            account_id=seeded["account_id"],
            review_id="review-boundary-overflow",
            decision="approved",
            gate_config={"human_only": True},
            decided_at=_BASE_TIME + timedelta(hours=12),
        )
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])
        entry = _by_type(index)[ARTIFACT_HITL_DECISION]

        # The COUNT itself is not windowed - it still counts every decided
        # gate - while the per-decision counts come from the truncated window
        # and must never be presented as exact.
        assert entry.record_count == self._LIMIT + 1
        assert entry.detail["human_only_scan_truncated"] is True
        assert entry.detail["human_only_counts_are_lower_bounds"] is True
        # The 4-row window holds exactly three human-only decisions and two
        # approvals under EITHER sort direction. Dropping the ``.limit()``
        # clause or an off-by-one window size yields 4 decisions here, so a
        # ``<=`` bound would absorb the very mutations this boundary exists
        # to catch.
        assert entry.detail["human_only_gate_decisions"] == 3
        assert entry.detail["human_only_gate_approvals"] == 2


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


class TestAuditPeriodScoping:
    """FAR-2055: ``period_from`` / ``period_to`` scope every artefact aggregate.

    ``_seed_full_org`` spreads its rows across hours so one window can include
    some artefact types and exclude others - a scoping bug that only filtered
    one collector is visible here.
    """

    #: Everything seeded strictly before +7h: the audit event at +0, the run at
    #: +0, all four HITL decisions (+1..+4) and the first two eval results
    #: (+5, +6). The autonomy events (+8, +9) are outside it.
    EARLY_FROM = _BASE_TIME
    EARLY_TO = _BASE_TIME + timedelta(hours=7)
    #: The tail of the seed: audit events at +8/+9, one eval result at +7, and
    #: both autonomy changes. Nothing else.
    LATE_FROM = _BASE_TIME + timedelta(hours=7)
    LATE_TO = _BASE_TIME + timedelta(hours=10)

    async def test_unscoped_default_covers_full_history_and_records_no_period(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(session, seeded["org_id"])

        assert index.period_from is None
        assert index.period_to is None
        entries = _by_type(index)
        assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 3
        assert entries[ARTIFACT_HITL_DECISION].record_count == 4
        assert entries[ARTIFACT_EVAL_RESULT].record_count == 3
        assert entries[ARTIFACT_AUTONOMY_CHANGE].record_count == 2

    async def test_a_scoped_index_records_the_period_it_covers(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=self.EARLY_FROM,
            period_to=self.EARLY_TO,
        )

        assert index.period_from == self.EARLY_FROM.isoformat()
        assert index.period_to == self.EARLY_TO.isoformat()

    async def test_the_early_window_counts_only_rows_inside_it(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=self.EARLY_FROM,
            period_to=self.EARLY_TO,
        )
        entries = _by_type(index)

        assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 1
        assert entries[ARTIFACT_RUN_SNAPSHOT].record_count == 1
        assert entries[ARTIFACT_HITL_DECISION].record_count == 4
        assert entries[ARTIFACT_EVAL_RESULT].record_count == 2
        assert entries[ARTIFACT_AUTONOMY_CHANGE].status == STATUS_ABSENT

    async def test_the_late_window_counts_the_tail_and_nothing_else(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=self.LATE_FROM,
            period_to=self.LATE_TO,
        )
        entries = _by_type(index)

        assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 2
        assert entries[ARTIFACT_RUN_SNAPSHOT].status == STATUS_ABSENT
        assert entries[ARTIFACT_HITL_DECISION].status == STATUS_ABSENT
        assert entries[ARTIFACT_EVAL_RESULT].record_count == 1
        assert entries[ARTIFACT_AUTONOMY_CHANGE].record_count == 2

    async def test_a_scoped_absence_reason_names_the_period(self, session: AsyncSession) -> None:
        """A scoped absence must never read as "this org has no rows at all"."""
        seeded = await _seed_full_org(session)
        await session.commit()

        index = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=self.LATE_FROM,
            period_to=self.LATE_TO,
        )

        reason = _by_type(index)[ARTIFACT_HITL_DECISION].absence_reason or ""
        assert "requested audit period" in reason
        # The bound itself is named, so a reader sees WHICH window was empty.
        assert self.LATE_FROM.isoformat() in reason

    async def test_an_open_start_window_bounds_only_the_end(self, session: AsyncSession) -> None:
        """``period_to`` alone is a supported open-start window, not a dead arm.

        Only the upper bound is set, so every row from the beginning of history
        up to (but not including) it must still be counted. The absence reason
        must name only the bound that was actually requested: adding an
        "on or after" leg that was never supplied would misdescribe the window
        an auditor is reading.
        """
        seeded = await _seed_full_org(session)
        await session.commit()
        upper = _BASE_TIME + timedelta(hours=7)

        index = await build_evidence_index(session, seeded["org_id"], period_to=upper)
        entries = _by_type(index)

        assert index.period_from is None
        assert index.period_to == upper.isoformat()
        # Rows before the bound are still counted, so the window really is open
        # on the left (all four HITL decisions sit at +1h..+4h).
        assert entries[ARTIFACT_HITL_DECISION].record_count == 4
        # Both autonomy events sit after the bound (+8h, +9h), so they are the
        # artefact type this one-sided window excludes entirely.
        autonomy = entries[ARTIFACT_AUTONOMY_CHANGE]
        assert autonomy.status == STATUS_ABSENT
        reason = autonomy.absence_reason or ""
        assert reason.endswith(f" in the requested audit period (before {upper.isoformat()})")
        assert "on or after" not in reason

    async def test_an_open_ended_window_bounds_only_the_start(self, session: AsyncSession) -> None:
        """``period_from`` alone is a supported open-ended window, not a dead arm.

        Only the lower bound is set, so rows at or after it are counted and
        everything earlier is excluded. The absence reason must name only the
        start: a "before" leg that was never requested would misstate which
        half of the interval was empty.
        """
        seeded = await _seed_full_org(session)
        await session.commit()
        lower = _BASE_TIME + timedelta(hours=8)

        index = await build_evidence_index(session, seeded["org_id"], period_from=lower)
        entries = _by_type(index)

        assert index.period_from == lower.isoformat()
        assert index.period_to is None
        # Every HITL decision was made before the bound (+1h..+4h), so this
        # artefact is excluded; the audit trail at +8h/+9h is still counted,
        # proving the window stays open above the bound.
        hitl = entries[ARTIFACT_HITL_DECISION]
        assert hitl.status == STATUS_ABSENT
        assert entries[ARTIFACT_AUDIT_CHAIN].record_count == 2
        reason = hitl.absence_reason or ""
        assert reason.endswith(f" in the requested audit period (on or after {lower.isoformat()})")
        assert "before" not in reason

    async def test_the_half_open_end_bound_is_exclusive(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()
        # The eval result at exactly +5h is the only row at this instant.
        boundary = _BASE_TIME + timedelta(hours=5)

        index = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=boundary,
            period_to=boundary,
        )

        # Empty window: the +5h row is NOT included (to is exclusive).
        assert _by_type(index)[ARTIFACT_EVAL_RESULT].status == STATUS_ABSENT

    async def test_a_start_after_the_end_is_rejected(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        with pytest.raises(ValueError, match="must not be after its end"):
            await build_evidence_index(
                session,
                seeded["org_id"],
                period_from=self.LATE_FROM,
                period_to=self.EARLY_FROM,
            )

    async def test_naive_bounds_are_read_as_utc(self, session: AsyncSession) -> None:
        seeded = await _seed_full_org(session)
        await session.commit()

        naively_scoped = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=self.EARLY_FROM.replace(tzinfo=None),
            period_to=self.EARLY_TO.replace(tzinfo=None),
        )
        aware_scoped = await build_evidence_index(
            session,
            seeded["org_id"],
            period_from=self.EARLY_FROM,
            period_to=self.EARLY_TO,
        )

        assert naively_scoped.period_from == aware_scoped.period_from
        assert [entry.record_count for entry in naively_scoped.artifacts] == [
            entry.record_count for entry in aware_scoped.artifacts
        ]

    async def test_a_scoped_index_keeps_the_tenant_boundary(self, session: AsyncSession) -> None:
        mine = await _seed_full_org(session, slug="window-mine")
        await _seed_full_org(session, slug="window-other")
        await session.commit()

        index = await build_evidence_index(
            session,
            mine["org_id"],
            period_from=self.EARLY_FROM,
            period_to=self.EARLY_TO,
        )

        assert index.organisation_id == str(mine["org_id"])
        assert _by_type(index)[ARTIFACT_RUN_SNAPSHOT].identifiers == (f"pipeline-snapshot:{mine['snapshot_id']}@v1",)


class TestNormaliseAuditPeriod:
    def test_both_none_stay_none(self) -> None:
        assert evidence_index_module.normalise_audit_period(None, None) == (None, None)

    def test_a_naive_bound_gains_utc(self) -> None:
        start, end = evidence_index_module.normalise_audit_period(
            datetime(2026, 1, 15, 9, 0, 0),
            datetime(2026, 1, 16, 9, 0, 0),
        )

        assert start == datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 1, 16, 9, 0, 0, tzinfo=UTC)

    def test_an_aware_bound_keeps_its_offset(self) -> None:
        aware = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))

        assert evidence_index_module.normalise_audit_period(aware, None)[0] is aware

    def test_an_inverted_period_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be after its end"):
            evidence_index_module.normalise_audit_period(
                datetime(2026, 1, 16, tzinfo=UTC),
                datetime(2026, 1, 15, tzinfo=UTC),
            )
