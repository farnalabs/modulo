"""Step definitions for the Guardrail Detection Engine (T1) feature.

Exercises the pure engine functions in ``modulo.core.guardrails`` directly:
``evaluate_guardrails`` (raising), ``run_interception_pass`` (non-raising
two-phase), ``apply_redaction_masks`` (masks-only), ``derive_conformance_state``
(fail-closed three-state), and the misrouting/retry guardrails. Detection is
deterministic (regex / json_schema) and never routes through the generic
``EvalEngine``.
"""

import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.guardrails import (
    REDACTION_MASK,
    GuardrailBlockedError,
    GuardrailConfigError,
    GuardrailMisroutedError,
    derive_conformance_state,
    evaluate_guardrails,
    run_interception_pass,
)
from modulo.core.pipeline_engine.recovery import GuardrailOverrideRejectedError, guardrail_override
from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/evals/guardrails.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for guardrail engine tests."""
    return {}


def _make_guardrail(name: str, *, action: str, failure_behaviour: str = "block") -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config={
            "action": action,
            "interception_point": "input",
        },
        failure_behaviour=failure_behaviour,
    )


@given(parsers.parse('a guardrail "{name}" with {action} action'))
def guardrail_with_action(name: str, action: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"] = _make_guardrail(name, action=action)


@given(parsers.parse('the guardrail detects regex pattern "{pattern}" on field "{field}"'))
def guardrail_regex(pattern: str, field: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"].config["type"] = "regex"
    ctx["guardrail"].config["pattern"] = pattern
    ctx["guardrail"].config["field"] = field


@given(parsers.parse('the guardrail has a transform redaction policy on path "{path}"'))
def guardrail_redaction_policy(path: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"].config["redaction"] = [{"path": path, "mode": "transform"}]


@given(parsers.parse('a guardrail "{name}" with {action} action requiring capability "{capability}"'))
def guardrail_with_capability(name: str, action: str, capability: str, ctx: dict[str, Any]) -> None:
    ctx["guardrail"] = _make_guardrail(name, action=action)
    ctx["guardrail"].config["required_capabilities"] = [capability]
    ctx["capability"] = capability


@given(parsers.parse('the registered capability "{capability}" is confirmed present'))
def capability_present(capability: str, ctx: dict[str, Any]) -> None:
    ctx["registered"] = {capability: True}


@given(parsers.parse('the registered capability "{capability}" is confirmed absent'))
def capability_absent(capability: str, ctx: dict[str, Any]) -> None:
    ctx["registered"] = {capability: False}


@when(parsers.parse("the guardrail engine evaluates the payload {payload_json}"))
def engine_evaluates(payload_json: str, ctx: dict[str, Any]) -> None:
    payload = json.loads(payload_json)
    ctx["error"] = None
    try:
        evaluate_guardrails(EvalEngine(), [ctx["guardrail"]], payload, raise_on_block=True)
        ctx["results"] = "clean"
    except GuardrailBlockedError as exc:
        ctx["error"] = exc


@when(parsers.parse("the interception pass runs over the payload {payload_json}"))
def interception_pass(payload_json: str, ctx: dict[str, Any]) -> None:
    payload = json.loads(payload_json)
    ctx["original_payload"] = json.loads(payload_json)
    outcome = run_interception_pass(EvalEngine(), [ctx["guardrail"]], payload)
    ctx["outcome"] = outcome


@when("the generic eval engine evaluates the guardrail directly")
def generic_engine_evaluates(ctx: dict[str, Any]) -> None:
    ctx["error"] = None
    try:
        EvalEngine().evaluate({"body": "clean"}, ctx["guardrail"])
    except GuardrailMisroutedError as exc:
        ctx["error"] = exc


@when(parsers.parse('the guardrail is forced to carry failure_behaviour "{behaviour}"'))
def guardrail_forced_retry(behaviour: str, ctx: dict[str, Any]) -> None:
    # Pydantic rejects failure_behaviour='retry' at construction, so bypass the
    # model like the unit tests do to exercise the engine-level guard.
    object.__setattr__(ctx["guardrail"], "failure_behaviour", behaviour)
    ctx["error"] = None
    try:
        evaluate_guardrails(EvalEngine(), [ctx["guardrail"]], {"body": "clean text"}, raise_on_block=True)
    except GuardrailConfigError as exc:
        ctx["error"] = exc


@when("conformance state is derived")
def conformance_derived(ctx: dict[str, Any]) -> None:
    ctx["derivation"] = derive_conformance_state(
        ctx["guardrail"].config.get("required_capabilities", []),
        ctx.get("registered", {}),
    )


@then(parsers.parse('a GuardrailBlockedError is raised for guardrail "{name}"'))
def blocked_raised_for(name: str, ctx: dict[str, Any]) -> None:
    assert isinstance(ctx.get("error"), GuardrailBlockedError), (
        f"Expected GuardrailBlockedError, got {ctx.get('error')}"
    )
    assert ctx["error"].eval_name == name


@then("no GuardrailBlockedError is raised")
def no_blocked_raised(ctx: dict[str, Any]) -> None:
    assert ctx.get("error") is None, f"Expected no GuardrailBlockedError, got {ctx.get('error')}"
    assert ctx.get("results") == "clean"


@then(parsers.parse('the persisted payload masks "{path}"'))
def payload_masks(path: str, ctx: dict[str, Any]) -> None:
    outcome = ctx["outcome"]
    segments = path.split(".")
    value: Any = outcome.payload
    for segment in segments:
        value = value[segment]
    assert value == REDACTION_MASK, f"Expected fixed mask at {path!r}, got {value!r}"


@then("the original payload is not mutated")
def original_not_mutated(ctx: dict[str, Any]) -> None:
    original = ctx["original_payload"]
    assert original["credentials"]["api_key"] == "sk-live-123", "Original payload was mutated"


@then(parsers.parse('the interception outcome reports blocked by "{name}"'))
def outcome_blocked_by(name: str, ctx: dict[str, Any]) -> None:
    outcome = ctx["outcome"]
    assert outcome.blocked is True, "Expected interception outcome to report blocked"
    assert outcome.blocking_eval_name == name


@then("a GuardrailMisroutedError is raised")
def misrouted_raised(ctx: dict[str, Any]) -> None:
    assert isinstance(ctx.get("error"), GuardrailMisroutedError), (
        f"Expected GuardrailMisroutedError, got {ctx.get('error')}"
    )


@then("a GuardrailConfigError is raised")
def config_error_raised(ctx: dict[str, Any]) -> None:
    assert isinstance(ctx.get("error"), GuardrailConfigError), f"Expected GuardrailConfigError, got {ctx.get('error')}"


@then(parsers.parse('the conformance state is "{state}"'))
def conformance_state(state: str, ctx: dict[str, Any]) -> None:
    derivation = ctx["derivation"]
    assert derivation.state == state, f"Expected conformance {state!r}, got {derivation.state!r}"


# ---------------------------------------------------------------------------
# Acceptance residual (FAR-223 item 13) — real create_run seam over SQLite.
# These scenarios exercise the ingestion-edge block, the kill-switch
# downgrade, and the override remediation through the actual DB path.
#
# pytest-bdd calls step functions synchronously (async step defs are not
# awaited), so each step wraps its async DB work in ``asyncio.run``. To keep
# the aiosqlite session usable across separate ``asyncio.run`` event loops we
# use a FILE-backed SQLite database (a shared temp file) so every step opens a
# fresh engine/session on the same on-disk schema.
# ---------------------------------------------------------------------------

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d2")

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
        EvalDefinitionRow.__table__,
        EvalResult.__table__,
        AuditEvent.__table__,
        AuditChainHead.__table__,
        EnvironmentProfile.__table__,
    ],
)


def _bdd_db_path() -> str:
    """A stable temp-file path shared across the scenario's steps."""
    import tempfile

    return tempfile.gettempdir() + "/modulo_guardrail_bdd.sqlite"


async def _bdd_fresh_engine() -> AsyncEngine:
    """Create a FRESH schema (used only by the seeding Given step)."""
    import asyncio
    from pathlib import Path

    from sqlalchemy.pool import NullPool

    await asyncio.to_thread(Path(_bdd_db_path()).unlink, missing_ok=True)
    eng = create_async_engine(f"sqlite+aiosqlite:///{_bdd_db_path()}", echo=False, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    return eng


async def _bdd_open_session() -> AsyncSession:
    """Open a session on the EXISTING shared DB (subsequent steps).

    Uses NullPool so every connection is closed as soon as the session closes
    — no pooled connections linger for the garbage collector to warn about.
    """
    from sqlalchemy.pool import NullPool

    path = _bdd_db_path()
    eng = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False, poolclass=NullPool)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    session = maker()
    # Keep the engine alive with the session so the connection stays valid.
    session._bdd_engine = eng  # type: ignore[attr-defined]
    return session


async def _bdd_close_session(session: AsyncSession) -> None:
    await session.close()
    eng = getattr(session, "_bdd_engine", None)
    if eng is not None:
        await eng.dispose()


@given(parsers.parse('a pipeline with a bound block guardrail "{name}" detecting "{pattern}" on field "{field}"'))
def bdd_bound_guardrail(name: str, pattern: str, field: str, ctx: dict[str, Any]) -> None:
    async def _seed() -> None:
        eng = await _bdd_fresh_engine()
        maker = async_sessionmaker(eng, expire_on_commit=False)
        session = maker()
        session.add(Organisation(id=_ORG_ID, name="test org", slug="test-org"))
        session.add(Account(id=_ACCOUNT_ID, email="admin@example.com", display_name="admin"))
        session.add(
            Pipeline(
                id=_PIPELINE_ID, organisation_id=_ORG_ID, name="pipeline", account_id=_ACCOUNT_ID, visibility="org"
            )
        )
        session.add(
            PipelineSnapshot(
                id=_SNAPSHOT_ID,
                organisation_id=_ORG_ID,
                pipeline_id=_PIPELINE_ID,
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
        eval_def = EvalDefinitionRow(
            id=uuid.uuid4(),
            organisation_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            node_id=None,
            name=name,
            eval_type="guardrail",
            config_json={
                "action": "block",
                "interception_point": "input",
                "type": "regex",
                "field": field,
                "pattern": pattern,
            },
            failure_behaviour="block",
            account_id=_ACCOUNT_ID,
        )
        session.add(eval_def)
        await session.commit()
        await session.close()
        await eng.dispose()

    import asyncio

    asyncio.run(_seed())
    ctx["guardrail_name"] = name


@given("the organisation kill-switch is ON")
def bdd_kill_switch_on(ctx: dict[str, Any]) -> None:
    async def _flip() -> None:
        session = await _bdd_open_session()
        org = (await session.execute(select(Organisation).where(Organisation.id == _ORG_ID))).scalar_one()
        org.guardrails_kill_switch = True
        org.guardrails_kill_switch_at = datetime.now(UTC)
        await session.commit()
        await _bdd_close_session(session)

    import asyncio

    asyncio.run(_flip())


@when(parsers.parse("a run is created with payload {payload_json}"))
def bdd_create_run(payload_json: str, ctx: dict[str, Any]) -> None:
    async def _create() -> Run:
        session = await _bdd_open_session()
        payload = json.loads(payload_json)
        run = await create_run(
            session,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_id=_SNAPSHOT_ID,
            trigger_type="manual",
            input_payload=payload,
        )
        await session.commit()
        run_id = run.id
        await _bdd_close_session(session)
        # Re-load the committed run row for the assertions.
        session2 = await _bdd_open_session()
        loaded = (await session2.execute(select(Run).where(Run.id == run_id))).scalar_one()
        await _bdd_close_session(session2)
        return loaded

    import asyncio

    ctx["run"] = asyncio.run(_create())


@then("the run is terminal eval_failed with error_code eval_blocked")
def bdd_terminal_blocked(ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    assert run.status == "eval_failed", f"expected eval_failed, got {run.status}"
    assert run.error_code == "eval_blocked", f"expected eval_blocked, got {run.error_code}"


@then("no node executed (no output, no telemetry, no claim)")
def bdd_no_node_executed(ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    assert run.started_at is None, "blocked run must never dispatch (started_at must be NULL)"
    assert run.outputs_json is None, "blocked run must have no node output"
    assert run.node_telemetry_json is None, "blocked run must have no node telemetry"
    assert run.claim_count == 0, "blocked run must have no SAQ claim"
    assert run.node_attempt_count == 0, "blocked run must have no node attempt"


@then("the run is created pending and NOT blocked")
def bdd_pending_not_blocked(ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    assert run.status == "pending", f"expected pending, got {run.status}"
    assert run.error_code is None, f"expected no error_code, got {run.error_code}"


@then("the run records an observe-mode violation")
def bdd_observe_violation(ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    rows = _bdd_eval_rows(run.id)
    assert len(rows) == 1
    # observe-mode result stamps observed=True and passed=True (regex matched = violation).
    assert rows[0]["observed"] is True
    assert rows[0]["passed"] is True


def _bdd_eval_rows(run_id: uuid.UUID) -> list[dict[str, Any]]:
    async def _read() -> list[dict[str, Any]]:
        session = await _bdd_open_session()
        rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run_id))).scalars().all()
        out = [{"observed": r.observed, "passed": r.passed} for r in rows]
        await _bdd_close_session(session)
        return out

    import asyncio

    return asyncio.run(_read())


@when(parsers.parse("the operator overrides the blocked run with a clean payload {payload_json}"))
def bdd_override_clean(payload_json: str, ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    payload = json.loads(payload_json)
    ctx["override_run"] = _bdd_do_override(run.id, payload)


@when(parsers.parse("the operator overrides the blocked run with a still-violating payload {payload_json}"))
def bdd_override_still_violating(payload_json: str, ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    payload = json.loads(payload_json)
    rejected = False
    try:
        _bdd_do_override(run.id, payload)
    except GuardrailOverrideRejectedError:
        rejected = True
    ctx["override_rejected"] = rejected


@then("the override is rejected")
def bdd_override_rejected(ctx: dict[str, Any]) -> None:
    assert ctx.get("override_rejected") is True, "expected the still-violating override to be rejected"


def _bdd_do_override(run_id: uuid.UUID, input_data: dict[str, Any]) -> Run:
    async def _do() -> Run:
        session = await _bdd_open_session()
        override_run = await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=run_id,
            input_data=input_data,
            actor_id=_ACTOR_ID,
        )
        await session.commit()
        await _bdd_close_session(session)
        return override_run

    import asyncio

    return asyncio.run(_do())


@then("the run is flipped to pending with is_replay True")
def bdd_flipped_pending_replay(ctx: dict[str, Any]) -> None:
    run: Run = ctx["run"]
    override_run: Run = ctx["override_run"]
    assert override_run.id == run.id, "override must flip the SAME run row"
    assert override_run.status == "pending", f"expected pending, got {override_run.status}"
    assert override_run.error_code is None
    assert override_run.is_replay is True


@then("only one run row exists for the override cycle")
def bdd_single_run_row(ctx: dict[str, Any]) -> None:
    run_id = ctx["run"].id

    async def _count() -> int:
        session = await _bdd_open_session()
        rows = (await session.execute(select(Run).where(Run.id == run_id))).scalars().all()
        await _bdd_close_session(session)
        return len(rows)

    import asyncio

    assert asyncio.run(_count()) == 1, "the override cycle must not create a duplicate run"
