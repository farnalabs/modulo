"""Author-warning checks — acceptance tests (FAR-957, chunk 9a §3.2/§3.5).

In-memory SQLite coverage of ``check_author_warnings``: the three warning
conditions (no guaranteed producer, temporal ordering, recent undefined),
the false-positive exclusion (a known producer suppresses ``no_producer``),
the safe-direction fallbacks (pipeline missing / unexpected error), and the
advisory contract — the check returns warnings, it never raises, and a
racy binding therefore still binds.

Spec criteria covered here: 11, 12, 13, 14, 15, 16.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.eval_engine.author_warnings import AuthorWarning, check_author_warnings
from modulo.db.models import Base
from modulo.db.models.eval import Eval
from modulo.db.models.evidence import Evidence
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tables() -> list[Any]:
    return [
        Pipeline.__table__,
        PipelineEdge.__table__,
        Eval.__table__,
        Evidence.__table__,
    ]


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    active = maker()
    yield active
    await active.close()
    await engine.dispose()


async def _seed_pipeline(
    session: AsyncSession,
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    session.add(
        Pipeline(
            id=pipeline_id,
            organisation_id=org_id,
            account_id=uuid.uuid4(),
            name="warnings-pipe",
            graph_nodes_json=nodes or [],
            retry_policy={},
        )
    )
    for source, target in edges or []:
        session.add(
            PipelineEdge(
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                source_node_id=source,
                target_node_id=target,
            )
        )
    await session.commit()
    return org_id, pipeline_id


async def _seed_eval(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    *,
    config: dict[str, Any],
    node_id: uuid.UUID | None = None,
    deleted_at: datetime | None = None,
) -> None:
    session.add(
        Eval(
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            account_id=uuid.uuid4(),
            eval_type="guardrail",
            config_json=config,
            node_id=node_id,
            deleted_at=deleted_at,
        )
    )
    await session.commit()


async def _seed_evidence(
    session: AsyncSession,
    org_id: uuid.UUID,
    key: str,
    value: Any,
) -> None:
    session.add(
        Evidence(
            organisation_id=org_id,
            key=key,
            subject_type="eval",
            subject_id=f"subject-{uuid.uuid4().hex[:8]}",
            value=value,
            producer_type="eval",
        )
    )
    await session.commit()


async def _check(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    evidence_key: str,
    binding_node_id: uuid.UUID | None = None,
) -> list[AuthorWarning]:
    return await check_author_warnings(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        evidence_key=evidence_key,
        binding_node_id=binding_node_id or uuid.uuid4(),
    )


def _codes(warnings: list[AuthorWarning]) -> set[str]:
    return {warning.code for warning in warnings}


# ---------------------------------------------------------------------------
# Condition (a): no guaranteed producer (§3.2 criterion 12)
# ---------------------------------------------------------------------------


async def test_no_producer_warns_when_key_has_no_producer(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)

    warnings = await _check(session, org_id, pipeline_id, "orphan.key")

    assert _codes(warnings) == {"no_producer"}
    assert "orphan.key" in warnings[0].message


async def test_soft_deleted_eval_does_not_count_as_producer(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)
    await _seed_eval(
        session,
        org_id,
        pipeline_id,
        config={"evidence_key": "retired.key"},
        deleted_at=datetime.now(UTC),
    )

    warnings = await _check(session, org_id, pipeline_id, "retired.key")

    assert _codes(warnings) == {"no_producer"}


@pytest.mark.parametrize("key", ["system.health", "connector_github.status", "sandbox_ready", "capability_tools"])
async def test_system_state_keys_are_guaranteed_producers(session: AsyncSession, key: str) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)

    warnings = await _check(session, org_id, pipeline_id, key)

    assert warnings == []


# ---------------------------------------------------------------------------
# False-positive exclusion (§3.2 criterion 16): known producers suppress the warning
# ---------------------------------------------------------------------------


async def test_eval_config_producer_suppresses_no_producer(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "gated.key"}, node_id=uuid.uuid4())

    warnings = await _check(session, org_id, pipeline_id, "gated.key")

    assert warnings == []


async def test_eval_nested_detection_config_counts_as_producer(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)
    await _seed_eval(session, org_id, pipeline_id, config={"detection": {"evidence_key": "detected.key"}})

    warnings = await _check(session, org_id, pipeline_id, "detected.key")

    assert warnings == []


async def test_node_config_evidence_key_counts_as_producer(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(
        session,
        nodes=[{"id": str(uuid.uuid4()), "config": {"evidence_key": "node.key"}}],
    )

    warnings = await _check(session, org_id, pipeline_id, "node.key")

    assert warnings == []


async def test_agent_command_evidence_key_counts_as_producer(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(
        session,
        nodes=[{"id": str(uuid.uuid4()), "agent_commands": [{"evidence_key": "cmd.key"}]}],
    )

    warnings = await _check(session, org_id, pipeline_id, "cmd.key")

    assert warnings == []


# ---------------------------------------------------------------------------
# Condition (b): temporal ordering (§3.2 criterion 13)
# ---------------------------------------------------------------------------


async def test_temporal_ordering_warns_when_producer_downstream(session: AsyncSession) -> None:
    binding_node, mid_node, producer_node = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    org_id, pipeline_id = await _seed_pipeline(session, edges=[(binding_node, mid_node), (mid_node, producer_node)])
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "racy.key"}, node_id=producer_node)

    warnings = await _check(session, org_id, pipeline_id, "racy.key", binding_node_id=binding_node)

    assert _codes(warnings) == {"temporal_ordering"}
    assert "racy.key" in warnings[0].message


async def test_temporal_ordering_silent_when_producer_upstream(session: AsyncSession) -> None:
    producer_node, binding_node = uuid.uuid4(), uuid.uuid4()
    org_id, pipeline_id = await _seed_pipeline(session, edges=[(producer_node, binding_node)])
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "ordered.key"}, node_id=producer_node)

    warnings = await _check(session, org_id, pipeline_id, "ordered.key", binding_node_id=binding_node)

    assert warnings == []


async def test_temporal_ordering_silent_when_same_node(session: AsyncSession) -> None:
    node = uuid.uuid4()
    org_id, pipeline_id = await _seed_pipeline(session)
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "same.key"}, node_id=node)

    warnings = await _check(session, org_id, pipeline_id, "same.key", binding_node_id=node)

    assert warnings == []


# ---------------------------------------------------------------------------
# Condition (c): recent undefined (§3.2 criterion 14)
# ---------------------------------------------------------------------------


async def test_recent_undefined_warns_on_null_values(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)
    # A registered producer keeps condition (a) out of the way — the
    # three warning conditions are independent of each other.
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "flaky.key"}, node_id=uuid.uuid4())
    for value in (None, None, True):
        await _seed_evidence(session, org_id, "flaky.key", value)

    warnings = await _check(session, org_id, pipeline_id, "flaky.key")

    assert _codes(warnings) == {"recent_undefined"}
    assert "2 of the last 3" in warnings[0].message


async def test_recent_undefined_silent_when_all_values_defined(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "solid.key"}, node_id=uuid.uuid4())
    for value in (True, False, 42):
        await _seed_evidence(session, org_id, "solid.key", value)

    warnings = await _check(session, org_id, pipeline_id, "solid.key")

    assert warnings == []


async def test_recent_undefined_silent_when_evidence_store_dormant(session: AsyncSession) -> None:
    org_id, pipeline_id = await _seed_pipeline(session)
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "fresh.key"}, node_id=uuid.uuid4())

    warnings = await _check(session, org_id, pipeline_id, "fresh.key")

    assert warnings == []


# ---------------------------------------------------------------------------
# Safe-direction fallbacks (§3.2) + advisory contract (§3.5 criterion 15)
# ---------------------------------------------------------------------------


async def test_pipeline_missing_falls_back_to_no_producer_warning(session: AsyncSession) -> None:
    warnings = await _check(session, uuid.uuid4(), uuid.uuid4(), "any.key")

    assert _codes(warnings) == {"no_producer"}
    assert "pipeline not found" in warnings[0].message


class _ExplodingSession:
    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("graph store unavailable")


async def test_unexpected_error_falls_back_to_warning(session: AsyncSession) -> None:
    warnings = await check_author_warnings(
        _ExplodingSession(),  # type: ignore[arg-type]
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        evidence_key="any.key",
        binding_node_id=uuid.uuid4(),
    )

    assert _codes(warnings) == {"no_producer"}
    assert "unexpected error" in warnings[0].message


async def test_racy_binding_accumulates_warnings_but_still_binds(session: AsyncSession) -> None:
    binding_node, mid_node, producer_node = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    org_id, pipeline_id = await _seed_pipeline(session, edges=[(binding_node, mid_node), (mid_node, producer_node)])
    await _seed_eval(session, org_id, pipeline_id, config={"evidence_key": "racy.key"}, node_id=producer_node)
    await _seed_evidence(session, org_id, "racy.key", None)

    warnings = await _check(session, org_id, pipeline_id, "racy.key", binding_node_id=binding_node)

    # Advisory by construction: the call above returned (never raised) with
    # every risk flagged, so a gate bound to this key is still created.
    assert _codes(warnings) == {"temporal_ordering", "recent_undefined"}


def test_warning_to_dict_shape() -> None:
    warning = AuthorWarning("no_producer", "Key 'k' has no guaranteed producer.")

    assert warning.to_dict() == {"code": "no_producer", "message": "Key 'k' has no guaranteed producer."}
