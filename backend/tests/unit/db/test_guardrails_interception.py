"""create_run guardrail interception (FAR-208 item 2) against in-memory SQLite.

These tests exercise the REAL ``create_run`` path (no mocks of the function
under test) and assert the ingestion-edge seam:

  * zero-guardrail fast path — a normal run is created ``pending`` when no
    guardrails are bound;
  * redaction — a redact-action guardrail's static field policy is applied to
    the PERSISTED input_payload (persisted state is post-redaction) and the
    raw value never reaches the run record;
  * block — a block-action guardrail fires → the run is created TERMINAL
    ``eval_failed`` (error_code ``eval_blocked``), never dispatched, and the
    eval_results evidence rows are persisted;
  * replay detection-only — an ``is_replay=True`` run with a violating payload
    is created ``pending`` (no act, no re-block) with detection results.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.guardrails import serialize_guardrail_pin
from modulo.core.pipeline_engine.recovery import (
    GuardrailOverrideRejectedError,
    GuardrailOverrideRequiredError,
    guardrail_override,
    recover_node,
)
from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_ENV_PROFILE = uuid.UUID("00000000-0000-0000-0000-0000000000e1")

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        Pipeline.__table__,
        Account.__table__,
        Team.__table__,
        Run.__table__,
        PipelineSnapshot.__table__,
        Journey.__table__,
        EvalDefinition.__table__,
        EvalResult.__table__,
        AuditEvent.__table__,
        AuditChainHead.__table__,
        EnvironmentProfile.__table__,
    ],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug="test-org"))
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    await session.flush()


async def _seed_kill_switch(session: AsyncSession, *, enabled: bool) -> None:
    session.add(
        Organisation(
            id=_ORG,
            name="test org",
            slug="test-org",
            guardrails_kill_switch=enabled,
            guardrails_kill_switch_at=datetime.now(UTC) if enabled else None,
        )
    )
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    await session.flush()


async def _seed_env_profile(session: AsyncSession, *, capabilities: list[str]) -> None:
    session.add(
        EnvironmentProfile(
            id=_ENV_PROFILE,
            organisation_id=_ORG,
            name="env",
            capabilities_json=capabilities,
            account_id=_ACCOUNT,
        )
    )
    await session.flush()


async def _seed_snapshot_with_pins(
    session: AsyncSession,
    *,
    guardrail_defs: list[EvalDefinition],
) -> None:
    session.add(
        PipelineSnapshot(
            id=_SNAPSHOT,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=1,
            graph_json={"nodes": [], "edges": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            guardrail_pins_json=[serialize_guardrail_pin(d) for d in guardrail_defs],
            run_context_defaults={},
        )
    )
    await session.flush()


async def _seed_guardrail(
    session: AsyncSession,
    *,
    name: str,
    action: str,
    config: dict[str, Any] | None = None,
) -> uuid.UUID:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    if config:
        cfg.update(config)
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=name,
        eval_type="guardrail",
        config_json=cfg,
        failure_behaviour="block" if action in ("block", "redact") else "warn",
        account_id=_ACCOUNT,
    )
    session.add(eval_def)
    await session.flush()
    return eval_def.id


async def _get_guardrail_row(session: AsyncSession, guardrail_id: uuid.UUID) -> EvalDefinition:
    return (await session.execute(select(EvalDefinition).where(EvalDefinition.id == guardrail_id))).scalar_one()


async def _create(
    session: AsyncSession,
    *,
    input_payload: dict[str, Any] | None = None,
    is_replay: bool | None = None,
    trigger_type: str = "manual",
) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type=trigger_type,
        input_payload=input_payload or {},
        is_replay=is_replay,
    )


async def test_create_run_zero_guardrail_fast_path(session: AsyncSession):
    await _seed(session)
    run = await _create(session, input_payload={"body": "no guards bound"})
    assert run.status == "pending"
    assert run.input_payload == {"body": "no guards bound"}


async def test_create_run_redact_action_masks_persisted_payload(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(
        session,
        name="redact-key",
        action="redact",
        config={"redaction": [{"path": "credentials.api_key", "mode": "transform"}]},
    )
    run = await _create(
        session,
        input_payload={"credentials": {"api_key": "sk-live-123"}, "body": "clean"},
    )
    # Persisted state is post-redaction — the raw value never reaches the run.
    assert run.status == "pending"
    assert run.input_payload["credentials"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
    assert run.input_payload["body"] == "clean"


async def test_create_run_block_action_creates_terminal_eval_failed(session: AsyncSession):
    await _seed(session)
    guardrail_id = await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(
        session,
        input_payload={"body": "leak SECRET_ABC12345"},
    )
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "no-secrets" in (run.error_detail or "")
    # Evidence rows persisted with detail never carrying the raw payload.
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].eval_id == guardrail_id
    assert "SECRET_ABC12345" not in (rows[0].detail or "")


async def test_create_run_clean_payload_not_blocked(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "clean text"})
    assert run.status == "pending"
    assert run.input_payload == {"body": "clean text"}


async def test_create_run_replay_is_detection_only(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    # A replay of a violating payload is NOT blocked and NOT redacted — it is
    # created pending with detection results only (item 10).
    run = await _create(
        session,
        input_payload={"credentials": {"api_key": "sk-live-123"}, "body": "leak SECRET_ABC12345"},
        is_replay=True,
    )
    assert run.status == "pending"
    assert run.input_payload["credentials"]["api_key"] == "sk-live-123"
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].passed is True  # raw regex matched → violation detected, no act


async def test_create_run_observe_mode_stamps_observed(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="shadow", action="observe")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "pending"
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].observed is True


async def test_create_run_nested_detection_field_blocks(session: AsyncSession):
    """MAJOR-2 integration: a block guardrail whose detection ``field`` is a
    nested static path (``config.credentials.api_key``) must actually fire at
    the ingestion edge — a top-level-only lookup would silently pass (fail-open)
    and create a pending run."""
    await _seed(session)
    await _seed_guardrail(
        session,
        name="nested-credential",
        action="block",
        config={
            "type": "regex",
            "field": "config.credentials.api_key",
            "pattern": r"sk-[a-z]+-\d{6}",
        },
    )
    run = await _create(
        session,
        input_payload={"config": {"credentials": {"api_key": "sk-live-123456"}}, "body": "clean"},
    )
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "nested-credential" in (run.error_detail or "")


async def test_guardrail_blocked_run_cannot_be_resurrected_via_generic_recover(session: AsyncSession):
    """MAJOR-1 integration: a guardrail-blocked terminal run cannot be
    resurrected through the generic recover_node path — it raises
    GuardrailOverrideRequiredError and stays terminal. The ONLY remediation is
    the guardrail-override path, which re-runs the guardrail pass (re-block
    safe) and flips the run to pending on clean input."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"

    # Generic recover is refused — the blocked payload must never flow into the
    # pipeline via the generic path.
    with pytest.raises(GuardrailOverrideRequiredError):
        await recover_node(
            session,
            org_id=_ORG,
            run_id=run.id,
            node_id="manual-node-1",
            input_data={"body": "clean replacement text"},
        )
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"

    # The override is re-block safe: a still-violating input is refused and the
    # run stays terminal.
    with pytest.raises(GuardrailOverrideRejectedError):
        await guardrail_override(
            session,
            org_id=_ORG,
            run_id=run.id,
            input_data={"body": "leak SECRET_ABC12345"},
        )
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"

    # Clean input through the dedicated override is the ONLY remediation — the
    # run flips to pending with is_replay=True and the post-redaction payload
    # stored.
    override_run = await guardrail_override(
        session,
        org_id=_ORG,
        run_id=run.id,
        input_data={"body": "clean replacement text"},
    )
    assert override_run.status == "pending"
    assert override_run.error_code is None
    assert override_run.is_replay is True
    assert override_run.input_payload == {"body": "clean replacement text"}


async def test_create_run_json_schema_block_detail_never_round_trips_raw_payload(session: AsyncSession):
    # jsonschema's ValidationError.message embeds the raw offending value
    # ('SECRET_ABC12345' is not of type 'boolean'). The no-raw-persist
    # contract must hold for json_schema detections too: neither the persisted
    # eval_results.detail nor runs.error_detail may carry the raw payload.
    await _seed(session)
    await _seed_guardrail(
        session,
        name="schema-guard",
        action="block",
        config={
            "type": "json_schema",
            "field": "body",
            "schema": {"type": "object", "required": ["safe"], "properties": {"safe": {"type": "boolean"}}},
        },
    )
    run = await _create(session, input_payload={"body": {"safe": "SECRET_ABC12345"}})
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "schema-guard" in (run.error_detail or "")
    assert "SECRET_ABC12345" not in (run.error_detail or "")
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert "SECRET_ABC12345" not in (rows[0].detail or "")


# ---------------------------------------------------------------------------
# FAR-223 item 7 — per-node cap enforcement at create_run (fail closed)
# ---------------------------------------------------------------------------


async def test_create_run_cap_violation_fails_closed(session: AsyncSession):
    await _seed(session)
    # 9 org-level guardrails exceeds the default per-node cap of 8 → the run
    # must fail closed as a mechanism error, never dispatch with an unbounded
    # binding.
    for i in range(9):
        await _seed_guardrail(session, name=f"g-{i}", action="observe")
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "cap" in (run.error_detail or "").lower()


async def test_create_run_cap_within_budget_passes(session: AsyncSession):
    await _seed(session)
    for i in range(8):
        await _seed_guardrail(session, name=f"g-{i}", action="observe")
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "pending"


async def test_create_run_cap_violation_respects_feature_off(session: AsyncSession):
    await _seed(session)
    for i in range(12):
        await _seed_guardrail(
            session,
            name=f"g-{i}",
            action="observe",
            config={"max_guardrails_per_node": 0},
        )
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "pending"


async def test_create_run_cap_violation_node_bound(session: AsyncSession):
    await _seed(session)
    for i in range(9):
        await _seed_guardrail(session, name=f"node-g-{i}", action="observe")
    # Re-bind all 9 as node-bound rows on the same node.
    rows = (await session.execute(select(EvalDefinition))).scalars().all()
    for row in rows:
        row.node_id = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
    await session.flush()
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "eval_failed"
    assert "cap" in (run.error_detail or "").lower()


# ---------------------------------------------------------------------------
# FAR-223 item 9 — org kill-switch downgrades to observe at run start
# ---------------------------------------------------------------------------


async def test_create_run_kill_switch_downgrades_block_to_observe(session: AsyncSession):
    await _seed_kill_switch(session, enabled=True)
    await _seed_guardrail(session, name="no-secrets", action="block")
    # A violating payload would block under normal enforcement — the kill-switch
    # must downgrade every bound guardrail to observe (shadow-only).
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "pending"
    assert run.error_code is None
    # Observe-mode results still stamp observed=True and never redact.
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].observed is True


async def test_create_run_kill_switch_never_redacts(session: AsyncSession):
    await _seed_kill_switch(session, enabled=True)
    await _seed_guardrail(
        session,
        name="redact-key",
        action="redact",
        config={"redaction": [{"path": "credentials.api_key", "mode": "transform"}]},
    )
    run = await _create(
        session,
        input_payload={"credentials": {"api_key": "sk-live-123"}, "body": "clean"},
    )
    assert run.status == "pending"
    # Shadow-only: the raw value is NOT redacted because the kill-switch
    # downgraded the action to observe before the pass.
    assert run.input_payload["credentials"]["api_key"] == "sk-live-123"


async def test_create_run_kill_switch_off_still_blocks(session: AsyncSession):
    await _seed_kill_switch(session, enabled=False)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"


# ---------------------------------------------------------------------------
# FAR-223 item 10 — snapshot-pinned guardrails on replay
# ---------------------------------------------------------------------------


async def test_create_run_replay_uses_pinned_guardrails(session: AsyncSession):
    """A replay evaluates the PINNED set, not the live rows."""
    await _seed(session)
    pinned = await _seed_guardrail(
        session,
        name="pinned-block",
        action="block",
        config={"type": "regex", "field": "body", "pattern": r"PINNED_MARKER_\d{4}"},
    )
    pinned_row = await _get_guardrail_row(session, pinned)
    await _seed_snapshot_with_pins(session, guardrail_defs=[pinned_row])
    # The LIVE row now carries a DIFFERENT pattern — the replay must evaluate
    # the PINNED pattern, never the live edit.
    pinned_row.config_json = {**pinned_row.config_json, "pattern": r"LIVE_MARKER_\d{4}"}
    await session.flush()

    run = await _create(session, input_payload={"body": "leak PINNED_MARKER_1234"}, is_replay=True)
    # Replays are detection-only: pending, never re-blocked.
    assert run.status == "pending"
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    # passed=True = the PINNED regex matched (the live pattern would not have).
    assert rows[0].passed is True


async def test_create_run_replay_skips_soft_deleted_pinned_guardrail(session: AsyncSession):
    """A pinned guardrail whose live row is gone is SKIPPED with an audit event."""
    await _seed(session)
    pinned = await _seed_guardrail(session, name="ghost-block", action="block")
    pinned_row = await _get_guardrail_row(session, pinned)
    await _seed_snapshot_with_pins(session, guardrail_defs=[pinned_row])
    # Delete the live row BEFORE the replay.
    await session.execute(EvalDefinition.__table__.delete().where(EvalDefinition.__table__.c.id == pinned))
    await session.flush()

    run = await _create(session, input_payload={"body": "clean"}, is_replay=True)
    assert run.status == "pending"
    assert run.error_code is None
    # No eval result rows for a skipped guardrail (its row no longer exists).
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert rows == []
    # The skip is audited (guardrail.skipped) so the enforcement gap is visible.
    audit = (
        (await session.execute(select(AuditEvent).where(AuditEvent.event_type == "guardrail.skipped"))).scalars().all()
    )
    assert len(audit) == 1
    assert audit[0].payload_json["guardrail"] == "ghost-block"
    assert audit[0].payload_json["reason"] == "soft_deleted"


async def test_create_run_replay_without_pins_falls_back_to_live_rows(session: AsyncSession):
    await _seed(session)
    # A snapshot with NO pins (pre-pinning snapshot) falls back to live rows.
    session.add(
        PipelineSnapshot(
            id=_SNAPSHOT,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=1,
            graph_json={"nodes": [], "edges": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            guardrail_pins_json=None,
            run_context_defaults={},
        )
    )
    await session.flush()
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"}, is_replay=True)
    assert run.status == "pending"  # detection-only replay of the live row


# ---------------------------------------------------------------------------
# FAR-223 item 7 "Plus" — conformance enforcement at dispatch time
# ---------------------------------------------------------------------------


async def test_create_run_conformance_non_conformant_block_fails_closed(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(
        session,
        name="needs-docker",
        action="block",
        config={"required_capabilities": ["docker"]},
    )
    # No EnvironmentProfile declares 'docker' → unknown → fail closed.
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "non-conformant" in (run.error_detail or "")


async def test_create_run_conformance_satisfied_passes(session: AsyncSession):
    await _seed(session)
    await _seed_env_profile(session, capabilities=["docker"])
    await _seed_guardrail(
        session,
        name="needs-docker",
        action="block",
        config={"required_capabilities": ["docker"]},
    )
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "pending"


async def test_create_run_conformance_observe_never_blocks(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(
        session,
        name="observe-needs-docker",
        action="observe",
        config={"required_capabilities": ["docker"]},
    )
    # observe/warn guardrails are advisory — conformance never fails them closed.
    run = await _create(session, input_payload={"body": "clean"})
    assert run.status == "pending"
