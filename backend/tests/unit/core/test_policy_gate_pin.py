"""Unit tests for FAR-967 chunk 10: policy-gate pin fingerprint + operator control.

Covers the acceptance criteria that do not need a live Postgres:

* §3.3 fingerprint determinism (C2, C7, C7a) + corrupt-set coverage (F5)
* §3.4 run-start verification three-case table (C4 shape, C5, C6, C15)
* §5 operator control — SINGLE-SOURCED at the executor build (F8): the
  run-start interception seam performs NO disabled-gate filtering; C8/C9
  behaviour lives in ``test_policy_gate_eval_wiring.py``
* F1 pin-mismatch override refusal (``guardrail_override``)
* §3.1/3.2 pin construction + snapshot persistence wiring (C1, C3, C14)
* schema-contract assertions (C11, C12, model-DDL level; F4 server default)

Migration round-trip + live-DB CHECK behaviour (C13) live in
``backend/tests/integration/test_policy_gate_pin_migration.py``.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.schema import CreateTable

from modulo.core.eval_engine.policy_gate import fingerprint_policy_gate_pins
from modulo.db.crud.pipeline_snapshot import (
    _build_policy_gate_pins,
    _load_policy_gate_rows_for_pipeline,
)
from modulo.db.crud.run import (
    POLICY_GATE_PIN_MISMATCH_MARKER,
    _stamp_guardrail_blocked_run,
    _verify_policy_gate_pin_fingerprint,
)
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.policy_gate import PolicyGate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _row_result(value: object) -> MagicMock:
    """A MagicMock whose ``one_or_none()`` and ``all()`` yield ``value``."""
    result = MagicMock()
    result.one_or_none.return_value = value
    result.all.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    scalars = MagicMock()
    scalars.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _gate_pin(action: str = "block") -> dict[str, str]:
    return {
        "policy_gate_id": str(uuid.uuid4()),
        "eval_id": str(uuid.uuid4()),
        "action": action,
        "node_id": str(uuid.uuid4()),
    }


def _pins(count: int = 2) -> list[dict[str, str]]:
    return [_gate_pin() for _ in range(count)]


_AUTO_SNAPSHOT = object()


def _interception_request(
    *,
    is_replay: bool = True,
    snapshot_id: Any = _AUTO_SNAPSHOT,
) -> Any:
    """Build an ``_InterceptionRequest``.

    ``snapshot_id`` defaults to a freshly minted UUID (a run normally carries
    one).  Passing ``None`` EXPLICITLY builds a snapshot-less request — the
    sentinel keeps ``None`` from being mistaken for "unspecified".
    """
    from modulo.db.crud.run import _InterceptionRequest

    resolved = uuid.uuid4() if snapshot_id is _AUTO_SNAPSHOT else snapshot_id
    return _InterceptionRequest(
        org_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        payload={"input": "x"},
        is_replay=is_replay,
        snapshot_id=resolved,
        guardrails_kill_switch=False,
    )


def _policy_gate_row(
    gate_id: uuid.UUID,
    *,
    node_id: uuid.UUID | None = None,
    action: str = "block",
) -> MagicMock:
    row = MagicMock()
    row.id = gate_id
    row.eval_id = uuid.uuid4()
    row.node_id = node_id if node_id is not None else uuid.uuid4()
    row.action = action
    return row


def _stubbed_guardrail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the guardrail pass so the policy-gate branch is the only
    live logic under test in ``_intercept_guardrails`` runs."""
    import modulo.db.crud.guardrail_config as guardrail_config

    monkeypatch.setattr(
        guardrail_config,
        "load_pipeline_guardrail_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "modulo.db.crud.run._resolve_pinned_guardrail_state",
        AsyncMock(
            return_value=MagicMock(
                pinned_defs=[],
                skipped_guardrails=[],
                blocked=False,
                block_message="",
            )
        ),
    )


# ---------------------------------------------------------------------------
# §3.3 — fingerprint_policy_gate_pins determinism (C2, C7, C7a)
# ---------------------------------------------------------------------------


def test_fingerprint_none_pins_returns_none() -> None:
    """``None`` (absent pins — legacy snapshot) fingerprints to ``None``."""
    assert fingerprint_policy_gate_pins(None) is None


def test_fingerprint_empty_pins_produce_deterministic_digest() -> None:
    """An EMPTY pin list is a deliberate state: it must produce a stable
    64-hex digest — never ``None``, never a per-call arbitrary value."""
    digest = fingerprint_policy_gate_pins([])
    assert digest is not None
    assert fingerprint_policy_gate_pins([]) == digest
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_fingerprint_empty_differs_from_absent() -> None:
    """C7: empty ≠ absent. The ``[]`` digest must never collide with the
    ``None`` sentinel of a legacy absent pin set."""
    assert fingerprint_policy_gate_pins([]) != fingerprint_policy_gate_pins(None)


def test_fingerprint_is_order_and_key_order_insensitive() -> None:
    """Re-ordering entries or reserialising dict keys must NOT change the
    digest — the canonical form is sorted, so iteration or persistence order
    is irrelevant."""
    pin_a = _gate_pin()
    pin_b = _gate_pin()
    base = fingerprint_policy_gate_pins([pin_a, pin_b])
    assert fingerprint_policy_gate_pins([pin_b, pin_a]) == base
    reordered_keys = [dict(reversed(list(pin.items()))) for pin in (pin_b, pin_a)]
    assert fingerprint_policy_gate_pins(reordered_keys) == base


def test_fingerprint_changes_when_pin_content_changes() -> None:
    """Any content change (an added, removed, or edited gate) must flip the
    digest — otherwise the run-start verification detects nothing."""
    pins = _pins(2)
    base = fingerprint_policy_gate_pins(pins)
    assert fingerprint_policy_gate_pins(pins[:1]) != base
    assert fingerprint_policy_gate_pins([*pins, _gate_pin()]) != base
    drifted = [dict(pins[0], action="warn"), pins[1]]
    assert fingerprint_policy_gate_pins(drifted) != base


def test_fingerprint_covers_non_dict_entries_verbatim() -> None:
    """F5 load-bearing: EVERY list element enters the digest — dict or not.
    The pre-F5 behaviour filtered non-dict entries out, so appending a junk
    element left the digest unchanged (corruption invisible at run start)
    and the junk later crashed per-entry consumers."""
    pins = _pins(2)
    base = fingerprint_policy_gate_pins(pins)
    assert fingerprint_policy_gate_pins([*pins, "junk-entry"]) != base
    assert fingerprint_policy_gate_pins([*pins, 42]) != base
    assert fingerprint_policy_gate_pins([*pins, {"not": "a pin"}]) != base
    # A pin REPLACED by junk also flips the digest (nothing is invisible).
    replaced = [*pins[:1], "junk-entry", pins[1]]
    assert fingerprint_policy_gate_pins(replaced) != base


def test_fingerprint_corrupt_top_level_value_fails_closed_deterministically() -> None:
    """F5: a corrupt top-level pin value (string / dict instead of a list)
    yields a deterministic sentinel digest that can never equal a
    list-computed stored fingerprint — fail closed, never raise out of the
    run-start seam."""
    sentinel = fingerprint_policy_gate_pins("not-a-list")  # type: ignore[arg-type]
    assert sentinel is not None
    # Deterministic: the same corrupt input always digests the same way.
    assert sentinel == fingerprint_policy_gate_pins("not-a-list")  # type: ignore[arg-type]
    assert sentinel == fingerprint_policy_gate_pins({"corrupt": True})  # type: ignore[arg-type]
    # But never equal to any list-computed digest.
    assert sentinel != fingerprint_policy_gate_pins(_pins(2))
    assert sentinel != fingerprint_policy_gate_pins([])
    # None still means "absent" (legacy), never the corrupt sentinel.
    assert fingerprint_policy_gate_pins(None) is None


# ---------------------------------------------------------------------------
# §3.4 — _verify_policy_gate_pin_fingerprint three-case table
# (C4 shape, C5, C6, C7a, C15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_case_i_no_pins_no_fingerprint_falls_back_to_live() -> None:
    """C6: a legacy snapshot with NO pins + NO fingerprint does not block —
    the replay proceeds against the pipeline's CURRENT live gates."""
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=None,
        saved_fingerprint=None,
    )
    assert not blocked
    assert message == ""


@pytest.mark.asyncio
async def test_verify_case_ii_pins_without_fingerprint_proceed_unverified() -> None:
    """C7a: pins present but NO fingerprint (the middle legacy state) — the
    run must proceed WITHOUT verification. Verifying anyway would spuriously
    block every snapshot created before the fingerprint migration."""
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=_pins(),
        saved_fingerprint=None,
    )
    assert not blocked
    assert message == ""


@pytest.mark.asyncio
async def test_verify_matching_fingerprint_trusts_the_pins() -> None:
    """C5: pins + matching fingerprint → verified and trusted, no block."""
    pins = _pins(3)
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=pins,
        saved_fingerprint=fingerprint_policy_gate_pins(pins),
    )
    assert not blocked
    assert message == ""


def _assert_mismatch_message(
    block_message: str,
    snapshot_id: uuid.UUID,
    stored: str,
    recomputed: str,
) -> None:
    assert "fingerprint mismatch" in block_message
    assert f"snapshot {snapshot_id}" in block_message, block_message
    assert stored[:12] in block_message
    assert recomputed[:12] in block_message
    assert stored not in block_message, "user-facing message must carry TRUNCATED digests only"
    assert recomputed not in block_message, "user-facing message must carry TRUNCATED digests only"


@pytest.mark.asyncio
async def test_verify_added_gate_fails_closed_with_truncated_actionable_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C4: a gate added to the pins after creation digests differently →
    fail CLOSED. The message names the snapshot, the truncated stored +
    recomputed digests, and the remediation; the LOG extras carry the FULL
    digests."""
    import logging

    pins = _pins(2)
    tampered = [*pins, _gate_pin()]
    stored = fingerprint_policy_gate_pins(pins)
    recomputed = fingerprint_policy_gate_pins(tampered)
    snapshot_id = uuid.uuid4()

    with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
        blocked, message = await _verify_policy_gate_pin_fingerprint(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            snapshot_id=snapshot_id,
            snap_pins=tampered,
            saved_fingerprint=stored,
        )

    assert blocked
    _assert_mismatch_message(message, snapshot_id, stored, recomputed)
    assert "create a new run" in message
    mismatch_records = [r for r in caplog.records if r.getMessage() == "policy_gates.pin_fingerprint_mismatch"]
    assert mismatch_records
    record = mismatch_records[-1]
    assert record.__dict__["stored_fingerprint"] == stored
    assert record.__dict__["recomputed_fingerprint"] == recomputed
    assert record.__dict__["snapshot_id"] == str(snapshot_id)


@pytest.mark.asyncio
async def test_verify_removed_gate_fails_closed() -> None:
    """A REMOVED pin is also a digest breaker — the set is immutable after
    creation, so the mismatch must fail closed in that direction too."""
    pins = _pins(3)
    snapshot_id = uuid.uuid4()
    stored = fingerprint_policy_gate_pins(pins)
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=snapshot_id,
        snap_pins=pins[:2],
        saved_fingerprint=stored,
    )
    assert blocked
    assert "fingerprint mismatch" in message
    assert f"snapshot {snapshot_id}" in message


@pytest.mark.asyncio
async def test_verify_mismatch_message_directs_at_a_new_run_only() -> None:
    """C15: the mismatch is NON-RETRYABLE — the remediation is creating a
    NEW run; retrying the same snapshot cannot reconcile pins and digest."""
    pins = _pins(2)
    blocked, message = await _verify_policy_gate_pin_fingerprint(
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        snap_pins=[*pins, _gate_pin()],
        saved_fingerprint=fingerprint_policy_gate_pins(pins),
    )
    assert blocked
    assert message.endswith("create a new run)")


def test_stamp_guardrail_blocked_run_marks_terminal_eval_failed() -> None:
    """C15 run-side: an interception-blocked run — including the policy-gate
    fingerprint block — is stamped TERMINAL (eval_failed), never dispatched
    to the executor, never auto-retried."""
    run = MagicMock()
    message = (
        "policy gate mechanism error: snapshot policy-gate pin fingerprint mismatch (remediation: create a new run)"
    )
    _stamp_guardrail_blocked_run(run, message)
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert run.error_detail == message
    assert run.completed_at is not None


# ---------------------------------------------------------------------------
# F1 — guardrail_override refuses pin-mismatch runs (mechanism failure)
# ---------------------------------------------------------------------------


def _override_run(*, error_detail: str) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.pipeline_id = uuid.uuid4()
    run.status = "eval_failed"
    run.error_code = "eval_blocked"
    run.error_detail = error_detail
    return run


def _override_session(run: MagicMock) -> AsyncMock:
    """Scripted session for ``guardrail_override``'s reads: get_run, the
    Pipeline FOR UPDATE lock, get_run again."""
    session = AsyncMock(spec=AsyncSession)
    run_result = _scalar_result(run)
    session.execute = AsyncMock(side_effect=[run_result, MagicMock(), run_result])
    return session


@pytest.mark.asyncio
async def test_guardrail_override_refuses_pin_mismatch_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1(b) load-bearing: a run blocked by a pin fingerprint mismatch is a
    MECHANISM failure — no operator-supplied input can remediate it, so the
    override refuses (base ``GuardrailOverrideError`` → 409) BEFORE the
    guardrail re-run pass is even attempted."""
    from modulo.core.pipeline_engine.recovery import (
        GuardrailOverrideError,
        guardrail_override,
    )

    mismatch_detail = (
        f"policy gate mechanism error: {POLICY_GATE_PIN_MISMATCH_MARKER} (… remediation: create a new run)"
    )
    run = _override_run(error_detail=mismatch_detail)
    session = _override_session(run)
    load_rows = AsyncMock(return_value=[])
    monkeypatch.setattr("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows", load_rows)

    with pytest.raises(GuardrailOverrideError) as exc_info:
        await guardrail_override(
            session,
            org_id=uuid.uuid4(),
            run_id=run.id,
            input_data={"input": "fixed"},
        )

    assert type(exc_info.value) is GuardrailOverrideError, "the refusal must be the base error (route maps it to 409)"
    assert "not overridable" in str(exc_info.value)
    assert POLICY_GATE_PIN_MISMATCH_MARKER in str(exc_info.value)
    assert exc_info.value.run_id == run.id
    # Refused BEFORE the guardrail pass — no rows were even loaded.
    assert load_rows.await_count == 0


@pytest.mark.asyncio
async def test_guardrail_override_still_runs_for_plain_guardrail_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discriminating sibling: a genuinely guardrail-blocked run (error_detail
    carries the guardrail block message, NOT the pin marker) passes the
    refusal check and reaches the re-run pass — proven by landing on the
    re-block error (``GuardrailOverrideRejectedError``) instead of the
    refusal."""
    from modulo.core.pipeline_engine.recovery import (
        GuardrailOverrideError,
        GuardrailOverrideRejectedError,
        guardrail_override,
    )

    run = _override_run(error_detail="Guardrail 'PII pattern' blocked the input")
    session = _override_session(run)
    load_rows = AsyncMock(return_value=[])
    monkeypatch.setattr("modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows", load_rows)
    re_run_pass = AsyncMock(
        return_value=MagicMock(
            blocked=True,
            blocking_eval_name="PII pattern",
            block_message="still matches",
            payload={},
        )
    )
    monkeypatch.setattr("modulo.core.guardrails.run_interception_pass_async", re_run_pass)

    with pytest.raises(GuardrailOverrideRejectedError) as exc_info:
        await guardrail_override(
            session,
            org_id=uuid.uuid4(),
            run_id=run.id,
            input_data={"input": "still bad"},
        )

    assert "still violates guardrail" in str(exc_info.value)
    # The refusal did NOT fire: the guardrail rows were loaded and the
    # re-run pass executed against the supplied input.
    assert load_rows.await_count == 1
    assert re_run_pass.await_count == 1
    # It is the re-block error (subclass), never the plain refusal — the
    # route maps these to different status codes (422 vs 409).
    assert type(exc_info.value) is not GuardrailOverrideError


# ---------------------------------------------------------------------------
# §5 — operator control SINGLE-SOURCED at the executor build (F8)
# ---------------------------------------------------------------------------


def test_operator_control_has_one_implementation_in_the_executor_build() -> None:
    """F8 load-bearing: disabled-gate exclusion exists in exactly ONE place —
    the executor's per-gate eval-def build (``eval_defs.gate_disabled_excluded``,
    exercised in ``test_policy_gate_eval_wiring.py`` C8/C9). The former
    duplicate filter copy at the run-start interception seam was removed;
    re-adding a second filter here would reintroduce the drift risk this
    single-sourcing exists to prevent."""
    import inspect

    import modulo.db.crud.run as run_crud

    source = inspect.getsource(run_crud)
    assert "_filter_disabled_policy_gates" not in source
    assert "_load_live_policy_gate_enabled_map" not in source
    # The seam's docstring/comment points at the authoritative site instead
    # of implementing its own copy.
    assert "gate_disabled_excluded" in source


@pytest.mark.asyncio
async def test_intercept_does_not_apply_operator_control_itself(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Behavioural half of F8: a pinned run whose snapshot pins verify
    cleanly passes through the interception seam with EXACTLY ONE query (the
    pin read) and NO ``operator_control_filtered`` audit — the seam must not
    load an enabled map or filter pins. Coverage of the exclusion itself
    lives at the executor build (wiring C8/C9)."""
    import logging

    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request()
    pins = _pins(2)
    matching_fp = fingerprint_policy_gate_pins(pins)

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((pins, matching_fp, 3))])

    with caplog.at_level(logging.INFO, logger="modulo.db.crud.run"):
        interception = await _intercept_guardrails(session, request)

    assert not interception.blocked
    assert not interception.block_message
    assert session.execute.await_count == 1, "the seam performs only the pin read — no enabled-map query"
    assert not [r for r in caplog.records if r.getMessage() == "policy_gates.operator_control_filtered"]


# ---------------------------------------------------------------------------
# C8 / C4 call-site wiring — _intercept_guardrails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intercept_no_snapshot_never_touches_the_policy_gate_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run carrying NO snapshot (``snapshot_id=None`` — e.g. a seed/fixture
    path) has nothing to verify: the policy-gate seam must not read or emit
    anything — zero session queries, no block.  (Runs WITH a snapshot always
    verify — see the non-replay run-start tests below; ``is_replay`` no
    longer gates the seam, spec §3.4 / criteria 4 + 16.)"""
    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request(is_replay=False, snapshot_id=None)
    session = AsyncMock(spec=AsyncSession)

    interception = await _intercept_guardrails(session, request)

    assert not interception.blocked
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_intercept_fingerprint_block_short_circuits_operator_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4 wiring: a fingerprint mismatch blocks IMMEDIATELY at run start —
    nothing else is read past the failed pin verification."""
    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request()
    pins = _pins(2)
    wrong_fp = fingerprint_policy_gate_pins(_pins(5))

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((pins, wrong_fp, None))])

    interception = await _intercept_guardrails(session, request)

    assert interception.blocked
    assert "fingerprint mismatch" in interception.block_message
    assert session.execute.await_count == 1, "no further reads must happen past a fingerprint block"


# ---------------------------------------------------------------------------
# C4/C16 run-start (not replay-only) — GAP 3 wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intercept_non_replay_mismatch_blocks_at_run_start(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C4 + C16 load-bearing at RUN START: a NON-replay run whose stored
    pin content disagrees with its saved fingerprint is blocked at the
    ingestion edge — before any evaluation.  Remove the verification (or
    re-gate it on ``is_replay``) and this test fails: the run would proceed.
    The user-facing message names the pipeline + snapshot (+ version), the
    TRUNCATED digests, and the remediation; the FULL digests go to the log."""
    import logging

    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request(is_replay=False)  # snapshot_id set, NOT a replay
    assert request.is_replay is False
    pins = _pins(2)
    tampered = [*pins, _gate_pin()]
    stored = fingerprint_policy_gate_pins(pins)
    recomputed = fingerprint_policy_gate_pins(tampered)

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((tampered, stored, 7))])

    with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
        interception = await _intercept_guardrails(session, request)

    assert interception.blocked, "a mismatch must block a non-replay run at run start"
    message = interception.block_message
    assert f"pipeline {request.pipeline_id}" in message, message
    assert f"snapshot {request.snapshot_id}" in message, message
    assert "(version 7)" in message, message
    assert "digest and content disagree" in message
    assert stored[:12] in message
    assert recomputed[:12] in message
    assert stored not in message, "user-facing message must carry TRUNCATED digests only"
    assert recomputed not in message, "user-facing message must carry TRUNCATED digests only"
    assert message.endswith("create a new run)")
    mismatch_records = [r for r in caplog.records if r.getMessage() == "policy_gates.pin_fingerprint_mismatch"]
    assert mismatch_records
    record = mismatch_records[-1]
    assert record.__dict__["stored_fingerprint"] == stored
    assert record.__dict__["recomputed_fingerprint"] == recomputed
    assert record.__dict__["pipeline_id"] == str(request.pipeline_id)
    # Terminal/non-retryable (C15): the block never reaches the guardrail
    # pass and the run-side stamp makes it eval_failed / never dispatched.
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_intercept_non_replay_matching_fingerprint_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C5's run-start sibling: verification at run start must NOT be an
    always-block — a non-replay run whose pins MATCH its fingerprint
    proceeds through the seam (with exactly the pin read — F8: no
    operator-control query lives here).  Together with the mismatch test
    above this proves the check is discriminating, not a blanket refusal
    (the always-block guard)."""
    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request(is_replay=False)
    pins = _pins(2)
    matching_fp = fingerprint_policy_gate_pins(pins)

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((pins, matching_fp, 3))])

    interception = await _intercept_guardrails(session, request)

    assert not interception.blocked
    assert not interception.block_message
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_intercept_non_replay_legacy_snapshot_falls_back_to_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6 at run start: a legacy snapshot (no pins, no fingerprint) on a
    NON-replay run falls back to the live gates — no block, and no spurious
    case-(ii) verification (exactly one query: the pin read)."""
    from modulo.db.crud.run import _intercept_guardrails

    _stubbed_guardrail_env(monkeypatch)
    request = _interception_request(is_replay=False)

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_row_result((None, None, None))])

    interception = await _intercept_guardrails(session, request)

    assert not interception.blocked
    assert session.execute.await_count == 1


# ---------------------------------------------------------------------------
# §3.1/3.2 — (C1, C3, C14) pin construction + snapshot persistence wiring
# ---------------------------------------------------------------------------


def test_build_policy_gate_pins_serialises_entry_fields() -> None:
    """C1: each pin entry carries policy_gate_id, eval_id, action, node_id —
    enough identity to reconstruct the gate's evaluation context and detect
    corruption of the action field."""
    gate_id = uuid.uuid4()
    node_id = uuid.uuid4()
    row = _policy_gate_row(gate_id, node_id=node_id, action="block")

    assert _build_policy_gate_pins([row]) == [
        {
            "policy_gate_id": str(gate_id),
            "eval_id": str(row.eval_id),
            "action": "block",
            "node_id": str(node_id),
        }
    ]


def test_build_policy_gate_pins_empty_list_when_no_rows() -> None:
    """F3: zero live gates → an EMPTY pin set (``[]``), never ``None``.
    Empty ≠ absent (§3.3): a snapshot with ``[]`` + its digest pins "zero
    gates" deliberately, so a gate disabled at snapshot time and re-enabled
    mid-run stays outside the evaluation universe (§5.4 interleavings 1/3)
    instead of falling back to live gates."""
    pins = _build_policy_gate_pins([])
    assert pins == []


@pytest.mark.asyncio
async def test_creation_loader_compiled_sql_excludes_disabled_and_deleted() -> None:
    """C14 load-bearing: the creation-time loader is the ONLY place gates
    are selected for pinning — its SQL must exclude disabled gates
    (``enabled IS true``) and soft-deleted gates (``deleted_at IS NULL``).
    Remove either predicate from the source and this fails."""
    from sqlalchemy.dialects import postgresql

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_scalars_result([]))

    await _load_policy_gate_rows_for_pipeline(
        session,
        pipeline_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
    )
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "policy_gates.enabled IS true" in compiled
    assert "policy_gates.deleted_at IS NULL" in compiled
    assert "evals.pipeline_id" in compiled
    assert "policy_gates.organisation_id" in compiled


def _creation_session(
    pipeline: MagicMock,
    edge: MagicMock,
    gate_rows: list[MagicMock],
) -> tuple[AsyncMock, Any]:
    """Return ``(session, lock_stub)`` for a snapshot-creation call.

    FAR-1287: the snapshot advisory lock is acquired/released on a DEDICATED
    connection from a dedicated NullPool engine resolved by
    ``_dedicated_lock_engine`` — never on the caller's session or its pool — so
    the lock/unlock results belong to the lock connection's ``execute``, and the
    session's sequence starts at the pipeline read. Enter the returned patch
    around the ``create_snapshot_from_live_graph`` call.
    """
    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = [lock_result, MagicMock()]  # try-lock, then unlock
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = AsyncMock(return_value=lock_conn)
    session.bind = engine
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        MagicMock(),  # FAR-1625 allocation row lock (result ignored)
        _scalar_result(1),
        _scalars_result([]),  # guardrail rows
        _scalars_result(gate_rows),  # policy gate rows
    ]
    return session, patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=engine)


def _simple_pipeline_and_edge() -> tuple[MagicMock, MagicMock]:
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.organisation_id = uuid.uuid4()
    pipeline.graph_nodes_json = [
        {"id": str(source_id), "agent_id": None, "connector_binding": None},
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}
    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_review_config = None
    edge.condition_expression = None
    return pipeline, edge


@pytest.mark.asyncio
async def test_creation_writes_policy_gate_pins_and_fingerprint_to_snapshot() -> None:
    """C1 + C3 end-to-end through ``create_snapshot_from_live_graph``: gate
    rows are serialised into ``policy_gate_pins_json`` and the snapshot's
    ``policy_gate_pins_fingerprint`` matches ``fingerprint_policy_gate_pins``
    of the STORED pins."""
    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

    pipeline, edge = _simple_pipeline_and_edge()
    gate_rows = [_policy_gate_row(uuid.uuid4()), _policy_gate_row(uuid.uuid4())]
    expected_pins = [
        {
            "policy_gate_id": str(row.id),
            "eval_id": str(row.eval_id),
            "action": row.action,
            "node_id": str(row.node_id),
        }
        for row in gate_rows
    ]

    session, lock_stub = _creation_session(pipeline, edge, gate_rows)
    with lock_stub:
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline.id)

    assert snapshot is not None
    assert snapshot.policy_gate_pins_json == expected_pins
    assert snapshot.policy_gate_pins_fingerprint == fingerprint_policy_gate_pins(expected_pins)
    assert len(snapshot.policy_gate_pins_fingerprint) == 64


@pytest.mark.asyncio
async def test_creation_zero_gates_store_empty_pin_set_with_digest() -> None:
    """F3 creation-side: a pipeline with no live gates snapshots with pins
    ``[]`` AND a fingerprint (the deterministic empty-set digest) — the
    deliberate "zero gates pinned" state, NOT ``None`` (which is reserved
    for genuinely pre-mechanism snapshots)."""
    from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph

    pipeline, edge = _simple_pipeline_and_edge()
    session, lock_stub = _creation_session(pipeline, edge, [])
    with lock_stub:
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline.id)

    assert snapshot is not None
    stored_pins = snapshot.policy_gate_pins_json
    assert stored_pins == []
    assert snapshot.policy_gate_pins_fingerprint == fingerprint_policy_gate_pins([])


# ---------------------------------------------------------------------------
# (C11, C12) schema contract — via sqlalchemy.inspect
# ---------------------------------------------------------------------------


def _sqlite_engine_for(tables: list[type]) -> Any:
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    for model in tables:
        model.__table__.create(engine, checkfirst=True)
    return engine


def test_schema_policy_gate_enabled_columns_contract() -> None:
    """C11: ``enabled`` is BOOLEAN NOT NULL DEFAULT true; the audit pair
    ``enabled_at``/``disabled_at`` exists and is nullable."""
    engine = _sqlite_engine_for([PolicyGate])
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("policy_gates")}
        enabled = columns["enabled"]
        assert not enabled["nullable"]
        assert "BOOLEAN" in str(enabled["type"]).upper()
        assert enabled["default"] is not None
        assert "true" in str(enabled["default"]).lower()
        for nullable_audit_column in ("enabled_at", "disabled_at"):
            assert nullable_audit_column in columns
            assert columns[nullable_audit_column]["nullable"]
    finally:
        engine.dispose()


def test_enabled_at_server_default_renders_portably() -> None:
    """F4: ``enabled_at`` carries a SERVER default so a raw SQL insert that
    omits the column (an OLD container during a rolling deploy, whose
    pre-0272 model knows no such column) still satisfies
    ck_policy_gates_enabled_timestamps. The default must be ``func.now()`` —
    it renders ``CURRENT_TIMESTAMP`` on SQLite and ``now()`` on Postgres.
    ``text("now()")`` instead would render ``DEFAULT (now())``, which SQLite
    parses but rejects at insert time ("unknown function: now()"), breaking
    every create_all-built table."""
    from sqlalchemy.dialects import postgresql, sqlite

    table = PolicyGate.__table__
    assert table.c.enabled_at.server_default is not None
    sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    assert "enabled_at DATETIME DEFAULT CURRENT_TIMESTAMP" in sqlite_ddl
    pg_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "enabled_at TIMESTAMP WITH TIME ZONE DEFAULT now()" in pg_ddl


def test_raw_insert_omitting_enabled_at_takes_the_server_default() -> None:
    """F4 behavioural half: an insert in the OLD-container shape (enabled
    set, enabled_at omitted entirely) succeeds against the model-built table
    because the server default fills the stamp — without it the symmetric
    CHECK rejects the row and gate create/replace 500s for the whole
    rollout."""
    from sqlalchemy import text

    engine = _sqlite_engine_for([PolicyGate])
    try:
        ids = {
            "i": str(uuid.uuid4()),
            "o": str(uuid.uuid4()),
            "e": str(uuid.uuid4()),
            "n": str(uuid.uuid4()),
        }
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                    "action, version, enabled) "
                    "VALUES (:i, :o, :e, :n, 'warn', 1, 1)"
                ),
                ids,
            )
            enabled_at = conn.execute(
                text("SELECT enabled_at FROM policy_gates WHERE id = :i"),
                ids,
            ).scalar()
        assert enabled_at is not None, "the server default must fill enabled_at so the CHECK passes"
    finally:
        engine.dispose()


def test_schema_policy_gate_enabled_timestamps_check_on_model() -> None:
    """C11 parity: the symmetric CHECK ships on the MODEL, not only in
    migration 0272 — ``metadata.create_all()`` / SQLite-mirror CTAS must
    match the shipped post-0272 contract (FAR-967 F1)."""
    from sqlalchemy import CheckConstraint

    checks = [
        c
        for c in PolicyGate.__table_args__
        if isinstance(c, CheckConstraint) and c.name == "ck_policy_gates_enabled_timestamps"
    ]
    assert len(checks) == 1, "PolicyGate model must declare ck_policy_gates_enabled_timestamps"
    predicate = str(checks[0].sqltext)
    assert "enabled AND enabled_at IS NOT NULL AND disabled_at IS NULL" in predicate
    assert "NOT enabled AND disabled_at IS NOT NULL AND enabled_at IS NULL" in predicate
    ddl = str(CreateTable(PolicyGate.__table__))
    assert "ck_policy_gates_enabled_timestamps" in ddl


def test_sqlite_create_all_enforces_enabled_timestamps_check() -> None:
    """The model-level CHECK is LIVE on a create_all-built SQLite table:
    an enabled row without enabled_at is rejected (mirror of migration
    0272), while the creation state (enabled + enabled_at set, disabled_at
    NULL) is accepted."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    engine = _sqlite_engine_for([PolicyGate])
    try:
        invalid = {
            "i": str(uuid.uuid4()),
            "o": str(uuid.uuid4()),
            "e": str(uuid.uuid4()),
            "n": str(uuid.uuid4()),
        }
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                    "action, version, enabled, enabled_at, disabled_at) "
                    "VALUES (:i, :o, :e, :n, 'warn', 1, 1, NULL, NULL)"
                ),
                invalid,
            )
        valid = {
            "i": str(uuid.uuid4()),
            "o": str(uuid.uuid4()),
            "e": str(uuid.uuid4()),
            "n": str(uuid.uuid4()),
        }
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO policy_gates (id, organisation_id, eval_id, node_id, "
                    "action, version, enabled, enabled_at, disabled_at) "
                    "VALUES (:i, :o, :e, :n, 'warn', 1, 1, '2026-01-01 00:00:00', NULL)"
                ),
                valid,
            )
    finally:
        engine.dispose()


def test_orm_insert_stamps_enabled_at_by_default() -> None:
    """§4.3 / CO-2 creation semantics, enforced at the MODEL: every ORM
    insert path stamps ``enabled_at`` so the symmetric CHECK is satisfied by
    construction.  ``eval_definition_write`` persists an Eval+PolicyGate
    WITHOUT touching the audit columns — before the model default, that
    insert violated ``ck_policy_gates_enabled_timestamps`` on Postgres
    (IntegrationError) for every eval created with a failure behaviour."""
    from sqlalchemy.orm import Session

    engine = _sqlite_engine_for([PolicyGate])
    try:
        with Session(engine) as session:
            gate = PolicyGate(
                id=uuid.uuid4(),
                organisation_id=uuid.uuid4(),
                eval_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                action="warn",
                version=1,
                # enabled / enabled_at deliberately omitted — the defaults
                # must produce the CHECK-acceptable creation state.
            )
            session.add(gate)
            session.flush()  # INSERT — raises IntegrityError without the default

            assert gate.enabled is True
            assert gate.enabled_at is not None
            assert gate.disabled_at is None
    finally:
        engine.dispose()


def test_schema_snapshot_policy_gate_pin_columns_contract() -> None:
    """C12: ``policy_gate_pins_fingerprint`` is a nullable VARCHAR(64) and
    ``policy_gate_pins_json`` is nullable JSON — legacy snapshots (no pins,
    no fingerprint) load cleanly."""
    engine = _sqlite_engine_for([PipelineSnapshot])
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("pipeline_snapshots")}
        fingerprint = columns["policy_gate_pins_fingerprint"]
        assert fingerprint["nullable"]
        assert "VARCHAR" in str(fingerprint["type"]).upper()
        assert fingerprint["type"].length == 64
        assert columns["policy_gate_pins_json"]["nullable"]
    finally:
        engine.dispose()


def test_create_table_ddl_for_policy_gate_pins_fingerprint_is_char_64() -> None:
    """C12 belt-and-braces: the rendered DDL carries VARCHAR(64) for the
    fingerprint column — the length is never silently dropped."""
    ddl = str(CreateTable(PipelineSnapshot.__table__))
    assert "policy_gate_pins_fingerprint VARCHAR(64)" in ddl
