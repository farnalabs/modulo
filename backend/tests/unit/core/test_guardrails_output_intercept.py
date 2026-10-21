"""Unit tests for FAR-209 Track B guardrail machinery.

Covers:
- the ``destroy`` redaction mode (deterministic keyed HMAC tokens, candidate
  generation, fail-closed mechanics),
- the ``interception_point`` seam vocabulary (engine + authoring schema),
- the output/input filter helpers' UNKNOWN-point semantics (D5),
- authoring fail-closed: an output-point guardrail MUST be node-bound
  (``create_or_update_eval`` rejects ``node_id IS NULL``),
- the executor's node-output seam (flags read, detection-driven block,
  fail-closed mechanism handling),
- the executor's node-output split shim (guardrails out of the eval loop,
  a guardrail-typed straggler never reaches the eval engine).
"""

from __future__ import annotations

import contextlib
import unittest.mock
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

import modulo.core.guardrails.redaction_mode as redaction_mode
import modulo.core.pipeline_engine.executor as executor_mod
from modulo.core.eval_engine import (
    EvalDefinition,
    EvalEngine,
    EvalType,
    GuardrailMisroutedError,
)
from modulo.core.eval_engine.eval_definition_write import create_or_update_eval
from modulo.core.guardrails import (
    GuardrailBlockedError,
    GuardrailConfig,
    GuardrailConfigError,
    apply_redaction_masks,
    run_interception_pass,
    run_interception_pass_async,
)
from modulo.core.guardrails.config import (
    GuardrailConfigItem,
    GuardrailConfigSet,
    config_item_from_engine_definition,
    to_eval_config,
    validate_config_set,
)
from modulo.core.guardrails.output_intercept import (
    exclude_output_interception_definitions,
    filter_output_interception_definitions,
    is_output_interception,
    read_output_interception_flags,
    run_output_interception_async,
)
from modulo.core.guardrails.redaction_mode import (
    DESTROY_TOKEN_PREFIX,
    GuardrailRedactionMechanismError,
    destroy_candidates,
    destroy_token,
    resolve_guardrail_destroy_pepper,
)
from modulo.core.pipeline_engine.eval_persist_order import run_evals_persist_before_decide

_ORG_ID = uuid.uuid4()


def _guardrail(
    *,
    name: str = "gr",
    action: str = "block",
    detection: dict[str, Any] | None = None,
    redaction: list[dict[str, Any]] | None = None,
    interception_point: str = "input",
) -> EvalDefinition:
    config: dict[str, Any] = {"action": action, "interception_point": interception_point}
    if redaction is not None:
        config["redaction"] = redaction
    if detection:
        config.update(detection)
    else:
        config.setdefault("type", "regex")
        config.setdefault("field", "body")
        config.setdefault("pattern", r"SECRET_[A-Z0-9]{8}")
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour="block",
    )


# ---------------------------------------------------------------------------
# destroy token mechanics (keyed HMAC, deterministic)
# ---------------------------------------------------------------------------


def test_destroy_token_is_deterministic_and_keyed():
    first = destroy_token("SECRET_ABC12345", "pepper")
    second = destroy_token("SECRET_ABC12345", "pepper")
    assert first == second
    assert first.startswith(DESTROY_TOKEN_PREFIX)
    # A different pepper (rotation) yields a different token.
    assert destroy_token("SECRET_ABC12345", "other-pepper") != first
    # Different candidates never collide.
    assert destroy_token("SECRET_ABC12345", "p") != destroy_token("SECRET_AAA23456", "p")
    # The token never embeds the candidate text.
    assert "SECRET" not in first


def test_destroy_candidates_pattern_matches_generate_per_match_candidates():
    value = "plain SECRET_ABC12345 and SECRET_XYZ98776"
    candidates = destroy_candidates(value, r"SECRET_[A-Z0-9]{8}")
    assert candidates == ["SECRET_ABC12345", "SECRET_XYZ98776"]


def test_destroy_candidates_no_match_falls_back_to_whole_value():
    assert destroy_candidates("untouched body", r"SECRET_[A-Z0-9]{8}") == ["untouched body"]


def test_destroy_candidates_no_pattern_falls_back_to_whole_value():
    assert destroy_candidates("whole leaf", None) == ["whole leaf"]


def test_destroy_candidates_non_string_leaf_is_compact_json():
    candidates = destroy_candidates({"k": ["v", 2]}, None)
    assert candidates == ['{"k": ["v", 2]}']


def test_destroy_redaction_replaces_only_matching_spans_leaving_surroundings():
    pattern = r"SECRET_[A-Z0-9]{8}"
    payload = {"body": "before SECRET_ABC12345 after"}
    with unittest.mock.patch(
        "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
        return_value="pepper",
    ):
        redacted, entries = apply_redaction_masks(
            payload,
            [{"path": "body", "mode": "destroy"}],
            raise_on_block=True,
            guardrail_name="gr",
            detection_pattern=pattern,
        )
    body = redacted["body"]
    assert body.startswith("before ")
    assert body.endswith(" after")
    assert "SECRET_ABC12345" not in body
    assert body[len("before ") : -len(" after")].startswith(DESTROY_TOKEN_PREFIX)
    assert len(entries) == 1
    assert entries[0].reason == "destroyed"


def test_destroy_redaction_without_pepper_fails_closed():
    payload = {"body": "vital SECRET_ABC12345"}
    with (
        unittest.mock.patch(
            "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
            return_value="",
        ),
        pytest.raises(GuardrailRedactionMechanismError),
    ):
        apply_redaction_masks(
            payload,
            [{"path": "body", "mode": "destroy"}],
            raise_on_block=True,
            guardrail_name="gr",
            detection_pattern=r"SECRET_[A-Z0-9]{8}",
        )


def test_destroy_redaction_non_string_leaf_becomes_one_token():
    with unittest.mock.patch(
        "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
        return_value="pepper",
    ):
        redacted, entries = apply_redaction_masks(
            {"nested": {"blob": {"secret": "value"}}},
            [{"path": "nested.blob", "mode": "destroy"}],
            raise_on_block=True,
            guardrail_name="gr",
        )
    assert redacted["nested"]["blob"].startswith(DESTROY_TOKEN_PREFIX)
    assert len(entries) == 1


def test_destroy_redaction_allowlisted_system_field_never_destroyed():
    """The never-touch allowlist also governs destroy mode (same static-path
    resolver, same refusal — no overlap with the never-touch set)."""
    redacted, entries = apply_redaction_masks(
        {"run_id": "keep me"},
        [{"path": "run_id", "mode": "destroy"}],
        raise_on_block=True,
        guardrail_name="gr",
        detection_pattern=r".*",
    )
    assert redacted == {"run_id": "keep me"}
    assert entries[0].applied is False
    assert entries[0].reason == "allowlist"


async def test_destroy_mechanism_error_through_async_pass_surfaces_fail_closed() -> None:
    """End-to-end at the seam contract: a destroy redaction without a pepper
    (unset deployment setting) surfaces as blocked=True + a mechanism-error
    record — the caller terminalizes, never an unredacted persist."""
    guardrail_defs = [
        _guardrail(
            name="gr-destroy",
            action="redact",
            detection={
                "type": "regex",
                "pattern": r"SECRET_[A-Z0-9]{8}",
                "field": "body",
            },
            redaction=[{"path": "body", "mode": "destroy"}],
            interception_point="output",
        )
    ]
    envelope = {"body": "free SECRET_ABC12345"}
    with (
        unittest.mock.patch(
            "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
            return_value="",
        ),
        pytest.raises(GuardrailRedactionMechanismError),
    ):
        await run_interception_pass_async(EvalEngine(), guardrail_defs, envelope)


def test_destroy_mode_in_live_pass_with_pepper_destroys_and_does_not_block():
    guardrail_defs = [
        _guardrail(
            name="gr-destroy",
            action="redact",
            detection={
                "type": "regex",
                "pattern": r"SECRET_[A-Z0-9]{8}",
                "field": "body",
            },
            redaction=[{"path": "body", "mode": "destroy"}],
            interception_point="output",
        )
    ]
    envelope = {"body": "free SECRET_ABC12345"}
    with unittest.mock.patch(
        "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
        return_value="pepper",
    ):
        outcome = run_interception_pass(EvalEngine(), guardrail_defs, envelope)
    assert outcome.blocked is False
    assert "SECRET_ABC12345" not in outcome.payload["body"]
    assert outcome.redactions[0].mode == "destroy"


def test_guardrail_misrouted_error_still_raised_for_eval_engine():
    """Guardrails keep never entering the eval engine unchanged (T1)."""
    bad = _guardrail(interception_point="output")
    with pytest.raises(GuardrailMisroutedError):
        EvalEngine().evaluate({}, bad)


# ---------------------------------------------------------------------------
# interception_point vocabulary (engine DTO + config schema)
# ---------------------------------------------------------------------------


def test_engine_guardrail_config_rejects_unknown_interception_point_fail_closed():
    config: dict[str, Any] = {
        "action": "block",
        "interception_point": "midstream",
        "type": "regex",
    }
    with pytest.raises(ValidationError):
        GuardrailConfig.model_validate(config)


def test_config_item_rebuild_rejects_unknown_interception_point():

    engine_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="gr-splunk",
        eval_type=EvalType.GUARDRAIL,
        config={"action": "block", "interception_point": "midstream", "type": "regex"},
        failure_behaviour="block",
    )
    with pytest.raises(GuardrailConfigError):
        config_item_from_engine_definition(engine_def)


def test_engine_guardrail_config_accepts_output_interception_point():
    config = GuardrailConfig.model_validate({"action": "block", "interception_point": "output", "type": "regex"})
    assert config.interception_point == "output"


def test_output_interception_filter_semantics_unknown_point_is_not_output():
    output_def = _guardrail(interception_point="output")
    input_def = _guardrail(name="gr-in", interception_point="input")
    unknown_def = _guardrail(name="gr-unknown", interception_point="midstream")

    assert is_output_interception(output_def) is True
    assert is_output_interception(input_def) is False
    assert is_output_interception(unknown_def) is False

    assert filter_output_interception_definitions([output_def, unknown_def]) == [output_def]
    assert not filter_output_interception_definitions([input_def, unknown_def])

    # D5: an UNKNOWN point value KEEPS the input pass (strict validation there
    # fails closed) — it is never silently unbound from both passes.
    kept = exclude_output_interception_definitions([input_def, unknown_def])
    assert kept == [input_def, unknown_def]


def test_to_eval_config_writes_interception_point():
    item = GuardrailConfigItem.model_validate(
        {
            "id": "gr-splunk",
            "name": "gr",
            "detection": {"type": "regex", "pattern": "SECRET_", "field": "body"},
            "action": "redact",
            "redaction": [{"path": "body", "mode": "destroy"}],
            "interception_point": "output",
        }
    )
    config = to_eval_config(item)
    assert config["interception_point"] == "output"
    assert config["redaction"] == [{"path": "body", "mode": "destroy"}]


def test_validate_config_set_destroy_requires_configured_pepper_fail_closed():
    item = GuardrailConfigItem.model_validate(
        {
            "id": "gr-splunk",
            "name": "gr",
            "detection": {"type": "regex", "pattern": "SECRET_", "field": "body"},
            "action": "redact",
            "redaction": [{"path": "body", "mode": "destroy"}],
            "interception_point": "output",
        }
    )
    config_set = GuardrailConfigSet.model_validate({"version": 1, "guardrails": [item.model_dump()]})
    with (
        unittest.mock.patch(
            "modulo.core.guardrails.config.resolve_guardrail_destroy_pepper",
            return_value="",
        ),
        pytest.raises(GuardrailConfigError),
    ):
        validate_config_set(config_set)
    # With the pepper configured the SAME authoring succeeds.
    with unittest.mock.patch(
        "modulo.core.guardrails.config.resolve_guardrail_destroy_pepper",
        return_value="pepper",
    ):
        validate_config_set(config_set)


# ---------------------------------------------------------------------------
# Output filter: is_output semantics are strict (unknown treated as input)
# ---------------------------------------------------------------------------


def test_output_filter_unknown_value_stays_on_input_pass():
    unknown = _guardrail(name="gr-unknown", interception_point="midstream")
    assert exclude_output_interception_definitions([unknown]) == [unknown]
    assert not filter_output_interception_definitions([unknown])


# ---------------------------------------------------------------------------
# Authoring (FAR-209 Track B): an output-point guardrail MUST be node-bound
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Create-path AsyncSession stand-in: records adds/flushes, never reads.

    The guard under test must fire BEFORE any persistence, so ``execute``
    (the UPDATE-path read) failing loudly proves nothing touched the DB.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushed = 0
        self.executed = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.executed += 1
        raise AssertionError("the create path must not read the DB before the guard fires")


def _output_point_config() -> dict[str, Any]:
    return {"action": "block", "interception_point": "output", "type": "regex"}


async def test_create_or_update_eval_rejects_unbound_output_point_guardrail():
    """A pipeline-level (``node_id IS NULL``) output-point guardrail is enforced
    by NO seam — the input pass excludes it and the output seam never loads an
    unbound row. Reject at authoring (422) instead of persisting a
    silently-unenforced block/redact guardrail."""
    session = _RecordingSession()
    with pytest.raises(HTTPException) as excinfo:
        await create_or_update_eval(
            session,
            org_id=_ORG_ID,
            account_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            node_id=None,
            name="gr-unbound-out",
            eval_type="guardrail",
            config_json=_output_point_config(),
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
        )
    assert excinfo.value.status_code == 422
    assert "node binding" in str(excinfo.value.detail)
    # Fail CLOSED before any write: nothing was added or flushed.
    assert not session.added
    assert session.flushed == 0
    assert session.executed == 0


async def test_create_or_update_eval_allows_node_bound_output_point_guardrail():
    """The SAME config persists when bound to a node — the guard is about the
    missing binding, never about the output point itself."""
    session = _RecordingSession()
    node_id = uuid.uuid4()
    row = await create_or_update_eval(
        session,
        org_id=_ORG_ID,
        account_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        node_id=node_id,
        name="gr-bound-out",
        eval_type="guardrail",
        config_json=_output_point_config(),
        failure_behaviour="warn",
        pass_threshold=None,
        suite_id=None,
    )
    assert row.node_id == node_id
    assert len(session.added) == 1
    assert session.flushed == 1


async def test_create_or_update_eval_allows_unbound_input_point_guardrail():
    """The pipeline-level INPUT path (config-as-code's shape) is untouched — an
    unbound INPUT-point guardrail still persists, because the ingestion edge
    loads pipeline-level rows and enforces it there."""
    session = _RecordingSession()
    row = await create_or_update_eval(
        session,
        org_id=_ORG_ID,
        account_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        node_id=None,
        name="gr-unbound-in",
        eval_type="guardrail",
        config_json={"action": "block", "interception_point": "input", "type": "regex"},
        failure_behaviour="warn",
        pass_threshold=None,
        suite_id=None,
    )
    assert row.node_id is None
    assert len(session.added) == 1


async def test_create_or_update_eval_allows_unbound_guardrail_without_a_point():
    """No ``interception_point`` key = the input default — unbound stays legal."""
    session = _RecordingSession()
    row = await create_or_update_eval(
        session,
        org_id=_ORG_ID,
        account_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        node_id=None,
        name="gr-unbound-default",
        eval_type="guardrail",
        config_json={"action": "observe", "type": "regex"},
        failure_behaviour="warn",
        pass_threshold=None,
        suite_id=None,
    )
    assert row.node_id is None
    assert len(session.added) == 1


# ---------------------------------------------------------------------------
# Executor: guardrail split at the node-output seam
# ---------------------------------------------------------------------------


class _RecordingHarness:
    """Holds the call records + executor for the chain-end split tests."""

    def __init__(self) -> None:
        self.guard_calls: list[dict[str, Any]] = []
        self.post_eval_calls: list[list] = []
        self.executor = executor_mod.PipelineExecutor(unittest.mock.MagicMock())

    async def _guard(self, **kwargs: Any) -> dict[str, Any]:
        self.guard_calls.append(kwargs)
        return kwargs["envelope"]

    async def _post_evals(self, *args: Any, **kwargs: Any) -> None:
        defs_by_node = args[2] if len(args) > 2 else kwargs.get("eval_definitions_by_node", {})
        self.post_eval_calls.append(list(defs_by_node.get(args[0]) or []))

    def _wire(self, monkeypatch: pytest.MonkeyPatch, ctx: executor_mod._StreamContext) -> None:
        monkeypatch.setattr(self.executor, "_run_output_guardrails", self._guard)
        monkeypatch.setattr(self.executor, "_run_post_node_evals", self._post_evals)
        monkeypatch.setattr(
            executor_mod,
            "_record_chain_end_output",
            unittest.mock.MagicMock(return_value=ctx.completed_node_outputs["n1"]),
        )
        monkeypatch.setattr(
            executor_mod,
            "_record_node_markers",
            unittest.mock.MagicMock(return_value=(None, None, None)),
        )


def _stream_ctx(defs: list[EvalDefinition]) -> executor_mod._StreamContext:
    envelope: dict[str, Any] = {"body": "plain text", "summary": "ok"}
    return executor_mod._StreamContext(
        node_ids={"n1"},
        guard=None,
        completed_node_outputs={"n1": envelope},
        run_trace_id="trace",
        broker=unittest.mock.MagicMock(),
        run_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        org_id=_ORG_ID,
        node_token_budgets=None,
        eval_definitions_by_node={"n1": defs},
        node_type_map=None,
    )


def _event(name: str) -> dict[str, Any]:
    return {"name": name, "data": {"output": {"status": "complete"}}}


async def test_chain_end_splits_guardrail_typed_defs_from_node_evals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An output-point guardrail bound to a node intercepts that node's
    output and is EXCLUDED from the eval loop; a non-guardrail eval still
    runs through _run_post_node_evals (never a GuardrailMisroutedError
    crash); an input-point guardrail never reaches the output seam."""
    envelope = {"body": "plain text", "summary": "ok"}
    output_def = _guardrail(name="gr-out", action="block", interception_point="output")
    input_def = _guardrail(name="gr-in", action="block", interception_point="input")
    non_guardrail = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="eval-check",
        eval_type=EvalType.REGEX,
        config={"type": "regex"},
        failure_behaviour="warn",
    )
    harness = _RecordingHarness()
    ctx = _stream_ctx([output_def, input_def, non_guardrail])
    ctx.completed_node_outputs = {"n1": envelope}
    harness._wire(monkeypatch, ctx)
    state = executor_mod._StreamState()

    await harness.executor._handle_chain_end_event(state=state, ctx=ctx, lg_event=_event("n1"))

    assert len(harness.guard_calls) == 1
    assert harness.guard_calls[0]["guardrail_defs"] == [output_def]
    assert harness.guard_calls[0]["org_id"] == _ORG_ID
    assert len(harness.post_eval_calls) == 1
    post_names = [d.name for d in harness.post_eval_calls[0]]
    # EvalDefinition is an identity-equality DTO — assert by name.
    assert post_names == ["gr-in", "eval-check"]


async def test_chain_end_redaction_overwrites_completed_node_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REDACTED envelope is what completed_node_outputs records — the
    unredacted original is never left behind for downstream state."""
    envelope = {"body": "plain text"}
    redacted_envelope = {"body": "redacted"}
    output_def = _guardrail(
        name="gr-destroy",
        action="redact",
        redaction=[{"path": "body", "mode": "destroy"}],
        interception_point="output",
    )
    harness = _RecordingHarness()

    async def _guard(**kwargs: Any) -> dict[str, Any]:
        harness.guard_calls.append(kwargs)
        return redacted_envelope

    monkeypatch.setattr(harness.executor, "_run_output_guardrails", _guard)
    ctx = _stream_ctx([output_def])
    ctx.completed_node_outputs = {"n1": envelope}
    monkeypatch.setattr(
        executor_mod,
        "_record_chain_end_output",
        unittest.mock.MagicMock(return_value=envelope),
    )
    monkeypatch.setattr(
        executor_mod,
        "_record_node_markers",
        unittest.mock.MagicMock(return_value=(None, None, None)),
    )
    state = executor_mod._StreamState()

    await harness.executor._handle_chain_end_event(state=state, ctx=ctx, lg_event=_event("n1"))

    assert ctx.completed_node_outputs["n1"] is redacted_envelope


# ---------------------------------------------------------------------------
# Output pass: block raise + fail-closed mechanism handling (executor seam)
# ---------------------------------------------------------------------------


def _flags_session_factory(row: tuple[bool, bool] | None = (False, False)) -> Any:
    """Session factory whose flags read yields *row* (replay=False, kill=False).

    ``read_output_interception_flags`` drives detection-only mode for the
    output seam. A bare ``MagicMock()`` factory BREAKS that read — the error
    escapes as the fail-closed mechanism guard, so a test would pass even with
    a NON-violating envelope. Wiring a real row is what makes a block in these
    tests attributable to DETECTION.
    """
    session = unittest.mock.AsyncMock()
    result = unittest.mock.MagicMock()
    result.first = unittest.mock.MagicMock(return_value=row)
    session.execute = unittest.mock.AsyncMock(return_value=result)

    @contextlib.asynccontextmanager
    async def _factory() -> Any:
        yield session

    return _factory


def _raising_flags_session_factory(exc: BaseException) -> Any:
    """Session factory whose construction raises *exc* (a broken flags read)."""

    def _factory() -> Any:
        raise exc

    return _factory


def _block_output_guardrail() -> EvalDefinition:
    return _guardrail(
        name="gr-block",
        action="block",
        detection={
            "type": "regex",
            "pattern": r"SECRET_[A-Z0-9]{8}",
            "field": "body",
        },
        interception_point="output",
    )


def _output_executor(flags_factory: Any) -> executor_mod.PipelineExecutor:
    executor = executor_mod.PipelineExecutor(unittest.mock.MagicMock())
    executor._session_factory = flags_factory
    return executor


async def test_run_output_guardrails_block_raise():
    """A VIOLATING envelope blocks, and the block comes from DETECTION.

    The session factory returns ``(is_replay=False, kill_switch=False)`` so
    the flags read succeeds and enforcement is active. The assertions pin the
    attribution: a detection block names the GUARDRAIL, the fail-closed
    mechanism guard names the NODE.
    """
    executor = _output_executor(_flags_session_factory())
    with pytest.raises(GuardrailBlockedError) as excinfo:
        await executor._run_output_guardrails(
            node_id="n1",
            envelope={"body": "SECRET_ABC12345"},
            guardrail_defs=[_block_output_guardrail()],
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
        )
    assert excinfo.value.eval_name == "gr-block"
    assert "mechanism error" not in excinfo.value.detail


async def test_run_output_guardrails_non_violating_envelope_is_not_blocked():
    """Same wiring, a clean envelope — NO block.

    This is the discriminating half of the test above: with the old bare
    ``MagicMock()`` session factory the flags read itself failed and the
    seam's fail-closed guard raised on ANY envelope, so a green block test
    proved nothing about detection.
    """
    executor = _output_executor(_flags_session_factory())
    envelope = {"body": "harmless"}
    result = await executor._run_output_guardrails(
        node_id="n1",
        envelope=envelope,
        guardrail_defs=[_block_output_guardrail()],
        run_id=uuid.uuid4(),
        org_id=_ORG_ID,
    )
    assert result == envelope


async def test_run_output_guardrails_flags_read_failure_keeps_enforcement():
    """A ``SQLAlchemyError`` on the flags read fails open to ``(False, False)``
    — enforcement stays ACTIVE. The unavailable kill-switch/replay downgrade
    must never be assumed ON (that would be a fail-open on a data-safety
    control)."""
    executor = _output_executor(_raising_flags_session_factory(SQLAlchemyError("flags read unavailable")))
    with pytest.raises(GuardrailBlockedError) as excinfo:
        await executor._run_output_guardrails(
            node_id="n1",
            envelope={"body": "SECRET_ABC12345"},
            guardrail_defs=[_block_output_guardrail()],
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
        )
    assert excinfo.value.eval_name == "gr-block"


async def test_run_output_guardrails_broken_flags_read_fails_closed():
    """A NON-SQLAlchemy flags-read failure escapes the read, and the seam's
    defect-path guard fail-closes: a blocking guardrail is bound, so the
    envelope must never pass through unredacted — even a clean one."""
    executor = _output_executor(_raising_flags_session_factory(RuntimeError("flags read exploded")))
    with pytest.raises(GuardrailBlockedError) as excinfo:
        await executor._run_output_guardrails(
            node_id="n1",
            envelope={"body": "harmless"},
            guardrail_defs=[_block_output_guardrail()],
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
        )
    assert excinfo.value.eval_name == "n1"
    assert "mechanism error" in excinfo.value.detail


async def test_run_output_guardrails_mechanism_error_names_node_output_seam():
    """A detection mechanism error at this seam must be labelled THIS seam.

    The shared pass is driven by both seams; without the ``seam`` parameter a
    mechanism-error block here read "guardrail mechanism error at ingestion
    edge" — the FAR-209 review's misleading-detail finding. An unsupported
    detection type makes ``_validate_guardrail_definition`` raise
    ``GuardrailConfigError`` inside the detection loop (the mechanism path),
    deliberately distinct from the detection block asserted above.
    """
    malformed = _guardrail(
        name="gr-malformed",
        action="block",
        detection={"type": "py_eval"},
        interception_point="output",
    )
    executor = _output_executor(_flags_session_factory())
    with pytest.raises(GuardrailBlockedError) as excinfo:
        await executor._run_output_guardrails(
            node_id="n1",
            envelope={"body": "harmless"},
            guardrail_defs=[malformed],
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
        )
    assert excinfo.value.detail == "guardrail mechanism error at node-output edge"
    assert "ingestion edge" not in excinfo.value.detail


# ---------------------------------------------------------------------------
# Pepper resolution + destroy candidate error handling (redaction_mode)
# ---------------------------------------------------------------------------


def test_resolve_guardrail_destroy_pepper_explicit_argument_wins():
    """An explicit non-empty pepper short-circuits the settings read."""
    assert resolve_guardrail_destroy_pepper("explicit-pepper") == "explicit-pepper"


def test_resolve_guardrail_destroy_pepper_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch):
    """No explicit argument → the lazily-loaded settings value is used."""
    settings = unittest.mock.MagicMock()
    settings.guardrail_destroy_pepper = "settings-pepper"
    monkeypatch.setattr(redaction_mode, "get_settings", lambda: settings)
    assert resolve_guardrail_destroy_pepper() == "settings-pepper"


def test_destroy_candidates_unusable_pattern_fails_closed():
    """An invalid regex in a destroy policy's candidate pattern raises the
    mechanism error — the seam then fails closed, never a silent no-act."""
    with pytest.raises(GuardrailRedactionMechanismError):
        destroy_candidates("value", "(")


def test_destroy_redaction_duplicate_candidate_is_replaced_once():
    """Repeated candidates (same match text) are destroyed once and the
    duplicate is skipped — the per-candidate loop never re-replaces."""
    payload = {"body": "SECRET_ABC12345 SECRET_ABC12345"}
    with unittest.mock.patch(
        "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
        return_value="pepper",
    ):
        redacted, entries = apply_redaction_masks(
            payload,
            [{"path": "body", "mode": "destroy"}],
            raise_on_block=True,
            guardrail_name="gr",
            detection_pattern=r"SECRET_[A-Z0-9]{8}",
        )
    body = redacted["body"]
    assert "SECRET_ABC12345" not in body
    assert body.count(DESTROY_TOKEN_PREFIX) == 2
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Output seam flags + kill-switch downgrade (output_intercept)
# ---------------------------------------------------------------------------


async def test_read_output_interception_flags_no_row_defaults_enforcement():
    """A flags read that returns NO row (run/org not found) defaults to
    ``(False, False)`` — enforcement stays active, never a silent downgrade."""
    factory = _flags_session_factory(None)
    replay, kill = await read_output_interception_flags(factory, run_id=uuid.uuid4(), org_id=_ORG_ID)
    assert (replay, kill) == (False, False)


async def test_output_kill_switch_downgrades_pass_to_detection_only():
    """Kill switch ON → the pass is detection-only and never acts, so a
    would-block envelope passes through untouched."""
    envelope = {"body": "SECRET_ABC12345"}
    outcome = await run_output_interception_async(
        [_block_output_guardrail()],
        envelope,
        session_factory=_flags_session_factory((False, True)),
        run_id=uuid.uuid4(),
        org_id=_ORG_ID,
    )
    assert outcome.blocked is False
    assert outcome.payload == envelope


# ---------------------------------------------------------------------------
# Authoring: unknown interception_point fails closed (eval_definition_write)
# ---------------------------------------------------------------------------


async def test_create_or_update_eval_rejects_unknown_interception_point():
    """A value outside the input|output vocabulary is a 422 at authoring —
    never a silently-unenforced guardrail."""
    session = _RecordingSession()
    with pytest.raises(HTTPException) as excinfo:
        await create_or_update_eval(
            session,
            org_id=_ORG_ID,
            account_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            node_id=uuid.uuid4(),
            name="gr-bogus-point",
            eval_type="guardrail",
            config_json={"action": "block", "interception_point": "midstream", "type": "regex"},
            failure_behaviour="warn",
            pass_threshold=None,
            suite_id=None,
        )
    assert excinfo.value.status_code == 422
    assert "interception_point" in str(excinfo.value.detail)
    assert not session.added


# ---------------------------------------------------------------------------
# Eval persist loop: a guardrail-typed straggler is skipped (eval_persist_order)
# ---------------------------------------------------------------------------


async def test_run_evals_persist_before_decide_skips_guardrail_typed_def():
    """A guardrail-typed def reaching the shared eval loop is skipped loudly —
    it is interception-owned; the eval engine would crash on it. The sibling
    non-guardrail eval in the same batch is still computed."""
    normal = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="eval-ok",
        eval_type=EvalType.REGEX,
        config={"type": "regex", "pattern": "x", "field": "body"},
        failure_behaviour="warn",
    )
    results = await run_evals_persist_before_decide(
        eval_defs=[_guardrail(name="gr-straggler"), normal],
        resolve_eval_target=lambda _d: {"body": "x"},
        resolve_model_judge=lambda _d: None,
        node_id="n1",
    )
    assert set(results) == {"eval-ok"}


def test_validate_config_set_rejects_json_schema_without_schema_dict():
    """A ``json_schema`` detection with no schema dict fails the cross-field
    check at propose time (the DTO allows the omission; validation catches it)."""
    item = GuardrailConfigItem.model_validate(
        {
            "id": "gr-no-schema",
            "name": "gr",
            "detection": {"type": "json_schema"},
        }
    )
    config_set = GuardrailConfigSet.model_validate({"version": 1, "guardrails": [item.model_dump()]})
    with pytest.raises(GuardrailConfigError, match="schema"):
        validate_config_set(config_set)


def test_validate_config_set_rejects_whitespace_only_redaction_path():
    """A redaction path that is only whitespace passes the DTO's min_length but
    is empty after strip — validated out at propose time."""
    item = GuardrailConfigItem.model_validate(
        {
            "id": "gr-blank-path",
            "name": "gr",
            "detection": {"type": "regex", "pattern": "x", "field": "body"},
            "action": "redact",
            "redaction": [{"path": " ", "mode": "drop"}],
        }
    )
    config_set = GuardrailConfigSet.model_validate({"version": 1, "guardrails": [item.model_dump()]})
    with pytest.raises(GuardrailConfigError, match="empty redaction path"):
        validate_config_set(config_set)


# ---------------------------------------------------------------------------
# Executor node-output seam: no-org, destroy mechanism, fail-open, split arms
# ---------------------------------------------------------------------------


async def test_run_output_guardrails_without_org_returns_envelope_untouched():
    """No org context → the seam cannot read or persist, so it returns the
    envelope untouched (nothing to attribute the interception to)."""
    executor = _output_executor(_flags_session_factory())
    envelope = {"body": "SECRET_ABC12345"}
    result = await executor._run_output_guardrails(
        node_id="n1",
        envelope=envelope,
        guardrail_defs=[_block_output_guardrail()],
        run_id=uuid.uuid4(),
        org_id=None,
    )
    assert result == envelope


async def test_run_output_guardrails_destroy_mechanism_error_blocks():
    """A destroy policy with no pepper fails closed through the seam: the
    mechanism error becomes a terminal GuardrailBlockedError."""
    destroy_def = _guardrail(
        name="gr-destroy",
        action="redact",
        detection={"type": "regex", "pattern": r"SECRET_[A-Z0-9]{8}", "field": "body"},
        redaction=[{"path": "body", "mode": "destroy"}],
        interception_point="output",
    )
    executor = _output_executor(_flags_session_factory())
    with (
        unittest.mock.patch(
            "modulo.core.guardrails.resolve_guardrail_destroy_pepper",
            return_value="",
        ),
        pytest.raises(GuardrailBlockedError) as excinfo,
    ):
        await executor._run_output_guardrails(
            node_id="n1",
            envelope={"body": "SECRET_ABC12345"},
            guardrail_defs=[destroy_def],
            run_id=uuid.uuid4(),
            org_id=_ORG_ID,
        )
    assert "destroy redaction failed closed" in excinfo.value.detail


async def test_run_output_guardrails_non_guarding_pass_failure_fails_open():
    """An unexpected pass failure with ONLY observe/warn guardrails fails open
    (log + pass through) — there is no guarding action to fail closed for."""
    observe_def = _guardrail(name="gr-observe", action="observe", interception_point="output")
    executor = _output_executor(_raising_flags_session_factory(RuntimeError("flags read exploded")))
    envelope = {"body": "harmless"}
    result = await executor._run_output_guardrails(
        node_id="n1",
        envelope=envelope,
        guardrail_defs=[observe_def],
        run_id=uuid.uuid4(),
        org_id=_ORG_ID,
    )
    assert result == envelope


async def test_chain_end_without_output_guardrail_runs_eval_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node bound to no output-point guardrail skips the interception seam
    and still runs its non-guardrail evals through the eval path."""
    non_guardrail = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name="eval-check",
        eval_type=EvalType.REGEX,
        config={"type": "regex"},
        failure_behaviour="warn",
    )
    harness = _RecordingHarness()
    ctx = _stream_ctx([non_guardrail])
    ctx.completed_node_outputs = {"n1": {"body": "x"}}
    harness._wire(monkeypatch, ctx)
    state = executor_mod._StreamState()

    await harness.executor._handle_chain_end_event(state=state, ctx=ctx, lg_event=_event("n1"))

    assert not harness.guard_calls
    assert len(harness.post_eval_calls) == 1
    assert [d.name for d in harness.post_eval_calls[0]] == ["eval-check"]


async def test_chain_end_redaction_without_completed_outputs_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``completed_node_outputs`` is absent (None) the redacted envelope
    is still returned — the write-back is skipped, never a crash."""
    envelope = {"body": "plain text"}
    redacted_envelope = {"body": "redacted"}
    output_def = _guardrail(name="gr-out", action="block", interception_point="output")

    async def _guard(**kwargs: Any) -> dict[str, Any]:
        return redacted_envelope

    harness = _RecordingHarness()
    monkeypatch.setattr(harness.executor, "_run_output_guardrails", _guard)
    ctx = _stream_ctx([output_def])
    ctx.completed_node_outputs = None
    monkeypatch.setattr(
        executor_mod,
        "_record_chain_end_output",
        unittest.mock.MagicMock(return_value=envelope),
    )
    monkeypatch.setattr(
        executor_mod,
        "_record_node_markers",
        unittest.mock.MagicMock(return_value=(None, None, None)),
    )
    state = executor_mod._StreamState()

    await harness.executor._handle_chain_end_event(state=state, ctx=ctx, lg_event=_event("n1"))

    # No crash: the write-back was simply skipped.
    assert ctx.completed_node_outputs is None
